# Bucket Browser (BB)

A Python desktop utility built with wxPython for Windows, macOS, and Linux, supporting local files, Azure Blob Storage, AWS S3 / S3-compatible services, and Appwrite Storage. Dark appearance is the default. macOS and Linux support has been implemented with simulated platform tests on Windows; native validation on those systems is still required.

## Run on Windows

Install Python 3.12 or newer, then open PowerShell in this folder:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe bb.py
```

Alternatively, with dependencies installed in your active Python environment, run `python bb.py`. `start-bb.cmd` uses `.venv` when available. No administrator rights are required. A working package index is required for initial dependency installation.

Keep `bucket.png` beside `bb.py` when distributing the application. This generated bucket artwork has a transparent background. BB builds a `wx.IconBundle` from it at sizes 16–256 pixels and applies it with the window's `SetIcons` method. The icon path is resolved relative to the application files, so launching from another working directory also works. The generation prompt is recorded in `bucket-icon-prompt.txt`.

## Run on macOS or Linux

Install Python 3.12 or newer with a working wxPython installation, then run from a terminal in this folder:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python bb.py
```

Alternatively, run `sh start-bb.sh` to use `.venv` when available, or `python3` from your active environment. The launcher can be invoked from another working directory. Run BB in a graphical desktop session, as your regular user.

On macOS, use a Python installation compatible with the wxPython macOS wheels and allow BB access to Keychain when prompted. On Linux, wxPython may need GTK development libraries and a source build if no wheel matches your distribution/Python. Follow the [official wxPython installation instructions](https://wxpython.org/pages/downloads/) for your platform. If using distribution-provided wxPython, ensure it targets Python 3.12+ and make it visible to the environment (for example, create the venv with `--system-site-packages`).

Saved credentials on Linux require an unlocked Secret Service over the desktop session's D-Bus, such as GNOME Keyring or a compatible KWallet service. The Python `keyring` dependency installs its Linux SecretStorage support; the desktop service must also be installed and running. BB selects the native secure backend explicitly and never falls back to plaintext storage. A missing/locked keychain produces an actionable error and preserves existing settings. See [keyring's platform requirements](https://keyring.readthedocs.io/en/stable/).

## Connect and browse

1. Open **Connections & preferences**, select **New**, and choose a provider.
2. Supply its credentials:
   - **Azure:** a complete connection string, or account name, access key, and endpoint suffix (defaults to `core.windows.net`). The connection string takes precedence over the separate fields.
   - **S3:** access key ID, secret access key, and region. For a compatible service, also enter its endpoint URL. Compatible services use path-style bucket addressing. Temporary credentials can include a session token.
   - **Appwrite:** endpoint including `/v1`, project ID, and a server API key.
3. Optionally enter a container name or bucket ID. Leave it blank to retrieve available buckets and choose one. Appwrite uses bucket IDs, which can differ from display names.
4. Click **Connect & save**. A connection is saved only after listing the selected bucket succeeds. Failed edits preserve the previously saved connection.

Appwrite connections accept self-signed certificates in this environment. The REST adapter uses `verify=False`, equivalent to the Appwrite SDK's `Client.set_self_signed(1)`, and suppresses `InsecureRequestWarning`. Azure and S3 retain certificate verification.

The local pane is on the left. **Cloud A** and **Cloud B** each hold multiple bucket tabs. Choose **Open in** before opening a connection to choose its group. Open the same saved connection again to browse another bucket. Each tab navigates independently. Double-click a folder to open it; use **Up**, enter a path, or press **F5** to refresh. The local address field accepts native filesystem paths, including Windows drive letters/UNC paths on Windows and `/` paths on macOS/Linux, plus `~` and environment variables.

**LOCAL COMPUTER** also supports multiple tabs. Click **Local tab…** or press **Ctrl+T** (**Command+T** on macOS) to choose another folder. Each tab keeps its own folder, selection, and listing; its label follows the folder you browse. Use **×** to close a tab (the last local tab stays open). All local tabs are available as **Copy to…** destinations and support drag-and-drop. The active local folder is remembered for the next launch. The three tab groups have equal widths at startup and remain equal as the window resizes, with matching tab-strip and browser alignment.

## Transfer and manage files

- Select files or folders and use **Copy to…** to select any open pane and its current folder. Buttons also work on touchscreens.
- Drag files or folders from your system file manager or the local pane into a connected bucket. Dropping onto a folder row targets that folder; dropping on open list space targets the current folder.
- Drag cloud files between visible bucket lists, into the local pane, or out to your file manager. Cloud drag-out first downloads a staging copy: keep the mouse button held while the status bar says it is preparing. For large cloud files, **Copy to…** gives better queue visibility. Staged files are retained until BB exits so the file manager can read them.
- Use **Rename** / **F2** for one selected file; use **Delete** / **Delete key** for selected files. Deletion requires confirmation and is permanent, subject to the provider's versioning/retention policies. Folder rename and folder deletion are not supported.
- Transfers are copies. Existing destination files cause a failed queue item; BB does not silently overwrite or delete source files. An error partway through a folder copy can leave earlier files successfully copied. Review the destination before retrying.
- The queue runs serially and continues after a failed job. Double-click a row or select **Details** for errors. **Cancel selected** stops at the next chunk/request boundary; it cannot undo a completed provider request. Wait for queued/running work to finish or cancel before closing.

Azure and S3 folders are virtual key prefixes. Appwrite presents a flat list of files with stable IDs: upload individual files, or open a source folder and select its files. Folder transfers into Appwrite are rejected. Duplicate Appwrite names are checked before upload/rename, though its API cannot enforce unique names against concurrent clients.

Cloud-to-cloud copies and Azure/S3 renames stage file contents in the system temporary directory. Allow enough free disk space for the largest individual file. Azure/S3 rename uploads the new file before conditionally deleting the old one. If deletion fails, both may remain. These operations preserve bytes; provider-specific ACLs, tags, metadata, version history, storage tiers, and HTTP content settings are not copied. Appwrite rename updates the name while retaining the file ID.

S3 uses multipart uploads for files at least 64 MiB, with best-effort abort cleanup on cancellation/failure. Compatible servers must support conditional creation (`If-None-Match`) and conditional deletion (`If-Match`); unsupported operations fail instead of falling back to an unsafe overwrite/delete. Azure deletes and downloads use the listed ETag. Appwrite has no equivalent conditional-delete check. Cloud credentials need permissions for the operations you use; a fixed bucket can avoid account-wide bucket-list permission. Appwrite keys typically need `buckets.read`, `files.read`, and `files.write`. Bucket rules and file size limits still apply.

## Preferences and XML

**Connections & preferences** includes dark/light appearance, larger touch targets, and an optional startup bucket. Select a saved connection and **Startup default**. If it has no fixed bucket, enter the bucket/container ID to open. **Clear startup default** disables automatic connection. Auto-connection only browses; it never starts transfers.

Settings are stored as UTF-8 XML at:

- Windows: `%APPDATA%\Bucket Browser\connections.xml` (the existing location).
- macOS: `~/Library/Application Support/Bucket Browser/connections.xml`.
- Linux: `$XDG_CONFIG_HOME/bucket-browser/connections.xml`, defaulting to `~/.config/bucket-browser/connections.xml`. Relative XDG paths are ignored.

Windows credentials retain DPAPI protection for the current Windows account, including compatibility with existing XML. On macOS/Linux, credentials use authenticated Fernet encryption with a key stored in macOS Keychain or Linux Secret Service, outside the XML. Temporary XML files have private permissions on Unix and replace settings atomically. Connection labels, bucket names, and non-secret settings remain readable. A failed settings load preserves the original XML; use **Import XML** to recover from a valid export.

The keychain key is shared by this user's BB settings/exports. Keep it when restoring encrypted XML backups; losing it makes their credentials unrecoverable. Moving encrypted XML alone to a different account/computer does not move credentials. Export portable XML on the original account/computer, import it on the destination, and BB will protect the imported secrets using that system's local storage.

**Export XML** offers:

- Settings only, omitting credentials (enter them again after import).
- Credentials encrypted for this OS account (requires the original DPAPI account or BB keychain key to read).
- Portable XML containing plaintext credentials, explicitly selected for moving to another account/computer. Store this export securely.

**Import XML** replaces saved connections and preferences after confirmation. Open tabs retain their current connections. The imported startup bucket is used on the next launch. XML must use the BB version 1 schema and UTF-8 encoding; DTDs/entities and files larger than 5 MiB are rejected. A settings example is provided in `connections.example.xml` with no real credentials.

## Development and validation

```powershell
python -m compileall -q bb.py bucket_browser
```

This command checks Python syntax. Exercise GUI changes in a graphical desktop session and validate provider changes with appropriate test data.

Native macOS/Linux startup, keychain prompts, GTK/Cocoa layouts/themes, file-manager drag-and-drop (Finder, Explorer, or a Linux file manager), live provider authentication/permissions, and physical touchscreen behavior require testing on each target OS. Wayland or desktop policies may limit cross-application dragging; **Copy to…** remains available. Local downloads retain conservative Windows-compatible filename checks on all platforms to reject unsafe paths; directory symlinks/junctions are not followed. Unix file publication/rename requires filesystem hard-link support and fails safely when unavailable.

Provider implementations live in `bucket_browser/providers.py`; the provider contract and registry isolate storage operations from the UI and transfer engine. Add a provider adapter, credential field definitions in `models.py`, and corresponding tests to extend support.

Implementation references: [wxPython drag and drop](https://docs.wxpython.org/dnd_overview.html), [Azure Python BlobClient](https://learn.microsoft.com/en-us/python/api/azure-storage-blob/azure.storage.blob.blobclient), [Boto3 S3](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3.html), [Appwrite Storage API](https://appwrite.io/docs/references/cloud/server-python/storage).
