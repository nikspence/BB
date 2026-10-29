"""Bucket Browser entry point. Run with python bb.py."""
from pathlib import Path

from bucket_browser.gui import main

if __name__ == "__main__":
    main(icon_path=Path(__file__).resolve().with_name("bucket.png"))
