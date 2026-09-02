from __future__ import annotations

import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path


class _SetupMarkupParser(HTMLParser):
    _VOID_TAGS = frozenset(
        {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[tuple[str, bool]] = []
        self._discord_depth = 0
        self.portal_links: list[dict[str, str]] = []
        self.bilibili_sections: list[dict[str, str]] = []
        self.bilibili_inputs: list[dict[str, str]] = []
        self.discord_inputs: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        is_discord_section = (
            tag == "div" and attributes.get("id") == "setup-discord-section"
        )
        if tag not in self._VOID_TAGS:
            self._stack.append((tag, is_discord_section))
        if is_discord_section:
            self._discord_depth += 1
        if tag == "a" and self._discord_depth:
            self.portal_links.append(attributes)
        if tag == "input" and self._discord_depth and attributes.get("id") == "setup-discord-token":
            self.discord_inputs.append(attributes)
        if tag == "div" and attributes.get("id") == "setup-bilibili-section":
            self.bilibili_sections.append(attributes)
        if tag == "input" and attributes.get("id") == "setup-bilibili-bot-account":
            self.bilibili_inputs.append(attributes)

    def handle_endtag(self, tag: str) -> None:
        if not self._stack:
            return
        open_tag, is_discord_section = self._stack.pop()
        if open_tag == tag and is_discord_section:
            self._discord_depth -= 1


class SetupFrontendSecretContractTests(unittest.TestCase):
    def test_setup_markup_contract(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        markup = (repo_root / "webUI" / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        parser = _SetupMarkupParser()
        parser.feed(markup)
        parser.close()

        self.assertEqual(len(parser.portal_links), 1)
        portal = parser.portal_links[0]
        self.assertEqual(
            portal.get("href"), "https://discord.com/developers/applications"
        )
        self.assertEqual(portal.get("target"), "_blank")
        rel_tokens = set(portal.get("rel", "").split())
        self.assertIn("noopener", rel_tokens)
        self.assertIn("noreferrer", rel_tokens)

        self.assertEqual(len(parser.discord_inputs), 1)
        discord_input = parser.discord_inputs[0]
        self.assertEqual(discord_input.get("id"), "setup-discord-token")
        self.assertEqual(discord_input.get("type"), "password")
        self.assertIn("required", discord_input)

        self.assertEqual(len(parser.bilibili_sections), 1)
        self.assertEqual(len(parser.bilibili_inputs), 1)
        bilibili_input = parser.bilibili_inputs[0]
        self.assertEqual(bilibili_input.get("type"), "text")
        self.assertEqual(bilibili_input.get("inputmode"), "numeric")
        self.assertEqual(bilibili_input.get("pattern"), "[0-9]+")
        self.assertIn("required", bilibili_input)

    def test_frontend_secret_contract(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        fixture = repo_root / "webUI" / "tests" / "frontend_secret_contract.test.js"
        result = subprocess.run(
            ["node", str(fixture)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, "frontend contract fixture failed")


if __name__ == "__main__":
    unittest.main()
