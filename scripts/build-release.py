"""Check project versions and package only source-controlled project files."""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INCLUDE_ROOTS = ("backend/", "docs/", "packs/", "scripts/", "templates/", "web/")
INCLUDE_FILES = {"README.md", "CHANGELOG.md", "start.ps1", "requirements.txt", "pyproject.toml"}
EXCLUDE_ROOTS = ("web/dist/", "web/node_modules/")


def version():
    backend = (ROOT / "backend/app/__init__.py").read_text(encoding="utf-8")
    match = re.fullmatch(r'__version__ = "(\d+\.\d+\.\d+)"\s*', backend)
    if not match:
        raise ValueError("backend version must be a single X.Y.Z assignment")
    current = match.group(1)
    frontend = json.loads((ROOT / "web/package.json").read_text(encoding="utf-8"))
    if frontend["version"] != current:
        raise ValueError(f"frontend version {frontend['version']} != backend {current}")
    return current


def tracked_files():
    names = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).split(b"\0")
    files = []
    for raw in names:
        if not raw:
            continue
        name = raw.decode("utf-8")
        if (name in INCLUDE_FILES or name.startswith(INCLUDE_ROOTS)) and not name.startswith(
            EXCLUDE_ROOTS
        ):
            path = ROOT / name
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(ROOT):
                raise ValueError(f"unsafe or missing tracked file: {name}")
            files.append(name)
    return files


def archive(current):
    files = tracked_files()
    if "backend/app/api.py" not in files or "docs/imported-sources.json" not in files:
        raise ValueError("required project files are not tracked by Git")
    assets = sorted((ROOT / "web/dist").rglob("*"))
    assets = [path for path in assets if path.is_file() and not path.is_symlink()]
    if not (ROOT / "web/dist/index.html").is_file() or not any(
        path.parent.name == "assets" for path in assets
    ):
        raise ValueError("build the web UI with pnpm --dir web build first")
    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    name = f"subscription-workbench-v{current}.zip"
    target = output / name
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for filename in sorted(files):
            bundle.write(ROOT / filename, filename)
        for path in assets:
            bundle.write(path, path.relative_to(ROOT).as_posix())
    checksum = hashlib.sha256(target.read_bytes()).hexdigest()
    (output / f"{name}.sha256").write_text(f"{checksum}  {name}\n", encoding="ascii")
    print(f"{target} sha256={checksum}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="only validate version consistency")
    parser.add_argument("--tag", help="require a matching vX.Y.Z Git tag")
    args = parser.parse_args()
    try:
        current = version()
        if args.tag and args.tag != f"v{current}":
            raise ValueError(f"tag {args.tag} != v{current}")
        print(f"Project version: {current}")
        if not args.check:
            archive(current)
    except (OSError, ValueError, subprocess.CalledProcessError, KeyError) as exc:
        print(f"Release check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
