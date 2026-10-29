from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4


PROVIDERS = {"azure": "Azure Blob Storage", "s3": "S3 / S3 compatible", "appwrite": "Appwrite"}
FIELDS = {
    "azure": [("connection_string", "Connection string", True),
              ("account_name", "Account name", False), ("account_key", "Access key", True),
              ("endpoint_suffix", "Endpoint suffix", False)],
    "s3": [("key_id", "Access key ID", False), ("access_key", "Secret access key", True),
           ("region", "Region", False), ("endpoint", "Custom endpoint URL (optional)", False),
           ("session_token", "Session token (optional)", True)],
    "appwrite": [("endpoint", "Endpoint URL (including /v1)", False),
                 ("project_id", "Project ID", False), ("api_key", "API key", True)],
}
SECRETS = {"connection_string", "account_key", "access_key", "session_token", "api_key"}


@dataclass
class Connection:
    name: str
    provider: str
    settings: dict[str, str] = field(default_factory=dict, repr=False)
    bucket: str = ""
    id: str = field(default_factory=lambda: uuid4().hex)

    def validate(self):
        if not self.name.strip():
            raise ValueError("Enter a connection name.")
        if self.provider not in PROVIDERS:
            raise ValueError("Unknown storage provider.")
        required = {"s3": ["key_id", "access_key", "region"],
                    "appwrite": ["endpoint", "project_id", "api_key"],
                    "azure": [] if self.settings.get("connection_string") else
                    ["account_name", "account_key", "endpoint_suffix"]}[self.provider]
        missing = [key for key in required if not self.settings.get(key, "").strip()]
        if missing:
            raise ValueError("Required fields: " + ", ".join(missing))
        endpoint = self.settings.get("endpoint", "")
        if endpoint and not endpoint.startswith(("https://", "http://")):
            raise ValueError("Endpoint must start with https:// or http://.")


@dataclass
class Preferences:
    theme: str = "dark"
    touch: bool = True
    default_connection: str = ""
    default_bucket: str = ""
    local_path: str = field(default_factory=lambda: str(Path.home()))


@dataclass(frozen=True)
class Entry:
    key: str
    name: str
    size: int = 0
    modified: str = ""
    is_dir: bool = False
    version: str = ""
