# Repository Guidelines

## Project Structure & Module Organization

`bb.py` starts the wxPython desktop application. The `bucket_browser/` package contains the interface (`gui.py`), storage adapters (`providers.py`), transfer queue (`transfers.py`), data models (`models.py`), and XML/credential handling (`config.py`). Keep `bucket.png` beside `bb.py`; `connections.example.xml` is a credential-free configuration example. Platform launchers are `start-bb.cmd` and `start-bb.sh`.

## Build, Test, and Development Commands

Use Python 3.12 or newer. On Windows, from the repository root:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe bb.py
python -m compileall -q bb.py bucket_browser
```

These commands create an environment, install dependencies, launch the GUI, and check Python syntax. `start-bb.cmd` uses the local environment when available; on macOS/Linux use `sh start-bb.sh`.

## Coding Style & Architecture

Follow the existing Python style: four-space indentation, `snake_case` for functions and modules, and `PascalCase` for classes. Keep provider operations behind the contract and registry in `providers.py`; add credential fields in `models.py` when introducing an adapter. Keep UI, transfer, and settings responsibilities in their existing modules. No formatter or linter configuration is present.

## Testing Guidelines

Use `unittest`, fake provider clients, and temporary files for new tests; name test modules `test_*.py`. Exercise GUI changes in a graphical session and report any platform or live-provider checks not run.

## Changes & Security

Git history is unavailable in this checkout, so no established commit convention can be inferred. Write concise, descriptive commit subjects. Pull requests should explain behavior changes and validation, link relevant issues, and include screenshots for visible UI changes. Never commit credentials or portable plaintext XML exports. Preserve secure credential storage and the existing safeguards against overwrites and unsafe paths.