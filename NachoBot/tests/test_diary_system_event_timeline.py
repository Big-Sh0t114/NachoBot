import json
import unittest
from unittest.mock import patch

from ncnk_message import build_system_event
from src.common.data_models.database_data_model import DatabaseMessages


class DiarySystemEventTimelineTests(unittest.TestCase):
    @staticmethod
    def _message(
        message_id: str,
        text: str,
        *,
        user_id: str = "",
        nickname: str = "",
        additional_config: str | None = None,
    ) -> DatabaseMessages:
        return DatabaseMessages(
            message_id=message_id,
            time=1.0,
            chat_id="qq-group",
            processed_plain_text=text,
            additional_config=additional_config,
            user_id=user_id,
            user_nickname=nickname,
            user_platform="qq" if user_id else "",
            chat_info_stream_id="qq-group",
            chat_info_platform="qq",
            chat_info_group_id="group-1",
            chat_info_group_name="测试群",
            chat_info_group_platform="qq",
        )

    def test_senderless_system_event_is_environment_context_not_user_message(self):
        from plugins.diary_plugin.core.diary_service import DiaryService
        import plugins.diary_plugin.core.diary_service as diary_service_module

        event = build_system_event(
            "qq.poke",
            actor={"user_id": "123", "name": "测试用户"},
            target={"user_id": "999", "name": "NachoBot"},
        )
        messages = [
            self._message(
                "system-event",
                "用鼠标戳了戳你",
                additional_config=json.dumps({"system_event": event}, ensure_ascii=False),
            ),
            self._message("ordinary", "晚上好", user_id="456", nickname="群友"),
        ]

        service = DiaryService()
        with patch.object(diary_service_module.config_api, "get_global_config", return_value="999"):
            timeline = service.build_chat_timeline(messages)

        self.assertIn("[系统事件] 测试用户用鼠标戳了戳你", timeline)
        self.assertIn("群友: 晚上好", timeline)
        self.assertNotIn("某人: 用鼠标戳了戳你", timeline)
        self.assertEqual(service._timeline_stats["system_events"], 1)
        self.assertEqual(service._timeline_stats["user_messages"], 1)

    def test_senderless_non_event_row_is_skipped_without_crashing(self):
        from plugins.diary_plugin.core.diary_service import DiaryService
        import plugins.diary_plugin.core.diary_service as diary_service_module

        service = DiaryService()
        senderless_row = self._message("senderless", "不应伪装成用户消息")
        with patch.object(diary_service_module.config_api, "get_global_config", return_value="999"):
            timeline = service.build_chat_timeline([senderless_row])

        self.assertNotIn("不应伪装成用户消息", timeline)
        self.assertEqual(service._timeline_stats["skipped_senderless"], 1)

    def test_legacy_action_timeline_delegates_to_shared_senderless_logic(self):
        from plugins.diary_plugin.core.actions import DiaryGeneratorAction
        from plugins.diary_plugin.core.diary_service import DiaryService

        event = build_system_event("qq.notice", actor={"name": "系统管理员"})
        action = DiaryGeneratorAction.__new__(DiaryGeneratorAction)
        action.diary_service = DiaryService()
        message = self._message(
            "legacy-action-event",
            "更新了群设置",
            additional_config=json.dumps({"system_event": event}, ensure_ascii=False),
        )

        timeline = action.build_chat_timeline([message])

        self.assertIn("[系统事件] 系统管理员更新了群设置", timeline)
        self.assertEqual(action._timeline_stats["system_events"], 1)


if __name__ == "__main__":
    unittest.main()
