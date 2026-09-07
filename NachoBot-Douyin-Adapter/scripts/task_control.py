from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import load_config  # noqa: E402


API_BASE = "https://webcast.bytedance.com/api/live_data/task"


async def request_task(action: str, message_type: str) -> int:
    config = load_config(ROOT / "config.toml")
    if not config.douyin.app_id or not config.douyin.room_id or not config.douyin.access_token:
        print("请先在 config.toml 填写 app_id、room_id 和 douyin.tasks.access_token")
        return 2
    url = f"{API_BASE}/{action}"
    payload = {
        "appid": config.douyin.app_id,
        "roomid": config.douyin.room_id,
        "msg_type": message_type,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            json=payload,
            headers={"access-token": config.douyin.access_token},
        ) as response:
            text = await response.text()
            print(f"HTTP {response.status}: {text}")
            if response.status != 200:
                return 1
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                return 1
            return 0 if int(payload.get("err_no", -1)) == 0 else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="管理抖音直播数据推送任务")
    parser.add_argument("action", choices=("start", "stop"))
    parser.add_argument(
        "message_type", choices=("live_comment", "live_gift", "live_like", "live_fansclub")
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(request_task(args.action, args.message_type)))


if __name__ == "__main__":
    main()
