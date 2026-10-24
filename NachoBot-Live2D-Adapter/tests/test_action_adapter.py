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

    def test_directional_question_turns_toward_requested_side(self) -> None:
        decision = self.adapter.decide(
            question="你能看一下右边吗？",
            reply="我看看右边。",
        )

        self.assertEqual(decision.action_id, "TURN_RIGHT")
        self.assertEqual(decision.reason, "direction:right")

    def test_confirmation_uses_reply_polarity(self) -> None:
        accepted = self.adapter.decide(question="这样可以吗？", reply="当然可以。")
        rejected = self.adapter.decide(question="这样可以吗？", reply="不行，先等等。")

        self.assertEqual(accepted.action_id, "NOD")
        self.assertEqual(accepted.reason, "confirmation:yes")
        self.assertEqual(rejected.action_id, "SHAKE_HEAD")
        self.assertEqual(rejected.reason, "confirmation:no")


if __name__ == "__main__":
    unittest.main()
