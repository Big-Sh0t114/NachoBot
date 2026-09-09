from __future__ import annotations

import argparse
import shutil
import sys
import os
from pathlib import Path


FILE_SOURCES = {
    "template/template.env": "docker-config/mmc/.env",
    "template/bot_config_template.toml": "docker-config/mmc/bot_config.toml",
    "template/model_config_template.toml": "docker-config/mmc/model_config.toml",
    "template/topics_config_template.toml": "docker-config/mmc/topics_config.toml",
    "template/mcp_config_template.toml": "docker-config/mmc/mcp_config.toml",
}

GENERATED_FILES = {
    "data/NachoBot/nachobot_statistics.html": (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>NachoBot Statistics</title></head><body>"
        "<p>NachoBot statistics have not been generated yet.</p></body></html>\n"
    ),
}

MULTIMODAL_CONFIG_SOURCES = {
    "../NachoBot-Multimodal-Adapter/template_configs/base_template.toml": (
        "../NachoBot-Multimodal-Adapter/configs/base.toml"
    ),
    "../NachoBot-Multimodal-Adapter/template_configs/perception_template.toml": (
        "../NachoBot-Multimodal-Adapter/configs/perception.toml"
    ),
    "../NachoBot-Multimodal-Adapter/template_configs/gpt-sovits_template.toml": (
        "../NachoBot-Multimodal-Adapter/configs/gpt-sovits.toml"
    ),
    "../NachoBot-Multimodal-Adapter/template_configs/vox_template.toml": (
        "../NachoBot-Multimodal-Adapter/configs/vox.toml"
    ),
}

DIRECTORY_SOURCES = (
    "data/adapters",
    "data/NachoBot/logs",
    "docker-config/napcat",
    "data/qq",
)


def _assert_file_or_missing(path: Path) -> None:
    if path.exists() and not path.is_file():
        raise RuntimeError(
            f"{path} must be a file, but another filesystem object already exists. "
            "Move it aside manually before bootstrapping."
        )


def bootstrap(project_root: Path) -> list[Path]:
    """Create missing Compose bind sources without replacing user configuration."""
    created: list[Path] = []
    for relative_path in DIRECTORY_SOURCES:
        destination = project_root / relative_path
        if destination.exists() and not destination.is_dir():
            raise RuntimeError(f"{destination} must be a directory")
        if not destination.exists():
            destination.mkdir(parents=True)
            created.append(destination)

    for source_relative, destination_relative in {
        **FILE_SOURCES,
        **MULTIMODAL_CONFIG_SOURCES,
    }.items():
        source = project_root / source_relative
        destination = project_root / destination_relative
        if not source.is_file():
            raise RuntimeError(f"bootstrap source is missing: {source}")
        _assert_file_or_missing(destination)
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            created.append(destination)
    for destination_relative, contents in GENERATED_FILES.items():
        destination = project_root / destination_relative
        _assert_file_or_missing(destination)
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(contents, encoding="utf-8", newline="\n")
            created.append(destination)
    return created


def validate(project_root: Path, *, include_legacy_adapters: bool = False) -> None:
    missing: list[str] = []
    invalid: list[str] = []
    for destination_relative in (
        *FILE_SOURCES.values(),
        *MULTIMODAL_CONFIG_SOURCES.values(),
        *GENERATED_FILES,
    ):
        destination = project_root / destination_relative
        if not destination.exists():
            missing.append(destination_relative)
        elif not destination.is_file():
            invalid.append(f"{destination_relative} is not a file")
    for directory_relative in DIRECTORY_SOURCES:
        directory = project_root / directory_relative
        if not directory.exists():
            missing.append(directory_relative)
        elif not directory.is_dir():
            invalid.append(f"{directory_relative} is not a directory")
    if include_legacy_adapters:
        legacy_config = project_root / "docker-config/adapters/config.toml"
        if not legacy_config.is_file():
            missing.append("docker-config/adapters/config.toml")

    if missing or invalid:
        details = [*(f"missing: {item}" for item in missing), *invalid]
        raise RuntimeError(
            "Compose bind-source preflight failed:\n- "
            + "\n- ".join(details)
            + "\nRun: python scripts/bootstrap_compose.py"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safely initialize NachoBot Compose bind sources."
    )
    parser.add_argument("--check", action="store_true", help="validate only")
    parser.add_argument(
        "--legacy-adapters",
        action="store_true",
        help="also require docker-config/adapters/config.toml",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    project_root = args.root.resolve()

    try:
        if not args.check:
            created = bootstrap(project_root)
            for path in created:
                print(f"created: {os.path.relpath(path, project_root)}")
            if not created:
                print("Compose bind sources already initialized; no files changed.")
        validate(project_root, include_legacy_adapters=args.legacy_adapters)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print("Compose bind-source preflight passed.")
    if args.legacy_adapters:
        print("Legacy adapters profile configuration is present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
