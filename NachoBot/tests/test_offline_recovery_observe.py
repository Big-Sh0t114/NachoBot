import asyncio
import json
import re
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from test_offline_recovery_runtime import (
    OfflineRecoveryRuntimeTests,
    _recovery_messages,
    brain_module,
    heart_module,
    processor_module,
)
from src.chat.brain_chat.brain_chat import BrainChatting
from src.chat.heart_flow.heartFC_chat import HeartFChatting
from src.common.data_models.database_data_model import DatabaseMessages


class OfflineRecoveryObserveTests(unittest.TestCase):
    def test_mixed_planner_history_only_maps_actual_recovery_rows(self):
        for runtime_type, module, suffix in (
            (BrainChatting, brain_module, "brain"),
            (HeartFChatting, heart_module, "heartflow"),
        ):
            chat_id = f"offline-observe-mixed-history-{suffix}"
            recovered = _recovery_messages(chat_id, 2)
            recovered[1].time = recovered[0].time
            second_metadata = json.loads(recovered[1].additional_config)
            second_metadata["offline_recovery"]["original_timestamp"] = recovered[0].time
            recovered[1].additional_config = json.dumps(second_metadata)
            ordinary_with_unrelated_row_id = DatabaseMessages(
                message_id="ordinary-with-row-id",
                time=recovered[0].time - 2,
                chat_id=chat_id,
                processed_plain_text="ordinary history with a nonbatch row id",
                user_id="sender-1",
                user_nickname="Sender",
                user_platform="qq",
                chat_info_stream_id=chat_id,
                chat_info_platform="qq",
            )
            ordinary_with_unrelated_row_id.focus_row_id = 700
            ordinary_without_row_id = DatabaseMessages(
                message_id="ordinary-without-row-id",
                time=recovered[0].time - 1,
                chat_id=chat_id,
                processed_plain_text="ordinary history without a row id",
                user_id="sender-1",
                user_nickname="Sender",
                user_platform="qq",
                chat_info_stream_id=chat_id,
                chat_info_platform="qq",
            )
            # DatabaseMessages instances can compare equal regardless of row
            # contents. The production _observe path must match the batch IDs.
            self.assertEqual(ordinary_with_unrelated_row_id, recovered[0])
            formatted_batches = []
            formatter = module.format_recovery_batch_context

            def capture_recovery_block(*args, _formatter=formatter, _batches=formatted_batches, **kwargs):
                rendered = _formatter(*args, **kwargs)
                _batches.append(rendered)
                return rendered

            with patch.object(module, "format_recovery_batch_context", side_effect=capture_recovery_block):
                OfflineRecoveryRuntimeTests()._exercise_recovery_turn(
                    runtime_type,
                    module,
                    chat_id,
                    messages=recovered,
                    raw_history_rows=[ordinary_with_unrelated_row_id, ordinary_without_row_id],
                )

            rows = re.findall(
                r'<message index="\d+" planner_id="([^"]+)"[^>]* original_timestamp="([^"]+)"',
                formatted_batches[-1],
            )
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0][1], f"{recovered[0].time:.6f}")
            self.assertEqual(rows[1][1], f"{recovered[1].time:.6f}")
            self.assertNotEqual(rows[0][0], rows[1][0])


class OfflineRecoveryLogTests(unittest.TestCase):
    @staticmethod
    def _inbound_message(*, text: str, additional_config: dict, user: bool = True):
        user_info = (
            SimpleNamespace(user_id="sender-1", user_nickname="Sender", user_cardname=None) if user else None
        )
        chat = SimpleNamespace(stream_id="qq_private_sender-1", group_info=None)
        message_info = SimpleNamespace(
            platform="qq",
            user_info=user_info,
            group_info=None,
            additional_config=additional_config,
        )
        return SimpleNamespace(
            processed_plain_text=text,
            message_info=message_info,
            chat_stream=chat,
            is_picid=False,
            is_emoji=False,
            is_voice=False,
        )

    def test_original_time_is_visible_for_recovery_without_changing_normal_or_event_logs(self):
        original_timestamp = 1_791_061_416
        receive_timestamp = 1_791_061_463.685
        recovered_message = self._inbound_message(
            text="recovered text",
            additional_config={
                "offline_recovery": {
                    "version": 1,
                    "original_timestamp": original_timestamp,
                    "connection_epoch_started_at": receive_timestamp + 100,
                    "history_run": {"run_id": "run-1", "cutoff_at": receive_timestamp},
                }
            },
        )
        ordinary_message = self._inbound_message(text="ordinary text", additional_config={})
        event_message = self._inbound_message(
            text="管理员开启了全体禁言",
            additional_config={
                "system_event": {
                    "version": 1,
                    "type": "qq.group_admin",
                    "actor": {"user_id": "42", "name": "管理员"},
                    "target": None,
                    "data": {"group_id": "77", "sub_type": "set"},
                }
            },
            user=False,
        )
        receiver = object.__new__(processor_module.HeartFCMessageReceiver)
        receiver.storage = SimpleNamespace(store_message=AsyncMock(return_value=SimpleNamespace(chat_id="qq_private_sender-1")))
        heartflow_chat = SimpleNamespace(signal_new_message=Mock())

        async def process(message):
            receiver.storage.store_message.reset_mock()
            receiver.storage.store_message.return_value = SimpleNamespace(chat_id=message.chat_stream.stream_id)
            with (
                patch.object(processor_module, "_calculate_interest", new=AsyncMock(return_value=(1.0, []))),
                patch.object(processor_module.offline_recovery_manager, "record_stored_message", return_value=False),
                patch.object(processor_module.offline_recovery_manager, "record_deferred_live_message"),
                patch.object(processor_module.offline_recovery_manager, "is_held", return_value=False),
                patch.object(
                    processor_module.focus_coordinator,
                    "route_message",
                    new=AsyncMock(return_value=SimpleNamespace(managed=False)),
                ),
                patch.object(
                    processor_module.heartflow,
                    "get_or_create_heartflow_chat",
                    new=AsyncMock(return_value=heartflow_chat),
                ),
                patch.object(processor_module.global_config.mood, "enable_mood", False),
                patch.object(processor_module, "replace_user_references", side_effect=lambda text, *_args, **_kwargs: text),
                patch.object(processor_module.Person, "register_person"),
                patch.object(processor_module.logger, "info") as log_info,
            ):
                await receiver.process_message(message)
                return [call.args[0] for call in log_info.call_args_list]

        recovered_logs = asyncio.run(process(recovered_message))
        ordinary_logs = asyncio.run(process(ordinary_message))
        event_logs = asyncio.run(process(event_message))

        expected_original = datetime.fromtimestamp(original_timestamp).astimezone().isoformat(
            sep=" ", timespec="seconds"
        )
        self.assertIn(f"[原消息时间 {expected_original}][私聊]Sender:recovered text", recovered_logs)
        self.assertIn("[私聊]Sender:ordinary text", ordinary_logs)
        self.assertNotIn("原消息时间", "".join(ordinary_logs))
        self.assertEqual(event_logs, ["[私聊][系统事件] 管理员开启了全体禁言"])


if __name__ == "__main__":
    unittest.main()
