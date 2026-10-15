"""Isolated regression tests for the built-in help pattern formatter."""

import unittest

from src.plugins.built_in.help_plugin.pattern_display import format_pattern_for_display


class PatternDisplayTests(unittest.TestCase):
    def test_qqcall_literal_operations_expand_with_their_suffixes(self) -> None:
        pattern = (
            r"(?i)^[ 　]*(?:#(?P<operation>join|leave|stat)_vc_qq"
            r"|#qqcall\s+(?P<private_operation>accept|hangup|reject)"
            r"|\[语音：[ 　]*#join_vc_qq[ 　]*\])[ 　]*$"
        )
        self.assertEqual(
            format_pattern_for_display(pattern, "qqcall_control"),
            "#join_vc_qq/#leave_vc_qq/#stat_vc_qq/"
            "#qqcall accept/#qqcall hangup/#qqcall reject/[语音：#join_vc_qq]",
        )

    def test_group_muter_optional_at_prefix_is_omitted(self) -> None:
        pattern = r"^(?:\[CQ:at,[^\]]+\]\s*|@\S+\s*)*#mute_(?P<duration>\d+|true)\s*$"
        self.assertEqual(format_pattern_for_display(pattern, "mute_command"), "#mute_[duration]")

    def test_group_muter_default_command_is_clean(self) -> None:
        pattern = r"^(?:\[CQ:at,[^\]]+\]\s*|@\S+\s*)*#mute\s*$"
        self.assertEqual(format_pattern_for_display(pattern, "mute_default"), "#mute")

    def test_simple_case_insensitive_command(self) -> None:
        self.assertEqual(format_pattern_for_display(r"(?i)^#mute$", "mute"), "#mute")

    def test_named_variable_argument_uses_placeholder(self) -> None:
        self.assertEqual(format_pattern_for_display(r"^#lookup\s+(?P<user_id>\d+)\s*$"), "#lookup [user_id]")

    def test_nested_alternatives_expand_as_complete_triggers(self) -> None:
        self.assertEqual(
            format_pattern_for_display(r"^(?:#x|#(?:y|z))_(?:on|off)$", "mode"),
            "#x_on/#x_off/#y_on/#y_off/#z_on/#z_off",
        )

    def test_fullwidth_and_regex_whitespace_normalize(self) -> None:
        pattern = r"^[ 　]*#qqcall[ 　]+(?P<operation>accept|hangup)[ 　]*$"
        self.assertEqual(
            format_pattern_for_display(pattern, "qqcall_control"),
            "#qqcall accept/#qqcall hangup",
        )

    def test_escaped_punctuation_stays_literal(self) -> None:
        self.assertEqual(format_pattern_for_display(r"^#cmd\+literal\.\*$", "literal"), "#cmd+literal.*")

    def test_optional_arguments_are_marked_without_stray_spaces(self) -> None:
        summary_pattern = r"^#summary(?:\s+(?:(?P<qq>\d+)\s+)?(?P<date>\d{4}-\d{2}-\d{2}))?\s*$"
        self.assertEqual(format_pattern_for_display(summary_pattern, "summary"), "#summary [qq]? [date]?")
        self.assertEqual(format_pattern_for_display(r"^#topic(?:\s+(?P<topic>.+))?$"), "#topic [topic]?")

    def test_root_named_capture_keeps_management_trigger(self) -> None:
        pattern = r"(?P<manage_command>^/pm(\s[a-zA-Z0-9_]+)*\s*$)"
        self.assertEqual(format_pattern_for_display(pattern, "management"), "/pm [参数]?")

    def test_manual_angle_bracket_placeholders_are_preserved(self) -> None:
        pattern = "#bind_<平台>_<平台id/Discord昵称>"
        self.assertEqual(format_pattern_for_display(pattern), pattern)

    def test_angle_brackets_inside_unsupported_regex_do_not_bypass_fallback(self) -> None:
        self.assertEqual(format_pattern_for_display(r"^#foo(?=<x>)$", "foo_command"), "#foo_command")
        self.assertEqual(format_pattern_for_display("#foo.<id>", "foo_command"), "#foo_command")

    def test_malformed_regex_uses_registered_name(self) -> None:
        self.assertEqual(format_pattern_for_display(r"^(?P<broken>", "bad_pattern"), "#bad_pattern")

    def test_unrepresentable_pattern_uses_registered_name(self) -> None:
        self.assertEqual(format_pattern_for_display(r"^[A-Z]+$", "unusual_command"), "#unusual_command")

    def test_branch_expansion_is_bounded(self) -> None:
        alternatives = "|".join(f"#item{index}" for index in range(17))
        self.assertEqual(format_pattern_for_display(f"^(?:{alternatives})$", "many_options"), "#many_options")

    def test_existing_special_display_conventions_remain(self) -> None:
        self.assertEqual(format_pattern_for_display(r"(?i)^#lang_switch$", "language"), "#lang_switch")
        self.assertEqual(format_pattern_for_display(r"^(?:点歌|播放|来首)\s*(?P<query>.+)$", "music"), "点歌/播放/来首 + 关键词")


if __name__ == "__main__":
    unittest.main()
