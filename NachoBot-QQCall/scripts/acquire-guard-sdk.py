"""Acquire the pinned SDK into workspace storage without installing system software."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from urllib.request import urlopen
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> tuple[int, bytes]:
    total = 0
    hashed = hashlib.sha512()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            total += len(chunk)
            hashed.update(chunk)
    return total, hashed.digest()


def acquire(package: dict, version: str, output: Path) -> dict:
    package_id = package["id"]
    expected = base64.b64decode(package["sha512"], validate=True)
    archive = output / f"{package_id}.{version}.nupkg"
    if not archive.exists() or digest(archive) != (package["size"], expected):
        url = f"https://api.nuget.org/v3-flatcontainer/{package_id}/{version}/{package_id}.{version}.nupkg"
        partial = archive.with_suffix(".download")
        with urlopen(url, timeout=30) as response, partial.open("wb") as target:
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > package["size"]:
                    raise ValueError("sdk_package_too_large")
                target.write(chunk)
        if digest(partial) != (package["size"], expected):
            raise ValueError("sdk_package_hash_mismatch")
        partial.replace(archive)
    destination = output / package_id
    extracted = 0
    with ZipFile(archive) as zipped:
        for entry in zipped.infolist():
            relative = PurePosixPath(entry.filename.replace("\\", "/"))
            if relative.is_absolute() or ".." in relative.parts or any(":" in part for part in relative.parts):
                raise ValueError("sdk_archive_path_invalid")
            name = str(relative)
            if entry.is_dir() or not (name.startswith(("c/Include/", "c/um/x64/", "c/ucrt/x64/"))
                                      or "license" in name.lower()):
                continue
            target = (destination / Path(*relative.parts)).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise ValueError("sdk_archive_path_invalid")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zipped.open(entry) as source, target.open("wb") as sink:
                shutil.copyfileobj(source, sink, length=1024 * 1024)
            extracted += 1
    return {"id": package_id, "size": package["size"], "sha512": package["sha512"], "extractedFiles": extracted}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, default=ROOT / ".runtime/toolchain/sdk")
    args = parser.parse_args()
    release = json.loads((ROOT / "native/sdk-release.json").read_text(encoding="utf-8"))
    output = args.output_root.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        packages = list(pool.map(lambda package: acquire(package, release["package_version"], output), release["packages"]))
    include = output / "microsoft.windows.sdk.cpp/c/Include" / release["sdk_version"]
    libraries = output / "microsoft.windows.sdk.cpp.x64/c"
    if not (include / "um/mmdeviceapi.h").is_file() or not (libraries / "um/x64/Mmdevapi.lib").is_file():
        raise ValueError("sdk_layout_invalid")
    report = {"schema": 1, "sdkVersion": release["sdk_version"], "packages": packages,
              "includeRoot": str(include), "libraryRoot": str(libraries)}
    (output / "verified-sdk.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
