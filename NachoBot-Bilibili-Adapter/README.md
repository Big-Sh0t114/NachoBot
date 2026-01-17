# NachoBot-Bilibili-Adapter

Bridge NachoBot to Bilibili live danmu and comment replies.

## Features
- Multi-room live danmu receive/send (reply danmu supported).
- Comment reply send (all comment types).
- Reply notification polling and forwarding.
- Private message polling and send (text only).
- QR login helper to update cookies in `config.toml`.

## Setup
1. Install deps:
   - `pip install -r requirements.txt`
2. Edit `config.toml` with `SESSDATA`, `bili_jct`, `buvid3`, and room IDs.
3. Run:
   - `python main.py`

## Compatibility
- Set `compat.disable_video_sender_plugin = true` to avoid triggering the Bilibili video sender plugin on the core.
- Set `compat.disable_command_trigger = true` to prevent command triggers for bilibili messages.

## Live reply prompt
- Set `live.reply_prompt` to override the live room reply prompt (uses `replyer_prompt` template name).
- Set `live.planner_prompt` to override the planner prompt shown in core logs (uses `planner_prompt` template name).
- Use a TOML multi-line string (`"""..."""`) if you need line breaks.
- `live.ws_proxy` controls the WebSocket proxy: `auto` (env), `none` (disable), or an explicit proxy URL.
- `live.open_timeout` controls the WebSocket open timeout (seconds), useful to avoid long hangs on blocked networks.
- `live.max_hosts` limits how many hosts from `host_list` to try (0 = unlimited).
- `live.max_attempts` limits total connect attempts per run (0 = unlimited).

## Private messages
- `private_message.sessions` lets you pin specific talker IDs.
- Set `private_message.auto_sessions = true` to auto-poll all recent sessions (recommended for "any user" DMs).
- `private_message.auto_session_types` defaults to `4` (all sessions).
- `private_message.auto_session_refresh_seconds` controls how often the session list refreshes.

## QR login
- `python qr_login.py`
- This writes `SESSDATA`, `bili_jct`, and `DedeUserID` into `config.toml`.

## Command usage
See `command_args.md`.
