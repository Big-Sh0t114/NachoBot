"""Standalone loopback API entrypoint for the UniversalVC audio service."""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from universalvc_service.runtime_compat import preload_onnxruntime
from universalvc_service.service_config import ServiceConfigError, load_service_settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the UniversalVC loopback audio service")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parent / "service.toml",
        help="service-owned TOML configuration (default: service.toml)",
    )
    return parser


async def _serve(settings) -> None:
    # Preserve the Windows DLL ordering used by the legacy entrypoint before
    # importing any service module that may load sherpa-onnx.
    preload_onnxruntime()
    from aiohttp import web

    from universalvc_service.api import create_app

    app = create_app(settings)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, settings.bind_host, settings.port)
    try:
        await site.start()
        logging.getLogger("universalvc.service").info(
            "UniversalVC service ready at http://%s:%d", settings.bind_host, settings.port
        )
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = load_service_settings(args.config)
    except ServiceConfigError as exc:
        _parser().error(str(exc))
    preload_onnxruntime()
    try:
        asyncio.run(_serve(settings))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
