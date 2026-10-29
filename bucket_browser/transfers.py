"""Disk-backed copy operations and a serial, cancellable background queue."""
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
import os
import queue
import sys
import tempfile
import threading
from uuid import uuid4

from .models import Entry


def safe_destination(root, relative):
    # Object keys are untrusted, including Windows drive, ADS and reserved-name syntax.
    name = PurePosixPath(relative)
    if name.is_absolute() or not name.parts or "\\" in relative or PureWindowsPath(relative).drive:
        raise ValueError("Unsafe destination filename: " + relative)
    for part in name.parts:
        base = part.split(".", 1)[0].upper()
        if part in {".", ".."} or any(c in part for c in '<>:"|?*') or any(ord(c) < 32 for c in part) or part.endswith((" ", ".")):
            raise ValueError("Unsafe destination filename: " + relative)
        if base in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
            raise ValueError("Windows reserved filename: " + relative)
    root = Path(root).resolve()
    target = root.joinpath(*name.parts).resolve()
    if not target.is_relative_to(root) or target == root:
        raise ValueError("Destination escapes the selected local folder.")
    return target


@dataclass
class Location:
    provider: object = None
    bucket: str = ""
    prefix: str = ""
    local: str = ""

    @property
    def label(self):
        return self.local if self.provider is None else self.bucket + "/" + self.prefix


@dataclass
class Job:
    label: str
    action: object
    id: str = field(default_factory=lambda: uuid4().hex)
    state: str = "Queued"
    done: int = 0
    cancel: threading.Event = field(default_factory=threading.Event)
    error: str = ""


class Cancelled(Exception):
    pass


def publish_file(source, target):
    """Publish a file without replacing a destination created by another process."""
    if sys.platform == "win32":
        os.rename(source, target)
    else:
        # A same-filesystem hard link fails atomically if target already exists.
        # Filesystems without hard links fail safely instead of overwriting.
        os.link(source, target, follow_symlinks=False)
        os.unlink(source)


class TransferQueue:
    def __init__(self, changed, completed):
        self.changed, self.completed = changed, completed
        self.queue = queue.Queue()
        self.jobs = []
        self.lock = threading.Lock()
        self.closed = False
        self.thread = threading.Thread(target=self.run, name="BB transfers", daemon=True)
        self.thread.start()

    def submit(self, label, action):
        with self.lock:
            if self.closed:
                raise RuntimeError("Transfer queue is closed")
            job = Job(label, action)
            self.jobs.append(job)
            self.queue.put(job)
        self.changed(job)
        return job

    def run(self):
        while True:
            job = self.queue.get()
            if job is None:
                return
            def progress(count=0):
                if job.cancel.is_set():
                    raise Cancelled()
                job.done += count
                self.changed(job)
            try:
                progress()
                job.state = "Running"
                self.changed(job)
                job.action(progress)
                # A completed provider request is successful even if cancel arrived during its final call.
                job.state = "Completed"
            except Cancelled:
                job.state = "Cancelled"
            except Exception as exc:
                job.state, job.error = "Failed", str(exc)
            finally:
                self.changed(job)
                self.completed(job)
                self.queue.task_done()

    def close(self):
        with self.lock:
            self.closed = True
            for job in self.jobs:
                job.cancel.set()
            self.queue.put(None)


def copy_file(source, entry, destination, relative, progress):
    """Stage remote transfers on disk; publish local files only after a complete download."""
    progress(0)
    if destination.provider is None:
        target = safe_destination(destination.local, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise FileExistsError("Destination already exists: " + str(target))
        fd, temporary = tempfile.mkstemp(prefix=".bb-", suffix=".part", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                read_into(source, entry, output, progress)
                output.flush()
                os.fsync(output.fileno())
            progress(0)
            publish_file(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    elif source.provider is None:
        with open(entry.key, "rb") as stream:
            progress(0)
            destination.provider.upload(destination.bucket, destination.prefix + relative, stream, progress)
    else:
        with tempfile.TemporaryDirectory(prefix="bb-transfer-") as folder:
            path = Path(folder) / "payload"
            with path.open("wb") as output:
                read_into(source, entry, output, progress)
            progress(0)
            with path.open("rb") as stream:
                destination.provider.upload(destination.bucket, destination.prefix + relative, stream, progress)


def read_into(location, entry, output, progress):
    if location.provider is not None:
        location.provider.download(location.bucket, entry, output, progress)
    else:
        with open(entry.key, "rb") as stream:
            while chunk := stream.read(1024 * 1024):
                output.write(chunk)
                progress(len(chunk))


def copy_tree(source, entries, destination, progress):
    """Snapshot the source tree before writing anything, including across sessions.

    This prevents a destination nested inside the source on the same service
    from being rediscovered repeatedly while files are being created.
    """
    files, directories = [], []
    stack = [(source, entry, entry.name) for entry in reversed(entries)]
    while stack:
        progress(0)
        current, entry, relative = stack.pop()
        if not entry.is_dir:
            files.append((current, entry, relative))
            continue
        if destination.provider and destination.provider.flat:
            raise ValueError("Appwrite has a flat file list. Open the source folder and select its files.")
        if current.provider is None:
            path = Path(entry.key)
            if path.is_symlink() or path.is_junction():
                raise ValueError("Directory links and junctions are not followed.")
            if destination.provider is None:
                target = safe_destination(destination.local, relative)
                if target.is_relative_to(path.resolve()):
                    raise ValueError("Cannot copy a folder into itself.")
            children = []
            for child in path.iterdir():
                info = child.stat()
                children.append(Entry(str(child), child.name, info.st_size, is_dir=child.is_dir()))
            child_source = Location(local=str(path))
        else:
            if current.provider is destination.provider and current.bucket == destination.bucket and destination.prefix.startswith(entry.key):
                raise ValueError("Cannot copy a cloud folder into itself.")
            children = current.provider.list(current.bucket, entry.key)
            child_source = Location(current.provider, current.bucket, entry.key)
        directories.append(relative)
        stack.extend((child_source, child, relative + "/" + child.name) for child in reversed(children))
    if destination.provider is None:
        for relative in directories:
            safe_destination(destination.local, relative).mkdir(parents=True, exist_ok=True)
    for current, entry, relative in files:
        copy_file(current, entry, destination, relative, progress)


def rename_remote(location, entry, new_name, progress):
    if not new_name.strip() or "/" in new_name or "\\" in new_name or new_name in {".", ".."}:
        raise ValueError("Enter a filename without folder separators.")
    provider = location.provider
    if provider.flat:
        provider.rename(location.bucket, entry, new_name)
        return
    key = location.prefix + new_name
    if key == entry.key:
        return
    # Copy succeeds before source deletion; a failed delete leaves both files intact.
    copy_file(location, entry, location, new_name, progress)
    try:
        provider.delete(location.bucket, entry)
    except Exception as exc:
        raise RuntimeError("New file was created, but source deletion failed; both may remain.") from exc
