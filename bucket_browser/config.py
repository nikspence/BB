"""Atomic XML settings with account-bound secrets and opt-in portable exports."""
import base64
import ctypes
import os
import re
import sys
from pathlib import Path
import tempfile
import xml.etree.ElementTree as ET

from .models import Connection, Preferences, SECRETS, FIELDS


def dpapi(value: bytes, decrypt=False) -> bytes:
    if sys.platform != "win32":
        raise RuntimeError("This XML uses Windows DPAPI. Export portable XML on the original Windows account, then import it here.")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel.LocalFree(target.pbData)


def default_path():
    if sys.platform == "win32":
        folder = Path(os.environ.get("APPDATA") or Path.home()) / "Bucket Browser"
    elif sys.platform == "darwin":
        folder = Path.home() / "Library" / "Application Support" / "Bucket Browser"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        # The XDG specification requires an absolute path.
        if not base.is_absolute():
            base = Path.home() / ".config"
        folder = base / "bucket-browser"
    return folder / "connections.xml"


def credential_store():
    """Choose only native secure stores; never accept a plaintext fallback backend."""
    if sys.platform == "darwin":
        from keyring.backends.macOS import Keyring
    elif sys.platform.startswith("linux"):
        from keyring.backends.SecretService import Keyring
    else:
        raise RuntimeError("No supported secure credential store for this operating system.")
    return Keyring()


def credential_cipher(create=False):
    if sys.platform == "win32":
        raise RuntimeError("This XML uses a macOS/Linux keychain key. Export portable XML on the original computer/account, then import it here.")
    from cryptography.fernet import Fernet

    service, account = "Bucket Browser", "settings-encryption-v1"
    try:
        store = credential_store()
        key = store.get_password(service, account)
        if key is None:
            if not create:
                raise RuntimeError("The encryption key is missing. Restore the original keychain or import a portable XML export.")
            key = Fernet.generate_key().decode("ascii")
            store.set_password(service, account, key)
        return Fernet(key.encode("ascii"))
    except Exception as exc:
        raise RuntimeError("Cannot access BB's secure credentials. Unlock macOS Keychain or enable and unlock a Linux Secret Service (such as GNOME Keyring). " + str(exc)) from exc


def save(path, preferences, connections, mode="encrypted"):
    if mode not in {"encrypted", "portable", "redacted"}:
        raise ValueError("Invalid credential mode")
    if mode == "encrypted" and sys.platform != "win32":
        mode = "keyring"
    cipher = None
    root = ET.Element("bucket-browser", version="1", credentials=mode)
    ET.SubElement(root, "preferences", {k: str(v) for k, v in vars(preferences).items()})
    parent = ET.SubElement(root, "connections")
    for connection in connections:
        node = ET.SubElement(parent, "connection", id=connection.id, name=connection.name,
                             provider=connection.provider, bucket=connection.bucket)
        for key, value in connection.settings.items():
            if key in SECRETS:
                if mode == "redacted":
                    continue
                if mode == "encrypted":
                    value = base64.b64encode(dpapi(value.encode())).decode() if value else ""
                elif mode == "keyring" and value:
                    if cipher is None:
                        cipher = credential_cipher(create=True)
                    value = cipher.encrypt(value.encode()).decode("ascii")
            ET.SubElement(node, "setting", name=key).text = value
    ET.indent(root)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".bb-", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as output:
            output.write(ET.tostring(root, encoding="utf-8", xml_declaration=True))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load(path):
    path = Path(path)
    if path.stat().st_size > 5 * 1024 * 1024:
        raise ValueError("Settings XML is too large.")
    # Only UTF-8 XML is accepted. Reject declarations before parsing so entity
    # expansion and external entity definitions cannot enter the settings reader.
    document = path.read_bytes().decode("utf-8-sig")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", document, re.IGNORECASE):
        raise ValueError("DTD and entity declarations are not allowed in settings XML.")
    root = ET.fromstring(document)
    if root.tag != "bucket-browser" or root.get("version") != "1":
        raise ValueError("Unsupported Bucket Browser XML file.")
    mode = root.get("credentials", "redacted")
    if mode not in {"encrypted", "keyring", "portable", "redacted"}:
        raise ValueError("Unsupported credential encoding.")
    cipher = None
    node = root.find("preferences")
    values = node.attrib if node is not None else {}
    preferences = Preferences(**{key: value for key, value in values.items()
                                 if key in Preferences.__dataclass_fields__ and key != "touch"})
    preferences.touch = values.get("touch", "True") == "True"
    if preferences.theme not in {"dark", "light"}:
        preferences.theme = "dark"
    connections = []
    seen = set()
    for node in root.findall("connections/connection"):
        connection = Connection(name=node.get("name", ""), provider=node.get("provider", ""),
                                bucket=node.get("bucket", ""), id=node.get("id", ""))
        if connection.provider not in FIELDS or not connection.id or connection.id in seen:
            raise ValueError("Invalid or duplicate connection in XML.")
        seen.add(connection.id)
        allowed = {item[0] for item in FIELDS[connection.provider]}
        for item in node.findall("setting"):
            key, value = item.get("name"), item.text or ""
            if key not in allowed:
                continue
            if key in SECRETS and mode == "encrypted" and value:
                value = dpapi(base64.b64decode(value, validate=True), decrypt=True).decode()
            elif key in SECRETS and mode == "keyring" and value:
                if cipher is None:
                    cipher = credential_cipher()
                from cryptography.fernet import InvalidToken
                try:
                    value = cipher.decrypt(value.encode("ascii")).decode()
                except InvalidToken as exc:
                    raise ValueError("Cannot decrypt credentials with this account's keychain. Import a portable XML export from the original computer/account.") from exc
            connection.settings[key] = value
        connections.append(connection)
    if preferences.default_connection not in seen:
        preferences.default_connection = preferences.default_bucket = ""
    return preferences, connections
