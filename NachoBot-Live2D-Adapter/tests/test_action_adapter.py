from __future__ import annotations

import unittest

from live2d_adapter.action_adapter import ActionAdapter


class ActionAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = ActionAdapter()

    def test_question_gets_thoughtful_motion(self) -> None:
        decision = self.adapter.decide(question="你为什么喜欢这个游戏？", reply="我觉得很有趣")

        self.assertEqual(decision.emotion, "normal")
        self.assertEqual(decision.action_id, "TILT_HEAD")
        self.assertEqual(decision.reason, "question")

    def test_emotion_gets_matching_motion(self) -> None:
        decision = self.adapter.decide(emotion="shy", reply="突然被夸有点不好意思")

        self.assertEqual(decision.emotion, "shy")
        self.assertEqual(decision.action_id, "LOOK_AWAY")

    def test_explicit_platform_label_is_normalized(self) -> None:
        decision = self.adapter.decide(
            emotion="happy",
            requested_action="身体晃动/开心/兴奋",
        )

        self.assertEqual(decision.emotion, "joy")
        self.assertEqual(decision.action_id, "HAPPY")
        self.assertEqual(decision.reason, "explicit_action")

    def test_negative_question_beats_generic_emotion_inference(self) -> None:
        decision = self.adapter.decide(question="你不喜欢这个吗？", reply="那我们换一个")

        self.assertEqual(decision.action_id, "TILT_HEAD")


if __name__ == "__main__":
    unittest.main()
