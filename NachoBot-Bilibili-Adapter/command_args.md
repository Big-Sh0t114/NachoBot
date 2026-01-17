# Command Arguments
```python
Seg.type = "command"
```

## Send/Reply Comment
```python
Seg.data = {
    "name": "BILI_COMMENT_REPLY",
    "args": {
        "type": "comment_type",
        "oid": "comment_target_id",
        "message": "plain_text_message",
        "root": "root_rpid_optional",
        "parent": "parent_rpid_optional"
    },
}
```

- `type`/`oid` are required.
- `root`/`parent` are optional. Omit them for top-level comments.
- `message` must be plain text. Emoji and non-text segments are ignored.

## Reply Danmu (Optional)
```python
Seg.data = {
    "name": "BILI_LIVE_REPLY",
    "args": {
        "room_id": "live_room_id",
        "message": "plain_text_message",
        "reply_mid": "optional_reply_user_mid",
        "reply_dmid": "optional_reply_danmu_id"
    },
}
```

- Use this command to force a reply danmu if you already know the target user/dmid.
- If `reply_mid`/`reply_dmid` are absent, the adapter sends a normal danmu.

## Send Private Message
```python
Seg.data = {
    "name": "BILI_PRIVATE_SEND",
    "args": {
        "talker_id": "target_user_mid_or_group_id",
        "session_type": "1_for_user_or_2_for_fans_group",
        "message": "plain_text_message"
    },
}
```

- `message` must be plain text. Emoji and non-text segments are ignored.
