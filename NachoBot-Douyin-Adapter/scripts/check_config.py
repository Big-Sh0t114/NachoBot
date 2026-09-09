from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import load_config  # noqa: E402


def mark(value: str) -> str:
    return "已填写" if value else "缺失"


def main() -> int:
    parser = argparse.ArgumentParser(description="检查抖音适配器凭据是否已填写（不显示密钥）")
    parser.add_argument("--path", type=Path, default=ROOT / "config.toml")
    args = parser.parse_args()

    try:
        config = load_config(args.path)
    except Exception as exc:  # pragma: no cover - command-line diagnostic
        print(f"配置读取失败：{exc}")
        return 1

    checks = {
        "douyin.app_id": config.douyin.app_id,
        "douyin.room_id": config.douyin.room_id,
        "douyin.callback_secret": config.douyin.callback_secret,
        "douyin.tasks.access_token": config.douyin.access_token,
    }
    print(f"配置文件：{args.path.resolve()}")
    for name, value in checks.items():
        print(f"{name}: {mark(value)}")
    print(f"直播回调地址（部署后）：{config.server.callback_path}")
    print(f"私信功能：{'已启用' if config.im.enabled else '未启用'}")
    if config.douyin.allow_unsigned_local:
        print("警告：allow_unsigned_local=true，仅可用于本机测试，生产前必须关闭")

    required_ready = all(checks.values())
    print(f"直播任务凭据状态：{'可继续启动任务' if required_ready else '等待控制台凭据'}")
    return 0 if required_ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
