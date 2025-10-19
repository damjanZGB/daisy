#!/usr/bin/env python3
"""
Build deployment zip for the time-tools Lambda.

Usage:
    python aws/time_tools_lambda/package.py
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST_DIR = ROOT / "dist"
ZIP_NAME = "time_tools_lambda.zip"
FILES_TO_INCLUDE = ["lambda_function.py", "time_tools.py"]


def create_zip(python_bin: str) -> Path:
    DIST_DIR.mkdir(exist_ok=True)
    zip_path = DIST_DIR / ZIP_NAME
    if zip_path.exists():
        zip_path.unlink()

    with tempfile.TemporaryDirectory() as build_dir:
        build_path = Path(build_dir)
        for file_name in FILES_TO_INCLUDE:
            shutil.copy(ROOT / file_name, build_path / file_name)

        requirements = ROOT / "requirements.txt"
        if requirements.exists():
            cmd = [
                python_bin,
                "-m",
                "pip",
                "install",
                "-r",
                str(requirements),
                "-t",
                str(build_path),
            ]
            subprocess.check_call(cmd)

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in build_path.rglob("*"):
                if path.is_dir():
                    continue
                arcname = path.relative_to(build_path)
                zf.write(path, arcname)

    return zip_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Package time tools Lambda.")
    parser.add_argument("--python", default=sys.executable, help="Python binary to use for pip installs.")
    args = parser.parse_args()

    zip_path = create_zip(args.python)
    print(f"Created {zip_path}")


if __name__ == "__main__":
    main()
