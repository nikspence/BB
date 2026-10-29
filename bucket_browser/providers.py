"""Provider contract and adapters. SDK imports are lazy; add adapters to REGISTRY."""
from abc import ABC, abstractmethod
from pathlib import Path
import json
import mimetypes
from urllib.parse import quote
from uuid import uuid4

from .models import Entry


def stamp(value):
    return value.isoformat(sep=" ", timespec="seconds") if hasattr(value, "isoformat") else str(value or "")


class Provider(ABC):
    flat = False

    @abstractmethod
    def buckets(self): ...

    @abstractmethod
    def list(self, bucket, prefix=""): ...

    @abstractmethod
    def download(self, bucket, entry, target, progress): ...

    @abstractmethod
    def upload(self, bucket, key, source, progress):
        """Create a file, rejecting an existing destination. Return its Entry."""

    @abstractmethod
    def delete(self, bucket, entry): ...

    def check_bucket(self, bucket):
        # Exhausted lazily by some adapters; listing verifies read access.
        self.list(bucket)


def hierarchy(entries, prefix):
    result, folders = [], set()
    for entry in entries:
        rest = entry.key[len(prefix):]
        if not rest:
            continue
        if "/" in rest:
            folder = rest.split("/", 1)[0]
            if folder not in folders:
                result.append(Entry(prefix + folder + "/", folder, is_dir=True))
                folders.add(folder)
        else:
            result.append(Entry(entry.key, rest, entry.size, entry.modified, version=entry.version))
    return sorted(result, key=lambda item: (not item.is_dir, item.name.casefold()))


class Azure(Provider):
    def __init__(self, settings):
        from azure.storage.blob import BlobServiceClient
        value = settings.get("connection_string") or (
            "DefaultEndpointsProtocol=https;AccountName={account_name};AccountKey={account_key};"
            "EndpointSuffix={endpoint_suffix}".format(**settings))
        self.client = BlobServiceClient.from_connection_string(value, connection_timeout=15, read_timeout=60)

    def buckets(self):
        return [(item.name, item.name) for item in self.client.list_containers()]

    def list(self, bucket, prefix=""):
        from azure.storage.blob import BlobPrefix
        output = []
        for item in self.client.get_container_client(bucket).walk_blobs(name_starts_with=prefix, delimiter="/"):
            if isinstance(item, BlobPrefix):
                output.append(Entry(item.name, item.name[len(prefix):].rstrip("/"), is_dir=True))
            else:
                output.append(Entry(item.name, item.name[len(prefix):], item.size,
                                    stamp(item.last_modified), version=item.etag))
        return sorted(output, key=lambda e: (not e.is_dir, e.name.casefold()))

    def download(self, bucket, entry, target, progress):
        from azure.core import MatchConditions
        options = {"etag": entry.version, "match_condition": MatchConditions.IfNotModified} if entry.version else {}
        stream = self.client.get_blob_client(bucket, entry.key).download_blob(**options)
        for chunk in stream.chunks():
            target.write(chunk)
            progress(len(chunk))

    def upload(self, bucket, key, source, progress):
        blob = self.client.get_blob_client(bucket, key)
        previous = 0
        def update(current, total):
            nonlocal previous
            progress(max(0, current - previous))
            previous = current
        blob.upload_blob(source, overwrite=False, progress_hook=update)
        info = blob.get_blob_properties()
        return Entry(key, key.rsplit("/", 1)[-1], info.size, version=info.etag)

    def delete(self, bucket, entry):
        from azure.core import MatchConditions
        options = {"etag": entry.version, "match_condition": MatchConditions.IfNotModified} if entry.version else {}
        self.client.get_blob_client(bucket, entry.key).delete_blob(**options)


class S3(Provider):
    def __init__(self, settings):
        import boto3
        from botocore.config import Config
        self.client = boto3.client("s3", aws_access_key_id=settings["key_id"],
                                  aws_secret_access_key=settings["access_key"],
                                  aws_session_token=settings.get("session_token") or None,
                                  region_name=settings["region"], endpoint_url=settings.get("endpoint") or None,
                                  config=Config(connect_timeout=15, read_timeout=60,
                                                retries={"max_attempts": 3},
                                                request_checksum_calculation="when_required",
                                                response_checksum_validation="when_required",
                                                s3={"addressing_style": "path"} if settings.get("endpoint") else {}))

    def buckets(self):
        return [(b["Name"], b["Name"]) for page in self.client.get_paginator("list_buckets").paginate()
                for b in page.get("Buckets", [])]

    def list(self, bucket, prefix=""):
        result = []
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
            result.extend(Entry(p["Prefix"], p["Prefix"][len(prefix):].rstrip("/"), is_dir=True)
                          for p in page.get("CommonPrefixes", []))
            result.extend(Entry(p["Key"], p["Key"][len(prefix):], p["Size"], stamp(p.get("LastModified")),
                                version=p.get("ETag", ""))
                          for p in page.get("Contents", []) if p["Key"] != prefix)
        return sorted(result, key=lambda e: (not e.is_dir, e.name.casefold()))

    def download(self, bucket, entry, target, progress):
        options = {"IfMatch": entry.version} if entry.version else {}
        result = self.client.get_object(Bucket=bucket, Key=entry.key, **options)
        with result["Body"] as body:
            for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                target.write(chunk)
                progress(len(chunk))

    def upload(self, bucket, key, source, progress):
        size = Path(source.name).stat().st_size
        if size < 64 * 1024 ** 2:
            response = self.client.put_object(Bucket=bucket, Key=key, Body=source, ContentLength=size, IfNoneMatch="*")
            progress(size)
        else:
            part_size = max(16 * 1024 ** 2, (size + 9998) // 9999)
            if part_size > 5 * 1024 ** 3:
                raise ValueError("File exceeds the S3 multipart size limit.")
            upload_id = self.client.create_multipart_upload(Bucket=bucket, Key=key)["UploadId"]
            parts = []
            try:
                while True:
                    progress(0)
                    chunk = source.read(part_size)
                    if not chunk:
                        break
                    number = len(parts) + 1
                    result = self.client.upload_part(Bucket=bucket, Key=key, UploadId=upload_id,
                                                     PartNumber=number, Body=chunk)
                    parts.append({"ETag": result["ETag"], "PartNumber": number})
                    progress(len(chunk))
                progress(0)
                response = self.client.complete_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id,
                                                                 MultipartUpload={"Parts": parts}, IfNoneMatch="*")
            except Exception:
                try:
                    self.client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
                except Exception:
                    pass
                raise
        return Entry(key, key.rsplit("/", 1)[-1], size, version=response.get("ETag", ""))

    def delete(self, bucket, entry):
        options = {"IfMatch": entry.version} if entry.version else {}
        self.client.delete_object(Bucket=bucket, Key=entry.key, **options)


class Appwrite(Provider):
    flat = True

    def __init__(self, settings):
        import urllib3
        from urllib3.exceptions import InsecureRequestWarning

        # Equivalent to Appwrite Client.set_self_signed(1) for this REST adapter.
        urllib3.disable_warnings(InsecureRequestWarning)
        self.endpoint = settings["endpoint"].rstrip("/")
        self.headers = {"X-Appwrite-Project": settings["project_id"], "X-Appwrite-Key": settings["api_key"]}

    def request(self, method, path, **kwargs):
        import requests
        # Do not forward server keys on redirects to another host.
        result = requests.request(method, self.endpoint + "/storage/" + path,
                                  headers={**self.headers, **kwargs.pop("headers", {})},
                                  timeout=(15, 120), allow_redirects=False, verify=False, **kwargs)
        if not 200 <= result.status_code < 300:
            status = result.status_code
            result.close()
            raise RuntimeError(f"Appwrite request failed (HTTP {status}). Check endpoint, scopes, and bucket rules.")
        return result

    def pages(self, path, collection):
        cursor = None
        while True:
            queries = [{"method": "limit", "values": [100]}]
            if cursor:
                queries.append({"method": "cursorAfter", "values": [cursor]})
            with self.request("GET", path, params=[("queries[]", json.dumps(q)) for q in queries]) as response:
                rows = response.json()[collection]
            yield from rows
            if len(rows) < 100:
                break
            next_cursor = rows[-1]["$id"]
            if next_cursor == cursor:
                raise RuntimeError("Appwrite returned a repeated pagination cursor.")
            cursor = next_cursor

    def path(self, bucket, key=""):
        return "buckets/" + quote(bucket, safe="") + "/files" + ("/" + quote(key, safe="") if key else "")

    def buckets(self):
        return [(b["$id"], b["name"]) for b in self.pages("buckets", "buckets")]

    def list(self, bucket, prefix=""):
        return sorted([Entry(f["$id"], f["name"], f.get("sizeOriginal", 0), f.get("$updatedAt", ""))
                       for f in self.pages(self.path(bucket), "files")], key=lambda e: e.name.casefold())

    def download(self, bucket, entry, target, progress):
        with self.request("GET", self.path(bucket, entry.key) + "/download", stream=True) as response:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    target.write(chunk)
                    progress(len(chunk))

    def upload(self, bucket, key, source, progress):
        if any(e.name == key for e in self.list(bucket)):
            raise FileExistsError(f"A file named {key!r} already exists in this Appwrite bucket.")
        size = Path(source.name).stat().st_size
        file_id, offset = uuid4().hex, 0
        try:
            while True:
                chunk = source.read(5 * 1024 * 1024)
                headers = {}
                if size > 5 * 1024 * 1024:
                    headers["Content-Range"] = f"bytes {offset}-{offset + len(chunk) - 1}/{size}"
                if offset:
                    headers["X-Appwrite-ID"] = file_id
                with self.request("POST", self.path(bucket), headers=headers, data={"fileId": file_id},
                                  files={"file": (key, chunk, mimetypes.guess_type(key)[0] or "application/octet-stream")}) as response:
                    info = response.json()
                progress(len(chunk))
                offset += len(chunk)
                if offset >= size:
                    return Entry(info["$id"], info["name"], info.get("sizeOriginal", size))
        except Exception:
            # Partial uploads remain identifiable by ID; attempt cleanup without masking the error.
            try:
                self.delete(bucket, Entry(file_id, key))
            except Exception:
                pass
            raise

    def rename(self, bucket, entry, name):
        if any(e.name == name and e.key != entry.key for e in self.list(bucket)):
            raise FileExistsError("A file with this name already exists.")
        with self.request("PUT", self.path(bucket, entry.key), json={"name": name}):
            pass

    def delete(self, bucket, entry):
        with self.request("DELETE", self.path(bucket, entry.key)):
            pass


REGISTRY = {"azure": Azure, "s3": S3, "appwrite": Appwrite}


def connect(connection):
    connection.validate()
    return REGISTRY[connection.provider](connection.settings)
