import asyncio
import base64
import json
import tempfile
import unittest
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from pathlib import Path

import discord

from adapter import DiscordAdapter
from config import (
    AdapterConfig,
    ChatConfig,
    DiscordConfig,
    NachoBotConfig,
    PromptsConfig,
    VoiceConfig,
    VisualImageConfig,
)
from discord_client import DiscordTransportCog, VoiceCog
from identity_map import IdentityMap
from slash_interactions import SlashInteractionRegistry
from voice_codec import pcm16_to_wav_base64


USER_ID = "123456789012345678"
OTHER_USER_ID = "223456789012345678"
BOT_ID = "323456789012345678"
CHANNEL_ID = "423456789012345678"
OTHER_CHANNEL_ID = "523456789012345678"
GUILD_ID = "623456789012345678"
MESSAGE_ID = "723456789012345678"


class FakeChannel:
    _DEFAULT_RESULT = object()
    __slots__ = ("id", "guild", "recipient", "sent", "send_result", "name")

    def __init__(
        self,
        channel_id,
        *,
        guild=None,
        recipient=None,
        send_result=_DEFAULT_RESULT,
    ):
        self.id = int(channel_id)
        self.guild = guild
        self.recipient = recipient
        self.sent = []
        self.send_result = send_result
        self.name = ""

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))
        if self.send_result is not self._DEFAULT_RESULT:
            return self.send_result
        return SimpleNamespace(id=823456789012345678 + len(self.sent))


def _identity_map(*, include_private=True):
    return IdentityMap.from_mapping(
        {
            "schema_version": 1,
            "user_native_to_logical": {
                USER_ID: "legacy-user-41",
                OTHER_USER_ID: "legacy-user-42",
            },
            "channel_native_to_logical": {
                CHANNEL_ID: "legacy-room-8",
                OTHER_CHANNEL_ID: "legacy-dm-channel-9",
            },
            "private_channels": (
                [{"user_id": USER_ID, "channel_id": OTHER_CHANNEL_ID}]
                if include_private
                else []
            ),
            "bot_self_ids": [],
        }
    )


def _config(*, chat=None, voice=None):
    return AdapterConfig(
        discord=DiscordConfig(),
        nachobot=NachoBotConfig(),
        voice=voice or VoiceConfig(enabled=True),
        chat=chat or ChatConfig(),
        visual_image=VisualImageConfig(
            temperature=0.25,
            max_tokens=321,
            extra_params={"enable_thinking": False, "test_flag": "native"},
        ),
        prompts=PromptsConfig(
            planner_prompt="legacy planner {personality}",
            replyer_prompt="legacy voice reply {personality}",
            variables={"personality": "fixture"},
        ),
        identity_map=_identity_map(),
        identity_required=True,
        media_proxy_url="",
        max_attachment_bytes=1024 * 1024,
    )


def _adapter(*, config=None, channels=None, users=None):
    adapter = DiscordAdapter.__new__(DiscordAdapter)
    adapter.config = config or _config()
    adapter.identity_map = adapter.config.identity_map
    adapter.logger = Mock()
    adapter.router = SimpleNamespace(
        send_message=AsyncMock(return_value=True),
        send_custom_message=AsyncMock(),
        stop=AsyncMock(),
    )
    adapter.media = SimpleNamespace(download=AsyncMock(return_value=None))
    adapter._stopping = False
    adapter._ingress_tasks = set()
    adapter._outbound_tasks = set()
    adapter._slash_interactions = SlashInteractionRegistry()
    adapter.bot = SimpleNamespace(
        user=SimpleNamespace(id=int(BOT_ID)),
        get_channel=Mock(side_effect=lambda key: (channels or {}).get(str(key))),
        fetch_channel=AsyncMock(side_effect=lambda key: (channels or {}).get(str(key))),
        get_user=Mock(side_effect=lambda key: (users or {}).get(str(key))),
        fetch_user=AsyncMock(side_effect=lambda key: (users or {}).get(str(key))),
        get_voice_session=Mock(return_value=None),
        own_temp_audio=Mock(),
        speak=AsyncMock(return_value=True),
        close=AsyncMock(),
        is_closed=Mock(return_value=False),
    )
    adapter.media.close = AsyncMock()
    return adapter


class FakeInteractionFollowup:
    def __init__(self, first_id=923456789012345678):
        self.sent = []
        self._next_id = first_id

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))
        result = SimpleNamespace(id=self._next_id)
        self._next_id += 1
        return result


class FakeSlashContext:
    def __init__(self, channel, *, author_id=USER_ID, guild=None, interaction_id=MESSAGE_ID):
        self.channel = channel
        self.guild = guild if guild is not None else channel.guild
        self.author = SimpleNamespace(
            id=int(author_id), name="林", display_name="林"
        )
        self.deferred = False
        self.followup = FakeInteractionFollowup()
        self.interaction = SimpleNamespace(
            id=int(interaction_id),
            response=SimpleNamespace(is_done=lambda: self.deferred),
        )

    async def defer(self, *, ephemeral=False):
        self.deferred = bool(ephemeral)


def _core_response_for_slash(request, segment, *, message_id="core-slash-result"):
    source_info = request.message_info
    return SimpleNamespace(
        message_info=SimpleNamespace(
            platform="discord",
            message_id=message_id,
            additional_config={
                "delivery_target": dict(
                    source_info.additional_config["delivery_target"]
                )
            },
            group_info=source_info.group_info,
            user_info=source_info.user_info,
        ),
        message_segment=segment,
    )


def _core_message(*, target="absent", platform="discord", group=None, user=None, segment=None):
    additional = {}
    if target != "absent":
        additional["delivery_target"] = target
    info = SimpleNamespace(
        platform=platform,
        message_id="core-request-1",
        additional_config=additional,
        group_info=group,
        user_info=user,
    )
    return SimpleNamespace(
        message_info=info,
        message_segment=segment or {"type": "text", "data": "hello"},
    )


class DiscordNativeTransportTests(unittest.IsolatedAsyncioTestCase):
    def assert_mentions_disabled(self, value):
        self.assertFalse(value.everyone)
        self.assertFalse(value.users)
        self.assertFalse(value.roles)
        self.assertFalse(value.replied_user)

    async def test_text_ingress_uses_canonical_context_and_native_target(self):
        adapter = _adapter()
        author = SimpleNamespace(
            id=int(USER_ID), bot=False, display_name="小林", name="林"
        )
        channel = FakeChannel(CHANNEL_ID, guild=SimpleNamespace(id=int(GUILD_ID)))
        channel.name = "general"
        message = SimpleNamespace(
            author=author,
            channel=channel,
            guild=channel.guild,
            content="普通文字",
            id=int(MESSAGE_ID),
            attachments=[],
            stickers=[],
            embeds=[],
            reference=None,
            mentions=[],
            created_at=None,
        )

        self.assertTrue(await adapter.handle_discord_message(message))

        request = adapter.router.send_message.await_args.args[0]
        info = request.message_info
        self.assertEqual(info.platform, "discord")
        self.assertEqual(info.user_info.user_id, "legacy-user-41")
        self.assertEqual(info.group_info.group_id, "legacy-room-8")
        additional = info.additional_config
        self.assertEqual(
            additional["delivery_target"],
            {
                "schema_version": 1,
                "transport": "discord",
                "channel_id": CHANNEL_ID,
                "user_id": USER_ID,
                "guild_id": GUILD_ID,
                "mode": "text",
                "voice_generation": "",
            },
        )
        self.assertEqual(additional["runtime_capabilities"]["schema_version"], 1)
        self.assertIs(additional["runtime_capabilities"].get("planner_bypass", False), False)
        self.assertEqual(additional["runtime_capabilities"]["voice_payload_formats"], ["wav"])
        self.assertEqual(additional["visual_policy"]["profile"], "discord-native-v1")
        self.assertEqual(additional["visual_policy"]["image"]["max_tokens"], 321)
        self.assertEqual(additional["visual_policy"]["image"]["extra_params"]["test_flag"], "native")
        # Adapter-local voice prompts must not become ordinary text templates.
        self.assertIsNone(info.template_info)

    async def test_first_message_uses_local_forward_time_and_preserves_native_author_time(self):
        adapter = _adapter()
        channel = FakeChannel(CHANNEL_ID, guild=SimpleNamespace(id=int(GUILD_ID)))
        author = SimpleNamespace(
            id=int(USER_ID), bot=False, display_name="小林", name="林"
        )
        created_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        message = SimpleNamespace(
            author=author,
            channel=channel,
            guild=channel.guild,
            content="第一条消息",
            id=int(MESSAGE_ID),
            attachments=[],
            stickers=[],
            embeds=[],
            reference=None,
            created_at=created_at,
        )
        processing_finished_at = []
        route_started_at = []
        original_content_segments = adapter._content_segments

        async def delayed_content_segments(content):
            await asyncio.sleep(0.02)
            segments = await original_content_segments(content)
            processing_finished_at.append(time.time())
            return segments

        async def record_route(request):
            route_started_at.append(time.time())
            return True

        adapter._content_segments = delayed_content_segments
        adapter.router.send_message = AsyncMock(side_effect=record_route)

        self.assertTrue(await adapter.handle_discord_message(message))

        info = adapter.router.send_message.await_args.args[0].message_info
        self.assertEqual(info.user_info.user_id, "legacy-user-41")
        self.assertEqual(info.user_info.user_nickname, "小林")
        self.assertEqual(info.group_info.group_id, "legacy-room-8")
        self.assertGreaterEqual(info.time, processing_finished_at[0])
        self.assertLess(info.time, created_at.timestamp())
        self.assertLessEqual(info.time, route_started_at[0])
        self.assertEqual(
            info.additional_config["discord_transport"]["created_at"],
            created_at.isoformat(),
        )

    async def test_native_self_ids_are_still_rejected(self):
        config = _config()
        config.identity_map = IdentityMap.from_mapping(
            {
                "schema_version": 1,
                "user_native_to_logical": {},
                "channel_native_to_logical": {},
                "private_channels": [],
                "bot_self_ids": [USER_ID],
            }
        )
        adapter = _adapter(config=config)
        message = SimpleNamespace(
            author=SimpleNamespace(id=int(USER_ID), bot=False, display_name="self"),
            channel=FakeChannel(CHANNEL_ID, guild=SimpleNamespace(id=int(GUILD_ID))),
            guild=SimpleNamespace(id=int(GUILD_ID)),
            content="self message",
            id=int(MESSAGE_ID),
            attachments=[],
            stickers=[],
            embeds=[],
            reference=None,
            created_at=None,
        )

        self.assertFalse(await adapter.handle_discord_message(message))
        adapter.router.send_message.assert_not_awaited()

    async def test_native_bot_webhook_and_self_messages_remain_rejected(self):
        cases = (
            (SimpleNamespace(id=int(USER_ID), bot=True), None),
            (SimpleNamespace(id=int(USER_ID), bot=False), "webhook"),
            (SimpleNamespace(id=int(BOT_ID), bot=False), None),
        )
        for author, webhook_id in cases:
            with self.subTest(author_id=author.id, webhook_id=webhook_id):
                adapter = _adapter()
                message = SimpleNamespace(
                    author=author,
                    channel=FakeChannel(
                        CHANNEL_ID, guild=SimpleNamespace(id=int(GUILD_ID))
                    ),
                    guild=SimpleNamespace(id=int(GUILD_ID)),
                    content="忽略此消息",
                    id=int(MESSAGE_ID),
                    attachments=[],
                    stickers=[],
                    embeds=[],
                    reference=None,
                    created_at=None,
                    webhook_id=webhook_id,
                )

                self.assertFalse(await adapter.handle_discord_message(message))
                adapter.router.send_message.assert_not_awaited()

    async def test_current_bot_mentions_and_resolved_bot_replies_wake_core(self):
        cases = (
            ("current mention", [SimpleNamespace(id=int(BOT_ID))], None, True),
            (
                "foreign mention",
                [SimpleNamespace(id=int(OTHER_USER_ID))],
                None,
                False,
            ),
            (
                "reply to current bot",
                [],
                SimpleNamespace(
                    message_id=int(MESSAGE_ID),
                    resolved=SimpleNamespace(author=SimpleNamespace(id=int(BOT_ID))),
                ),
                True,
            ),
            (
                "reply to another user",
                [],
                SimpleNamespace(
                    message_id=int(MESSAGE_ID),
                    resolved=SimpleNamespace(
                        author=SimpleNamespace(id=int(OTHER_USER_ID))
                    ),
                ),
                False,
            ),
        )
        for label, mentions, reference, expected in cases:
            with self.subTest(label=label):
                config = _config()
                if label == "foreign mention":
                    config.identity_map = IdentityMap.from_mapping(
                        {
                            "schema_version": 1,
                            "user_native_to_logical": {
                                USER_ID: "legacy-user-41",
                                OTHER_USER_ID: "legacy-user-42",
                            },
                            "channel_native_to_logical": {
                                CHANNEL_ID: "legacy-room-8",
                                OTHER_CHANNEL_ID: "legacy-dm-channel-9",
                            },
                            "private_channels": [],
                            # This exported legacy ID is not the current Gateway bot.
                            "bot_self_ids": [OTHER_USER_ID],
                        }
                    )
                adapter = _adapter(config=config)
                channel = FakeChannel(
                    CHANNEL_ID, guild=SimpleNamespace(id=int(GUILD_ID))
                )
                channel.name = "general"
                message = SimpleNamespace(
                    author=SimpleNamespace(
                        id=int(USER_ID), bot=False, display_name="林", name="林"
                    ),
                    channel=channel,
                    guild=channel.guild,
                    content="说点什么",
                    id=int(MESSAGE_ID),
                    attachments=[],
                    stickers=[],
                    embeds=[],
                    mentions=mentions,
                    reference=reference,
                    created_at=None,
                )

                self.assertTrue(await adapter.handle_discord_message(message))
                info = adapter.router.send_message.await_args.args[0].message_info
                got = info.additional_config.get("is_mentioned")
                if expected:
                    self.assertEqual(got, 1.0)
                else:
                    self.assertIsNone(got)

    async def test_resolved_user_and_channel_tags_are_rendered_with_names_and_ids(self):
        adapter = _adapter()
        author = SimpleNamespace(
            id=int(USER_ID), bot=False, display_name="林", name="林"
        )
        channel = FakeChannel(
            CHANNEL_ID, guild=SimpleNamespace(id=int(GUILD_ID))
        )
        channel.name = "general"
        message = SimpleNamespace(
            author=author,
            channel=channel,
            guild=channel.guild,
            content=f"<@{BOT_ID}> 在 <#{CHANNEL_ID}>",
            id=int(MESSAGE_ID),
            attachments=[],
            stickers=[],
            embeds=[],
            mentions=[SimpleNamespace(id=int(BOT_ID), display_name="Nacho")],
            channel_mentions=[SimpleNamespace(id=int(CHANNEL_ID), name="general")],
            role_mentions=[],
            reference=None,
            created_at=None,
        )

        self.assertTrue(await adapter.handle_discord_message(message))
        request = adapter.router.send_message.await_args.args[0]
        text_segment = request.message_segment.data[0]
        self.assertEqual(
            text_segment.data,
            f"@Nacho ({BOT_ID}) 在 #general ({CHANNEL_ID})",
        )
        self.assertEqual(request.message_info.additional_config["is_mentioned"], 1.0)

    async def test_text_and_voice_whitelists_are_independent_and_bans_apply_to_both(self):
        config = _config(
            chat=ChatConfig(
                group_list_type="whitelist",
                group_list=["legacy-room-8"],
                private_list_type="whitelist",
                private_list=["legacy-user-41"],
                ban_user_id=["legacy-user-42"],
            ),
            voice=VoiceConfig(
                enabled=True,
                allowed_channel_list_type="whitelist",
                allowed_channel_list=["legacy-room-8"],
            ),
        )
        adapter = _adapter(config=config)
        author = SimpleNamespace(id=int(USER_ID))
        group = SimpleNamespace(
            id=int(CHANNEL_ID), guild=SimpleNamespace(id=int(GUILD_ID))
        )

        self.assertTrue(adapter.is_context_allowed(author, group))
        self.assertTrue(adapter.is_context_allowed(author, SimpleNamespace(id=1, guild=None)))
        self.assertTrue(adapter.is_voice_user_allowed(USER_ID, CHANNEL_ID))
        self.assertFalse(adapter.is_voice_user_allowed(OTHER_USER_ID, CHANNEL_ID))
        self.assertFalse(adapter.is_context_allowed(SimpleNamespace(id=int(OTHER_USER_ID)), group))
        self.assertFalse(adapter.is_voice_user_allowed(USER_ID, OTHER_CHANNEL_ID))

    async def test_invalid_or_foreign_targets_are_dropped_but_absent_target_uses_legacy_text_route(self):
        channel = FakeChannel(CHANNEL_ID)
        adapter = _adapter(channels={CHANNEL_ID: channel})
        group = SimpleNamespace(platform="discord", group_id="legacy-room-8")
        user = SimpleNamespace(platform="discord", user_id="legacy-user-41")

        foreign = _core_message(platform="qq", group=group, user=user)
        malformed = _core_message(
            target={
                "schema_version": 1,
                "transport": "discord",
                "channel_id": "42",  # legacy aliases are not native Snowflakes
                "user_id": USER_ID,
                "guild_id": GUILD_ID,
                "mode": "text",
                "voice_generation": "",
            },
            group=group,
            user=user,
        )
        await adapter.handle_from_nachobot(foreign)
        await adapter.handle_from_nachobot(malformed)
        self.assertEqual(channel.sent, [])
        adapter.router.send_custom_message.assert_not_awaited()

        legacy = _core_message(group=group, user=user)
        await adapter.handle_from_nachobot(legacy)
        self.assertEqual(channel.sent[0][0], "hello")
        adapter.router.send_custom_message.assert_awaited_once_with(
            "discord",
            "message_id_echo",
            {
                "type": "echo",
                "echo": "core-request-1",
                "actual_id": "823456789012345679",
                "platform": "discord",
            },
        )

    async def test_group_egress_uses_target_channel_and_slotted_reply_destination(self):
        channel = FakeChannel(CHANNEL_ID, guild=SimpleNamespace(id=int(GUILD_ID)))
        adapter = _adapter(channels={CHANNEL_ID: channel})
        target = {
            "schema_version": 1,
            "transport": "discord",
            "channel_id": CHANNEL_ID,
            "user_id": OTHER_USER_ID,
            "guild_id": GUILD_ID,
            "mode": "text",
            "voice_generation": "",
        }
        message = _core_message(
            target=target,
            group=SimpleNamespace(platform="discord", group_id="legacy-room-8"),
            # The last speaker differs from the addressed member; delivery stays
            # anchored to the validated channel in delivery_target.
            user=SimpleNamespace(platform="discord", user_id="legacy-user-41"),
            segment={
                "type": "seglist",
                "data": [
                    {"type": "reply", "data": {"message_id": MESSAGE_ID}},
                    {"type": "text", "data": "safe reply"},
                ],
            },
        )

        await adapter.handle_from_nachobot(message)

        self.assertEqual(len(channel.sent), 1)
        content, kwargs = channel.sent[0]
        self.assertEqual(content, "safe reply")
        self.assert_mentions_disabled(kwargs["allowed_mentions"])
        reference = kwargs["reference"]
        self.assertIsInstance(reference, discord.MessageReference)
        self.assertEqual(reference.message_id, int(MESSAGE_ID))
        self.assertEqual(reference.channel_id, int(CHANNEL_ID))
        receipt = adapter.router.send_custom_message.await_args.args[2]
        self.assertEqual(receipt["actual_id"], "823456789012345679")

    async def test_reply_marker_after_text_still_references_first_text_message(self):
        channel = FakeChannel(CHANNEL_ID, guild=SimpleNamespace(id=int(GUILD_ID)))
        adapter = _adapter(channels={CHANNEL_ID: channel})
        message = _core_message(
            target={
                "schema_version": 1,
                "transport": "discord",
                "channel_id": CHANNEL_ID,
                "user_id": USER_ID,
                "guild_id": GUILD_ID,
                "mode": "text",
                "voice_generation": "",
            },
            group=SimpleNamespace(platform="discord", group_id="legacy-room-8"),
            user=SimpleNamespace(platform="discord", user_id="legacy-user-41"),
            segment={
                "type": "seglist",
                "data": [
                    {"type": "text", "data": "first chunk"},
                    {"type": "text", "data": "second chunk"},
                    {"type": "reply", "data": {"message_id": MESSAGE_ID}},
                ],
            },
        )

        await adapter.handle_from_nachobot(message)

        self.assertEqual([sent[0] for sent in channel.sent], ["first chunk", "second chunk"])
        first_kwargs = channel.sent[0][1]
        second_kwargs = channel.sent[1][1]
        self.assertIsInstance(first_kwargs["reference"], discord.MessageReference)
        self.assertEqual(first_kwargs["reference"].message_id, int(MESSAGE_ID))
        self.assertNotIn("reference", second_kwargs)

    async def test_first_video_or_audio_attachment_consumes_reply_reference_once(self):
        attachment_cases = (
            (
                "video",
                {"base64": "AQ==", "size": 1, "name": "clip.mp4"},
            ),
            ("voice", "AQ=="),
        )
        for kind, payload in attachment_cases:
            with self.subTest(kind=kind):
                channel = FakeChannel(
                    CHANNEL_ID, guild=SimpleNamespace(id=int(GUILD_ID))
                )
                config = _config(
                    voice=VoiceConfig(enabled=False, use_tts=kind == "voice")
                )
                adapter = _adapter(config=config, channels={CHANNEL_ID: channel})
                if kind == "voice":
                    payload = pcm16_to_wav_base64(
                        b"\x01\x00" * 4_800,
                        sample_rate=48_000,
                        channels=1,
                    )
                    adapter.bot.http = SimpleNamespace(
                        request=AsyncMock(
                            return_value={
                                "id": "823456789012345679",
                                "channel_id": CHANNEL_ID,
                            }
                        )
                    )
                message = _core_message(
                    target={
                        "schema_version": 1,
                        "transport": "discord",
                        "channel_id": CHANNEL_ID,
                        "user_id": USER_ID,
                        "guild_id": GUILD_ID,
                        "mode": "text",
                        "voice_generation": "",
                    },
                    group=SimpleNamespace(
                        platform="discord", group_id="legacy-room-8"
                    ),
                    user=SimpleNamespace(
                        platform="discord", user_id="legacy-user-41"
                    ),
                    segment={
                        "type": "seglist",
                        "data": [
                            {"type": "reply", "data": {"message_id": MESSAGE_ID}},
                            {"type": kind, "data": payload},
                            {"type": "text", "data": "after attachment"},
                        ],
                    },
                )

                await adapter.handle_from_nachobot(message)

                if kind == "video":
                    self.assertEqual(len(channel.sent), 2)
                    attachment_kwargs = channel.sent[0][1]
                    self.assertIn("file", attachment_kwargs)
                    self.assertIsInstance(attachment_kwargs["reference"], discord.MessageReference)
                    self.assertEqual(
                        attachment_kwargs["reference"].message_id, int(MESSAGE_ID)
                    )
                    attachment_kwargs["file"].close()
                else:
                    self.assertEqual(len(channel.sent), 1)
                    audio_request = adapter.bot.http.request.await_args
                    voice_payload = json.loads(audio_request.kwargs["form"][0]["value"])
                    self.assertEqual(
                        voice_payload["message_reference"]["message_id"], int(MESSAGE_ID)
                    )
                self.assertNotIn("reference", channel.sent[-1][1])
                self.assertEqual(channel.sent[-1][0], "after attachment")

    async def test_dm_channel_is_accepted_only_for_its_exact_recipient(self):
        wrong_dm = FakeChannel(
            OTHER_CHANNEL_ID, recipient=SimpleNamespace(id=int(OTHER_USER_ID))
        )
        config = _config()
        config.identity_map = _identity_map(include_private=False)
        adapter = _adapter(config=config, channels={OTHER_CHANNEL_ID: wrong_dm})
        user = SimpleNamespace(platform="discord", user_id="legacy-user-41")
        target = {
            "schema_version": 1,
            "transport": "discord",
            "channel_id": OTHER_CHANNEL_ID,
            "user_id": USER_ID,
            "guild_id": "",
            "mode": "text",
            "voice_generation": "",
        }

        await adapter.handle_from_nachobot(
            _core_message(target=target, user=user)
        )

        self.assertEqual(wrong_dm.sent, [])
        adapter.router.send_custom_message.assert_not_awaited()

    async def test_dm_target_with_matching_cached_recipient_is_delivered(self):
        right_dm = FakeChannel(
            OTHER_CHANNEL_ID, recipient=SimpleNamespace(id=int(USER_ID))
        )
        config = _config()
        config.identity_map = IdentityMap.from_mapping(
            {
                "schema_version": 1,
                "user_native_to_logical": {USER_ID: "legacy-user-41"},
                "channel_native_to_logical": {},
                "private_channels": [],
                "bot_self_ids": [],
            }
        )
        adapter = _adapter(config=config, channels={OTHER_CHANNEL_ID: right_dm})
        target = {
            "schema_version": 1,
            "transport": "discord",
            "channel_id": OTHER_CHANNEL_ID,
            "user_id": USER_ID,
            "guild_id": "",
            "mode": "text",
            "voice_generation": "",
        }

        await adapter.handle_from_nachobot(
            _core_message(
                target=target,
                user=SimpleNamespace(platform="discord", user_id="legacy-user-41"),
            )
        )

        self.assertEqual(right_dm.sent[0][0], "hello")
        self.assertEqual(
            adapter.router.send_custom_message.await_args.args[2]["actual_id"],
            "823456789012345679",
        )

    async def test_voice_reply_plays_audio_without_sending_text_to_channel(self):
        guild = SimpleNamespace(id=int(GUILD_ID))
        text_destination = FakeChannel(CHANNEL_ID, guild=guild)
        voice_client = SimpleNamespace(channel=text_destination)
        session = SimpleNamespace(
            guild_id=int(GUILD_ID),
            channel_id=int(CHANNEL_ID),
            generation="voice-generation-7",
            voice_client=voice_client,
        )
        adapter = _adapter(channels={CHANNEL_ID: text_destination})
        adapter.bot.get_voice_session.return_value = session
        target = {
            "schema_version": 1,
            "transport": "discord",
            "channel_id": CHANNEL_ID,
            "user_id": USER_ID,
            "guild_id": GUILD_ID,
            "mode": "voice",
            "voice_generation": session.generation,
        }
        message = _core_message(
            target=target,
            group=SimpleNamespace(platform="discord", group_id="legacy-room-8"),
            user=SimpleNamespace(platform="discord", user_id="legacy-user-41"),
            segment={
                "type": "seglist",
                "data": [
                    {
                        "type": "text",
                        "data": '{"reply":"在这里文字回复","tts_text":"你好"}',
                    },
                    {"type": "voice", "data": "offline-wav-payload"},
                ],
            },
        )

        with patch("adapter.write_wav_base64", return_value="owned-audio.wav"):
            await adapter.handle_from_nachobot(message)

        self.assertEqual(text_destination.sent, [])
        adapter.bot.own_temp_audio.assert_called_once_with("owned-audio.wav")
        adapter.bot.speak.assert_awaited_once_with(
            int(CHANNEL_ID),
            "owned-audio.wav",
            session.generation,
            cleanup=True,
            wait_until_started=True,
        )
        receipt = adapter.router.send_custom_message.await_args.args[2]
        self.assertTrue(receipt["actual_id"].startswith("voice-playback:"))

    async def test_text_only_voice_reply_has_no_native_text_send_or_receipt(self):
        channel = FakeChannel(CHANNEL_ID, guild=SimpleNamespace(id=int(GUILD_ID)))
        session = SimpleNamespace(
            guild_id=int(GUILD_ID),
            channel_id=int(CHANNEL_ID),
            generation="voice-generation-8",
            voice_client=SimpleNamespace(channel=channel),
        )
        adapter = _adapter(channels={CHANNEL_ID: channel})
        adapter.bot.get_voice_session.return_value = session
        target = {
            "schema_version": 1,
            "transport": "discord",
            "channel_id": CHANNEL_ID,
            "user_id": USER_ID,
            "guild_id": GUILD_ID,
            "mode": "voice",
            "voice_generation": session.generation,
        }
        await adapter.handle_from_nachobot(
            _core_message(
                target=target,
                group=SimpleNamespace(platform="discord", group_id="legacy-room-8"),
                user=SimpleNamespace(platform="discord", user_id="legacy-user-41"),
                segment={
                    "type": "text",
                    "data": '{"reply":"should remain Core-only","tts_text":"spoken"}',
                },
            )
        )

        self.assertEqual(channel.sent, [])
        adapter.router.send_custom_message.assert_not_awaited()

    async def test_voice_target_keeps_explicit_file_attachment_delivery(self):
        channel = FakeChannel(CHANNEL_ID, guild=SimpleNamespace(id=int(GUILD_ID)))
        session = SimpleNamespace(
            guild_id=int(GUILD_ID),
            channel_id=int(CHANNEL_ID),
            generation="voice-generation-9",
            voice_client=SimpleNamespace(channel=channel),
        )
        adapter = _adapter(channels={CHANNEL_ID: channel})
        adapter.bot.get_voice_session.return_value = session
        target = {
            "schema_version": 1,
            "transport": "discord",
            "channel_id": CHANNEL_ID,
            "user_id": USER_ID,
            "guild_id": GUILD_ID,
            "mode": "voice",
            "voice_generation": session.generation,
        }
        content = b"tool output"
        await adapter.handle_from_nachobot(
            _core_message(
                target=target,
                group=SimpleNamespace(platform="discord", group_id="legacy-room-8"),
                user=SimpleNamespace(platform="discord", user_id="legacy-user-41"),
                segment={
                    "type": "file",
                    "data": {
                        "base64": base64.b64encode(content).decode("ascii"),
                        "size": len(content),
                        "name": "tool.txt",
                    },
                },
            )
        )

        self.assertEqual(len(channel.sent), 1)
        self.assertEqual(channel.sent[0][1]["file"].filename, "tool.txt")

    async def test_nonvoice_target_keeps_arbitrary_json_text_unchanged(self):
        channel = FakeChannel(CHANNEL_ID, guild=SimpleNamespace(id=int(GUILD_ID)))
        adapter = _adapter(channels={CHANNEL_ID: channel})
        target = {
            "schema_version": 1,
            "transport": "discord",
            "channel_id": CHANNEL_ID,
            "user_id": USER_ID,
            "guild_id": GUILD_ID,
            "mode": "text",
            "voice_generation": "",
        }
        json_text = '{"reply":"ordinary JSON","value":{"n":2}}'
        await adapter.handle_from_nachobot(
            _core_message(
                target=target,
                group=SimpleNamespace(platform="discord", group_id="legacy-room-8"),
                user=SimpleNamespace(platform="discord", user_id="legacy-user-41"),
                segment={"type": "text", "data": json_text},
            )
        )

        self.assertEqual(channel.sent[0][0], json_text)

    async def test_core_tts_attachment_uses_the_native_text_delivery_target(self):
        channel = FakeChannel(CHANNEL_ID, guild=SimpleNamespace(id=int(GUILD_ID)))
        adapter = _adapter(
            config=_config(voice=VoiceConfig(enabled=False, use_tts=True)),
            channels={CHANNEL_ID: channel},
        )
        adapter.bot.http = SimpleNamespace(
            request=AsyncMock(return_value={"id": "823456789012345679", "channel_id": CHANNEL_ID})
        )
        target = {
            "schema_version": 1,
            "transport": "discord",
            "channel_id": CHANNEL_ID,
            "user_id": USER_ID,
            "guild_id": GUILD_ID,
            "mode": "text",
            "voice_generation": "",
        }
        message = _core_message(
            target=target,
            group=SimpleNamespace(platform="discord", group_id="legacy-room-8"),
            user=SimpleNamespace(platform="discord", user_id="legacy-user-41"),
            segment={
                "type": "voice",
                "data": pcm16_to_wav_base64(
                    b"\x01\x00" * 4_800,
                    sample_rate=48_000,
                    channels=1,
                ),
            },
        )

        await adapter.handle_from_nachobot(message)

        adapter.bot.http.request.assert_awaited_once()
        self.assertEqual(channel.sent, [])
        receipt = adapter.router.send_custom_message.await_args.args[2]
        self.assertEqual(receipt["actual_id"], "823456789012345679")

    async def test_voicefile_from_core_media_tmp_is_copied_and_cleanup_preserves_source(self):
        guild = SimpleNamespace(id=int(GUILD_ID))
        text_destination = FakeChannel(CHANNEL_ID, guild=guild)
        session = SimpleNamespace(
            guild_id=int(GUILD_ID),
            channel_id=int(CHANNEL_ID),
            generation="voice-generation-9",
            voice_client=SimpleNamespace(channel=text_destination),
        )
        adapter = _adapter(channels={CHANNEL_ID: text_destination})
        adapter.bot.get_voice_session.return_value = session
        owned_paths = set()
        copied_paths = []

        def own_temp_audio(path):
            owned_paths.add(str(Path(path).resolve()))

        async def observe_started_playback(channel_id, audio_path, generation, **kwargs):
            copied_paths.append(Path(audio_path))
            self.assertEqual(channel_id, int(CHANNEL_ID))
            self.assertEqual(generation, session.generation)
            self.assertTrue(kwargs["cleanup"])
            self.assertTrue(kwargs["wait_until_started"])
            self.assertIn(str(Path(audio_path).resolve()), owned_paths)
            self.assertEqual(Path(audio_path).read_bytes(), b"trimmed voice")
            Path(audio_path).unlink()
            owned_paths.remove(str(Path(audio_path).resolve()))
            return True

        adapter.bot.own_temp_audio = Mock(side_effect=own_temp_audio)
        adapter.bot.speak = AsyncMock(side_effect=observe_started_playback)
        target = {
            "schema_version": 1,
            "transport": "discord",
            "channel_id": CHANNEL_ID,
            "user_id": USER_ID,
            "guild_id": GUILD_ID,
            "mode": "voice",
            "voice_generation": session.generation,
        }
        message = _core_message(
            target=target,
            group=SimpleNamespace(platform="discord", group_id="legacy-room-8"),
            user=SimpleNamespace(platform="discord", user_id="legacy-user-41"),
            segment={"type": "voicefile", "data": "trimmed-source.wav"},
        )

        with tempfile.TemporaryDirectory(prefix="discord-core-media-tmp-") as folder:
            root = Path(folder)
            media_dir = root / "NachoBot" / "data" / "media-tmp"
            media_dir.mkdir(parents=True)
            source = media_dir / "mus_trim-offline.wav"
            source.write_bytes(b"trimmed voice")
            message.message_segment["data"] = str(source)

            with patch("adapter._root_dir", root):
                await adapter.handle_from_nachobot(message)

            self.assertTrue(source.exists())
            self.assertEqual(source.read_bytes(), b"trimmed voice")
            self.assertEqual(len(copied_paths), 1)
            self.assertNotEqual(copied_paths[0].resolve(), source.resolve())
            self.assertFalse(copied_paths[0].exists())
            self.assertEqual(owned_paths, set())
        adapter.router.send_custom_message.assert_awaited_once()
        actual_id = adapter.router.send_custom_message.await_args.args[2]["actual_id"]
        self.assertTrue(
            actual_id.startswith(
                f"voice-playback:{CHANNEL_ID}:{session.generation}:"
            )
        )

    async def test_send_failure_does_not_emit_a_false_message_receipt(self):
        channel = FakeChannel(
            CHANNEL_ID,
            guild=SimpleNamespace(id=int(GUILD_ID)),
            send_result=None,
        )
        adapter = _adapter(channels={CHANNEL_ID: channel})
        message = _core_message(
            target={
                "schema_version": 1,
                "transport": "discord",
                "channel_id": CHANNEL_ID,
                "user_id": USER_ID,
                "guild_id": GUILD_ID,
                "mode": "text",
                "voice_generation": "",
            },
            group=SimpleNamespace(platform="discord", group_id="legacy-room-8"),
            user=SimpleNamespace(platform="discord", user_id="legacy-user-41"),
        )

        await adapter.handle_from_nachobot(message)

        self.assertEqual(len(channel.sent), 1)
        adapter.router.send_custom_message.assert_not_awaited()

    async def test_slash_business_commands_forward_but_join_and_leave_stay_local(self):
        forwarded = AsyncMock(return_value=True)
        adapter = _adapter()
        adapter.forward_slash = forwarded
        bot = SimpleNamespace(logger=Mock())
        transport = DiscordTransportCog(bot, adapter)
        response = SimpleNamespace(is_done=Mock(return_value=True))
        followup = AsyncMock()
        ctx = SimpleNamespace(
            interaction=SimpleNamespace(response=response),
            followup=SimpleNamespace(send=followup),
        )

        await transport._forward(ctx, "#help")
        forwarded.assert_awaited_once_with(ctx, "#help")
        followup.assert_not_awaited()

        voice_adapter = SimpleNamespace(
            is_context_allowed=Mock(return_value=True),
            is_voice_channel_allowed=Mock(return_value=True),
            forward_slash=AsyncMock(),
        )
        voice_channel = SimpleNamespace(id=int(CHANNEL_ID), name="voice", members=[])
        voice_client = SimpleNamespace(channel=None)
        guild = SimpleNamespace(id=int(GUILD_ID), voice_client=None)

        async def connect():
            voice_client.channel = voice_channel
            voice_client.guild = guild
            guild.voice_client = voice_client
            return voice_client

        async def disconnect(*, force):
            self.assertTrue(force)
            guild.voice_client = None

        voice_client.disconnect = disconnect
        voice_channel.connect = AsyncMock(side_effect=connect)
        voice_bot = SimpleNamespace(
            logger=Mock(),
            join_voice_channel=AsyncMock(return_value=("joined", None)),
            leave_voice_channel=AsyncMock(),
        )
        voice_cog = VoiceCog(voice_bot, voice_adapter)
        local_ctx = SimpleNamespace(
            interaction=SimpleNamespace(response=SimpleNamespace(is_done=Mock(return_value=True))),
            guild=guild,
            author=SimpleNamespace(
                id=int(USER_ID),
                voice=SimpleNamespace(channel=voice_channel),
            ),
            channel=FakeChannel(CHANNEL_ID, guild=guild),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        await VoiceCog.join_voice_channel.callback(voice_cog, local_ctx)
        voice_bot.join_voice_channel.assert_awaited_once_with(guild, voice_channel)
        guild.voice_client = voice_client
        await VoiceCog.leave_voice_channel.callback(voice_cog, local_ctx)
        voice_bot.leave_voice_channel.assert_awaited_once_with(guild)
        voice_channel.connect.assert_not_awaited()
        voice_adapter.forward_slash.assert_not_awaited()
        self.assertEqual(local_ctx.followup.send.await_count, 2)

    async def test_slash_business_command_contains_native_core_routing_metadata(self):
        channel = FakeChannel(CHANNEL_ID, guild=SimpleNamespace(id=int(GUILD_ID)))
        channel.name = "general"
        adapter = _adapter(channels={CHANNEL_ID: channel})
        dispatched = asyncio.Event()

        async def hold_for_core_response(request):
            self.assertTrue(ctx.deferred)
            dispatched.set()
            return True

        adapter.router.send_message = AsyncMock(side_effect=hold_for_core_response)
        ctx = FakeSlashContext(channel)
        transport = DiscordTransportCog(SimpleNamespace(logger=Mock()), adapter)
        forward_task = asyncio.create_task(transport._forward(ctx, "#summary"))
        await asyncio.wait_for(dispatched.wait(), timeout=1)

        request = adapter.router.send_message.await_args.args[0]
        self.assertEqual(request.message_info.platform, "discord")
        self.assertEqual(request.message_info.user_info.user_id, "legacy-user-41")
        self.assertEqual(request.message_segment.type, "text")
        self.assertEqual(request.message_segment.data, "#summary")
        self.assertEqual(
            request.message_info.additional_config["delivery_target"]["channel_id"],
            CHANNEL_ID,
        )
        self.assertRegex(
            request.message_info.additional_config["delivery_target"]["interaction_key"],
            r"^[0-9a-f]{32}$",
        )
        self.assertEqual(
            request.message_info.additional_config["runtime_capabilities"][
                "reply_delivery"
            ],
            "chunked",
        )
        self.assertEqual(
            request.message_info.additional_config["runtime_capabilities"][
                "voice_payload_formats"
            ],
            ["wav"],
        )
        raw_file = b"interaction-result-file"
        core_segments = {
            "type": "seglist",
            "data": [
                {"type": "reply", "data": {"message_id": MESSAGE_ID}},
                {"type": "text", "data": "x" * 2013},
                {
                    "type": "file",
                    "data": {
                        "base64": base64.b64encode(raw_file).decode("ascii"),
                        "size": len(raw_file),
                        "name": "result.txt",
                    },
                },
            ],
        }
        await adapter.handle_from_nachobot(_core_response_for_slash(request, core_segments))
        await asyncio.wait_for(forward_task, timeout=1)
        self.assertEqual(len(ctx.followup.sent), 3)
        self.assertEqual([len(call[0] or "") for call in ctx.followup.sent], [2000, 13, 0])
        self.assertEqual(ctx.followup.sent[0][0], "x" * 2000)
        self.assertEqual(ctx.followup.sent[1][0], "x" * 13)
        self.assertEqual(ctx.followup.sent[2][1]["file"].filename, "result.txt")
        for _, kwargs in ctx.followup.sent:
            self.assertTrue(kwargs["ephemeral"])
            self.assertTrue(kwargs["wait"])
            self.assert_mentions_disabled(kwargs["allowed_mentions"])
            self.assertNotIn("reference", kwargs)
        self.assertEqual(channel.sent, [])
        adapter.router.send_custom_message.assert_awaited_once()
        receipt = adapter.router.send_custom_message.await_args.args[2]
        self.assertEqual(receipt["actual_id"], "923456789012345678")

        image_bytes = b"second-response-image"
        await adapter.handle_from_nachobot(
            _core_response_for_slash(
                request,
                {
                    "type": "seglist",
                    "data": [
                        {"type": "text", "data": "additional Core output"},
                        {
                            "type": "image",
                            "data": {
                                "base64": base64.b64encode(image_bytes).decode("ascii"),
                                "name": "additional.png",
                            },
                        },
                    ],
                },
                message_id="core-slash-followup",
            )
        )
        self.assertEqual(len(ctx.followup.sent), 5)
        self.assertEqual(ctx.followup.sent[3][0], "additional Core output")
        self.assertEqual(ctx.followup.sent[4][1]["file"].filename, "additional.png")
        self.assertEqual(channel.sent, [])

    async def test_slash_command_uses_the_exact_native_dm_recipient_route(self):
        dm = FakeChannel(
            OTHER_CHANNEL_ID, recipient=SimpleNamespace(id=int(USER_ID))
        )
        adapter = _adapter(channels={OTHER_CHANNEL_ID: dm})
        dispatched = asyncio.Event()

        async def capture(request):
            dispatched.set()
            return True

        adapter.router.send_message = AsyncMock(side_effect=capture)
        ctx = FakeSlashContext(dm, guild=None)
        task = asyncio.create_task(adapter.forward_slash(ctx, "#help"))
        await asyncio.wait_for(dispatched.wait(), timeout=1)
        request = adapter.router.send_message.await_args.args[0]
        self.assertIsNone(request.message_info.group_info)
        self.assertIs(
            request.message_info.additional_config["runtime_capabilities"].get(
                "planner_bypass", False
            ),
            False,
        )
        target = request.message_info.additional_config["delivery_target"]
        self.assertEqual(target["channel_id"], OTHER_CHANNEL_ID)
        self.assertEqual(target["user_id"], USER_ID)
        await adapter.handle_from_nachobot(
            _core_response_for_slash(
                request, {"type": "text", "data": "private help result"}
            )
        )
        self.assertTrue(await asyncio.wait_for(task, timeout=1))
        self.assertEqual([call[0] for call in ctx.followup.sent], ["private help result"])
        self.assertEqual(dm.sent, [])

    async def test_concurrent_slash_commands_are_isolated_by_interaction_key(self):
        channel = FakeChannel(CHANNEL_ID, guild=SimpleNamespace(id=int(GUILD_ID)))
        adapter = _adapter(channels={CHANNEL_ID: channel})
        dispatched = asyncio.Event()
        requests = []

        async def capture(request):
            requests.append(request)
            if len(requests) == 2:
                dispatched.set()
            return True

        adapter.router.send_message = AsyncMock(side_effect=capture)
        first = FakeSlashContext(channel, interaction_id=MESSAGE_ID)
        second = FakeSlashContext(channel, interaction_id="823456789012345678")
        first_task = asyncio.create_task(adapter.forward_slash(first, "#help"))
        second_task = asyncio.create_task(adapter.forward_slash(second, "#summary"))
        await asyncio.wait_for(dispatched.wait(), timeout=1)

        keys = [
            request.message_info.additional_config["delivery_target"]["interaction_key"]
            for request in requests
        ]
        self.assertEqual(len(set(keys)), 2)
        await asyncio.gather(
            adapter.handle_from_nachobot(
                _core_response_for_slash(requests[0], {"type": "text", "data": "first result"})
            ),
            adapter.handle_from_nachobot(
                _core_response_for_slash(requests[1], {"type": "text", "data": "second result"})
            ),
        )
        self.assertEqual(await asyncio.gather(first_task, second_task), [True, True])
        self.assertEqual([call[0] for call in first.followup.sent], ["first result"])
        self.assertEqual([call[0] for call in second.followup.sent], ["second result"])
        self.assertEqual(channel.sent, [])

    async def test_bad_unknown_mismatched_and_expired_interaction_keys_fail_closed(self):
        channel = FakeChannel(CHANNEL_ID, guild=SimpleNamespace(id=int(GUILD_ID)))
        adapter = _adapter(channels={CHANNEL_ID: channel})
        adapter._slash_interactions = SlashInteractionRegistry(ttl_seconds=0.1)
        target = adapter._target(CHANNEL_ID, USER_ID, GUILD_ID, "text")
        followup = FakeInteractionFollowup()
        binding = adapter._slash_interactions.register(followup, target)
        bound_target = {**target, "interaction_key": binding.key}
        group = SimpleNamespace(platform="discord", group_id="legacy-room-8")
        user = SimpleNamespace(platform="discord", user_id="legacy-user-41")

        malformed = {**target, "interaction_key": "not-a-key"}
        unknown = {**target, "interaction_key": "a" * 32}
        mismatched = {**bound_target, "user_id": OTHER_USER_ID}
        for index, invalid_target in enumerate((malformed, unknown, mismatched)):
            await adapter.handle_from_nachobot(
                _core_message(
                    target=invalid_target,
                    group=group,
                    user=user,
                    segment={"type": "text", "data": f"must not be public {index}"},
                )
            )

        await asyncio.sleep(0.11)
        await adapter.handle_from_nachobot(
            _core_message(
                target=bound_target,
                group=group,
                user=user,
                segment={"type": "text", "data": "expired result"},
            )
        )
        self.assertEqual(channel.sent, [])
        self.assertEqual(followup.sent, [])
        self.assertEqual(len(adapter._slash_interactions), 0)

    async def test_expired_binding_while_waiting_for_send_never_falls_back(self):
        channel = FakeChannel(CHANNEL_ID, guild=SimpleNamespace(id=int(GUILD_ID)))
        adapter = _adapter(channels={CHANNEL_ID: channel})
        adapter._slash_interactions = SlashInteractionRegistry(ttl_seconds=0.02)
        target = adapter._target(CHANNEL_ID, USER_ID, GUILD_ID, "text")
        followup = FakeInteractionFollowup()
        binding = adapter._slash_interactions.register(followup, target)
        await binding.send_lock.acquire()
        message = _core_message(
            target={**target, "interaction_key": binding.key},
            group=SimpleNamespace(platform="discord", group_id="legacy-room-8"),
            user=SimpleNamespace(platform="discord", user_id="legacy-user-41"),
            segment={"type": "text", "data": "late response"},
        )
        egress = asyncio.create_task(adapter.handle_from_nachobot(message))
        await asyncio.sleep(0)
        await asyncio.sleep(0.03)
        binding.send_lock.release()
        await asyncio.wait_for(egress, timeout=1)
        self.assertEqual(followup.sent, [])
        self.assertEqual(channel.sent, [])

    async def test_cancelled_interaction_send_discards_its_binding(self):
        channel = FakeChannel(CHANNEL_ID, guild=SimpleNamespace(id=int(GUILD_ID)))
        adapter = _adapter(channels={CHANNEL_ID: channel})
        target = adapter._target(CHANNEL_ID, USER_ID, GUILD_ID, "text")
        followup = FakeInteractionFollowup()
        followup.send = AsyncMock(side_effect=asyncio.CancelledError)
        binding = adapter._slash_interactions.register(followup, target)
        message = _core_message(
            target={**target, "interaction_key": binding.key},
            group=SimpleNamespace(platform="discord", group_id="legacy-room-8"),
            user=SimpleNamespace(platform="discord", user_id="legacy-user-41"),
            segment={"type": "text", "data": "cancel this delivery"},
        )
        egress = asyncio.create_task(adapter.handle_from_nachobot(message))
        with self.assertRaises(asyncio.CancelledError):
            await egress
        self.assertEqual(len(adapter._slash_interactions), 0)
        self.assertEqual(channel.sent, [])

    async def test_dispatch_failure_and_no_result_report_one_ephemeral_error(self):
        channel = FakeChannel(CHANNEL_ID, guild=SimpleNamespace(id=int(GUILD_ID)))
        adapter = _adapter(channels={CHANNEL_ID: channel})
        transport = DiscordTransportCog(SimpleNamespace(logger=Mock()), adapter)
        adapter.router.send_message = AsyncMock(return_value=False)
        dispatch_context = FakeSlashContext(channel)
        await transport._forward(dispatch_context, "#help")
        self.assertTrue(dispatch_context.deferred)
        self.assertEqual(len(dispatch_context.followup.sent), 1)
        self.assertIn("Core", dispatch_context.followup.sent[0][0])
        self.assertTrue(dispatch_context.followup.sent[0][1]["ephemeral"])
        self.assertEqual(len(adapter._slash_interactions), 0)

        adapter._slash_interactions = SlashInteractionRegistry(ttl_seconds=0.01)
        adapter.router.send_message = AsyncMock(return_value=True)
        timeout_context = FakeSlashContext(channel, interaction_id="823456789012345678")
        await asyncio.wait_for(transport._forward(timeout_context, "#help"), timeout=1)
        self.assertEqual(len(timeout_context.followup.sent), 1)
        self.assertIn("没有返回", timeout_context.followup.sent[0][0])
        self.assertTrue(timeout_context.followup.sent[0][1]["ephemeral"])
        self.assertEqual(len(adapter._slash_interactions), 0)
        self.assertEqual(channel.sent, [])

    async def test_stop_clears_interaction_bindings_and_cancels_waiters(self):
        channel = FakeChannel(CHANNEL_ID, guild=SimpleNamespace(id=int(GUILD_ID)))
        adapter = _adapter(channels={CHANNEL_ID: channel})
        dispatched = asyncio.Event()

        async def accept_without_result(_request):
            dispatched.set()
            return True

        adapter.router.send_message = AsyncMock(side_effect=accept_without_result)
        ctx = FakeSlashContext(channel)
        forward_task = asyncio.create_task(adapter.forward_slash(ctx, "#help"))
        await asyncio.wait_for(dispatched.wait(), timeout=1)
        self.assertEqual(len(adapter._slash_interactions), 1)
        await adapter.stop()
        self.assertTrue(forward_task.cancelled())
        self.assertEqual(len(adapter._slash_interactions), 0)
        adapter.router.stop.assert_awaited_once()
        self.assertEqual(channel.sent, [])

    async def test_join_delegates_same_channel_recovery_to_lifecycle_owner(self):
        voice_channel = SimpleNamespace(id=int(CHANNEL_ID), name="voice", members=[])
        voice_client = SimpleNamespace(channel=voice_channel)
        guild = SimpleNamespace(id=int(GUILD_ID), voice_client=voice_client)
        adapter = SimpleNamespace(
            is_context_allowed=Mock(return_value=True),
            is_voice_channel_allowed=Mock(return_value=True),
            forward_slash=AsyncMock(),
        )
        bot = SimpleNamespace(
            logger=Mock(),
            join_voice_channel=AsyncMock(return_value=("joined", None)),
        )
        cog = VoiceCog(bot, adapter)
        followup = AsyncMock()
        ctx = SimpleNamespace(
            interaction=SimpleNamespace(response=SimpleNamespace(is_done=Mock(return_value=True))),
            guild=guild,
            author=SimpleNamespace(
                id=int(USER_ID), voice=SimpleNamespace(channel=voice_channel)
            ),
            channel=FakeChannel(CHANNEL_ID, guild=guild),
            followup=SimpleNamespace(send=followup),
        )

        await VoiceCog.join_voice_channel.callback(cog, ctx)

        bot.join_voice_channel.assert_awaited_once_with(guild, voice_channel)
        self.assertIn("加入 voice", followup.await_args.args[0])
        adapter.forward_slash.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
