# Build information

Build date: 2026-09-17
Adapter version: 0.2.0

## Compatibility baseline

- NachoBot repository: `Big-Sh0t114/NachoBot`
- Branch: `dev`
- Dev protocol commit inspected: `fab9fdd8b371008e07bd736e05888cbbaf958818` (`feat(message):regist system_message`)
- Core contract: `NachoBot/ncnk_message/system_event.py` (`SYSTEM_EVENT_VERSION = 1`)
- Behavior reference: `NachoBot-Napcat-Adapter/src/recv_handler/notice_handler.py` on the same `dev` branch.
- SnowLuma API baseline: SnowLuma 1.14.x OneBot WebSocket/action protocol.

## v0.2.0 protocol migration

1. Removed the old `notice -> synthetic ordinary user message` path.
2. Added canonical `system_event` envelopes using Core's shared `build_system_event()`.
3. Structured system events are senderless (`user_info=None`).
4. Added `system_event_route` for private events so Core can route without treating the actor as a message sender.
5. Added structured QQ poke, group-ban/lift-ban and generic group notice conversion.
6. Added optional fast-poke action result metadata compatible with the dev NapCat adapter.
7. Added `ban_qq_bot` admission configuration.
8. Retained v0.1.1 `MESSAGE_LIKE -> set_msg_emoji_like` compatibility fix.

## Local validation

- `python -m compileall` passed for the complete adapter tree.
- TOML parsing passed for `config.toml`, `template_config.toml` and `pyproject.toml`.
- `pytest` configuration tests passed (2/2).
- Fake SnowLuma WebSocket action/echo + event smoke test passed.
- Combined bridge smoke test passed for ordinary inbound, structured events, outbound `send_group_msg`, and `message_id_echo`.
- Structured-event smoke test passed for:
  - group `qq.poke`
  - private `qq.poke` + `system_event_route`
  - `qq.group_ban`
  - generic `qq.group_admin.set`
- Migration invariants verified: no synthetic-poke ordinary-message path remains; system events use `user_info=None`.

No live QQ/SnowLuma login was available in the build environment; live end-to-end behavior still requires deployment testing.
