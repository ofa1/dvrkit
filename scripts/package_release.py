#!/usr/bin/env python3
"""Validate version metadata and package the tracked integration at HEAD."""

import argparse
import json
from pathlib import Path, PurePosixPath
import subprocess
import tomllib
import zipfile


ROOT = "custom_components/dvrkit/"


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args])


def source(path: str) -> bytes:
    return git("show", f"HEAD:{path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", help="release tag, including the leading v")
    parser.add_argument("--output", default="dist/dvrkit.zip")
    args = parser.parse_args()

    manifest = json.loads(source(ROOT + "manifest.json"))
    version = manifest["version"]
    project = tomllib.loads(source("pyproject.toml").decode())
    lock = tomllib.loads(source("uv.lock").decode())
    locked = [p["version"] for p in lock["package"] if p["name"] == "dvrkit"]
    if project["project"]["version"] != version or locked != [version]:
        raise SystemExit("manifest, pyproject.toml and uv.lock versions must match")
    if args.tag is not None and args.tag != f"v{version}":
        raise SystemExit(f"tag must match manifest version v{version}")

    entries = git("ls-tree", "-r", "-z", "HEAD", "--", ROOT).split(b"\0")
    files = {}
    for entry in filter(None, entries):
        metadata, raw_path = entry.split(b"\t", 1)
        mode, kind, _ = metadata.decode().split()
        path = raw_path.decode()
        relative = PurePosixPath(path.removeprefix(ROOT))
        allowed = (
            len(relative.parts) == 1 and relative.suffix in {".py", ".json"}
            or len(relative.parts) == 2 and relative.parts[0] == "translations" and relative.suffix == ".json"
            or len(relative.parts) == 2 and relative.parts[0] == "brand" and relative.suffix == ".png"
        )
        if not path.startswith(ROOT) or mode != "100644" or kind != "blob" or not allowed:
            raise SystemExit(f"unexpected integration source: {path}")
        files[path] = source(path)

    required = {ROOT + name for name in ("__init__.py", "manifest.json", "strings.json", "translations/en.json", "brand/icon.png")}
    if not required <= files.keys():
        raise SystemExit(f"missing integration sources: {sorted(required - files.keys())}")
    if manifest["domain"] != "dvrkit":
        raise SystemExit("manifest domain must be dvrkit")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path, data in sorted(files.items()):
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data, compresslevel=9)
    print(f"Packaged {len(files)} tracked files for DVRKit {version}: {output}")


if __name__ == "__main__":
    main()
