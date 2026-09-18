from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from loguru import logger
import uvicorn

from adapter import DouyinAdapter
from config import AppConfig, load_config
from signature import verify_callback_signature, verify_webhook_signature


def create_app(config: AppConfig) -> FastAPI:
    adapter = DouyinAdapter(config)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await adapter.start()
        consumer = asyncio.create_task(adapter.consume_forever(), name="douyin-event-consumer")
        try:
            yield
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            await adapter.stop()

    app = FastAPI(title="NachoBot Douyin Adapter", version="0.2.0", lifespan=lifespan)
    app.state.adapter = adapter

    @app.get("/health")
    async def health() -> dict:
        return adapter.status()

    @app.post(config.server.callback_path)
    async def live_callback(request: Request) -> dict:
        body = await request.body()
        client_host = request.client.host if request.client else ""
        local_unsigned = config.douyin.allow_unsigned_local and client_host in {
            "127.0.0.1",
            "::1",
            "testclient",
        }
        if not local_unsigned:
            valid, reason = verify_callback_signature(
                request.headers,
                body,
                config.douyin.callback_secret,
                tolerance_seconds=config.douyin.timestamp_tolerance_seconds,
            )
            if not valid:
                logger.warning("Rejected Douyin callback: {}", reason)
                raise HTTPException(status_code=401, detail=reason)

        try:
            payload = json.loads(body.decode("utf-8"))
            if isinstance(payload, str):
                payload = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail="invalid JSON body") from exc

        message_type = str(request.headers.get("x-msg-type") or "").strip()
        room_id = str(request.headers.get("x-roomid") or config.douyin.room_id).strip()
        if message_type not in {"live_comment", "live_gift", "live_like", "live_fansclub"}:
            raise HTTPException(status_code=400, detail="unsupported x-msg-type")
        accepted = adapter.enqueue_payload(message_type, room_id, payload)
        return {"ok": True, "accepted": accepted}

    @app.post(config.im.callback_path)
    async def im_webhook(request: Request) -> dict:
        body = await request.body()
        client_host = request.client.host if request.client else ""
        local_unsigned = config.im.allow_unsigned_local and client_host in {
            "127.0.0.1",
            "::1",
            "testclient",
        }
        signature = request.headers.get("x-douyin-signature", "")
        if not local_unsigned and not verify_webhook_signature(
            body, config.im.webhook_secret, signature
        ):
            logger.warning("Rejected Douyin IM Webhook: signature mismatch")
            raise HTTPException(status_code=401, detail="signature mismatch")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail="invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="JSON object required")

        event = str(payload.get("event") or "")
        if event == "verify_webhook":
            content = payload.get("content")
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except json.JSONDecodeError as exc:
                    raise HTTPException(status_code=400, detail="invalid challenge") from exc
            if not isinstance(content, dict) or "challenge" not in content:
                raise HTTPException(status_code=400, detail="missing challenge")
            return {"challenge": content["challenge"]}
        if event == "im_send_msg":
            return {"ok": True, "accepted": 0}
        if event != "im_receive_msg":
            return {"ok": True, "accepted": 0}
        if not config.im.enabled:
            raise HTTPException(status_code=503, detail="Douyin IM is disabled")
        if config.im.client_key and payload.get("client_key") != config.im.client_key:
            raise HTTPException(status_code=403, detail="client_key mismatch")
        accepted = adapter.enqueue_private_payload(payload)
        return {"ok": True, "accepted": accepted}

    return app


def main() -> None:
    config_path = Path(__file__).resolve().parent / "config.toml"
    config = load_config(config_path)
    logger.remove()
    logger.add(sys.stderr, level=config.server.log_level.upper())
    logger.info(
        "Starting Douyin adapter at http://{}:{} (live: {}, im: {})",
        config.server.host,
        config.server.port,
        config.server.callback_path,
        config.im.callback_path,
    )
    uvicorn.run(
        create_app(config),
        host=config.server.host,
        port=config.server.port,
        log_level=config.server.log_level.lower(),
    )


if __name__ == "__main__":
    main()
