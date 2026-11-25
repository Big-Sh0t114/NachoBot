"""Small cluster-aware text fallback helpers for summary images.

Pillow's FreeType fonts do not expose a portable character coverage API. Here,
coverage is checked by comparing each scalar's rendered mask with an unassigned
Unicode scalar's missing-glyph mask. Fonts and these comparisons are cached.
"""

import os
import math
import unicodedata
from functools import lru_cache
from typing import Iterator, List, Tuple

from PIL import ImageDraw, ImageFont, features

from .constants import FontConfig


_MISSING_GLYPH_SAMPLE = "\u0378"  # unassigned in Unicode; renders as font .notdef
_ZWJ = "\u200d"
_VS15 = "\ufe0e"
_VS16 = "\ufe0f"
_HAS_RAQM = features.check("raqm")


@lru_cache(maxsize=256)
def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


@lru_cache(maxsize=8192)
def _font_supports(path: str, size: int, character: str) -> bool:
    """Return whether a font has a glyph distinct from its missing-glyph box."""
    try:
        font = _load_font(path, size)
        missing = font.getmask(_MISSING_GLYPH_SAMPLE)
        glyph = font.getmask(character)
        return glyph.size != missing.size or bytes(glyph) != bytes(missing)
    except Exception:
        return False


def _font_path(font: ImageFont.FreeTypeFont) -> str:
    path = getattr(font, "path", None)
    return os.path.normcase(os.path.abspath(os.fspath(path))) if path else ""


def _is_variation_selector(character: str) -> bool:
    codepoint = ord(character)
    return 0xFE00 <= codepoint <= 0xFE0F or 0xE0100 <= codepoint <= 0xE01EF


def _is_tag_character(character: str) -> bool:
    codepoint = ord(character)
    return 0xE0020 <= codepoint <= 0xE007F


def _display_text(text: str) -> str:
    """Avoid visible shaping-control tofu when Pillow cannot shape sequences.

    The original string stays intact for wrapping and API inputs. On builds
    without RAQM, draw the component glyphs and omit only joiners and tag
    controls that otherwise render as stray boxes in common fonts. Meaningful
    components such as skin-tone modifiers remain in the drawn text.
    """
    if _HAS_RAQM:
        return text
    return "".join(
        character
        for character in text
        if character != _ZWJ
        and not _is_tag_character(character)
    )


def _is_regional_indicator(character: str) -> bool:
    return 0x1F1E6 <= ord(character) <= 0x1F1FF


def _is_cluster_extension(character: str) -> bool:
    codepoint = ord(character)
    return (
        unicodedata.category(character).startswith("M")
        or 0x1F3FB <= codepoint <= 0x1F3FF  # emoji skin tones
        or _is_variation_selector(character)
        or _is_tag_character(character)
    )


def grapheme_clusters(text: str) -> Iterator[str]:
    """Yield common user-visible grapheme clusters without splitting emoji.

    This deliberately implements the sequences needed by names and chat text
    (combining marks, variation selectors, skin tones, tags, ZWJ sequences,
    and paired regional indicators) without adding a regex/UAX dependency.
    """
    cluster = ""
    for character in text:
        if not cluster:
            cluster = character
            continue

        regional_count = sum(1 for item in cluster if _is_regional_indicator(item))
        joins = (
            _is_cluster_extension(character)
            or character == _ZWJ
            or cluster.endswith(_ZWJ)
            or (character == "\n" and cluster.endswith("\r"))
            or (_is_regional_indicator(character) and regional_count == 1)
        )
        if joins:
            cluster += character
        else:
            yield cluster
            cluster = character

    if cluster:
        yield cluster


def _ignored_for_coverage(character: str) -> bool:
    # These codepoints affect cluster presentation but should not independently
    # force a fallback font (many fonts intentionally have no visible ZWJ glyph).
    return (
        character == _ZWJ
        or _is_variation_selector(character)
        or _is_tag_character(character)
        or unicodedata.category(character) in {"Cc", "Cf", "Cs"}
    )


def _is_emoji_cluster(cluster: str) -> bool:
    if _VS16 in cluster or _ZWJ in cluster or any(_is_regional_indicator(c) for c in cluster):
        return True
    for character in cluster:
        codepoint = ord(character)
        if 0x1F000 <= codepoint <= 0x1FAFF or 0x1F3FB <= codepoint <= 0x1F3FF:
            return True
        if character in "#*0123456789" and "\u20e3" in cluster:
            return True
    return False


def _fallback_paths(cluster: str) -> List[str]:
    configured = list(dict.fromkeys(FontConfig.FALLBACK_FONT_PATHS))
    if _is_emoji_cluster(cluster):
        preferred = FontConfig.EMOJI_FONT_PATHS + FontConfig.SYMBOL_FONT_PATHS
    else:
        preferred = FontConfig.SYMBOL_FONT_PATHS + FontConfig.EMOJI_FONT_PATHS
    ordered = [path for path in preferred if path in configured]
    ordered.extend(path for path in configured if path not in ordered)
    return ordered


def _supports_character(font: ImageFont.FreeTypeFont, character: str) -> bool:
    path = _font_path(font)
    if not path:
        try:
            missing = font.getmask(_MISSING_GLYPH_SAMPLE)
            glyph = font.getmask(character)
            return glyph.size != missing.size or bytes(glyph) != bytes(missing)
        except Exception:
            return False
    return _font_supports(path, int(font.size), character)


def _choose_font(cluster: str, primary: ImageFont.FreeTypeFont) -> ImageFont.FreeTypeFont:
    characters = [character for character in cluster if not _ignored_for_coverage(character)]
    if not characters:
        return primary

    primary_score = sum(_supports_character(primary, character) for character in characters)
    if primary_score == len(characters):
        return primary

    candidates = [primary]
    primary_path = _font_path(primary)
    for path in _fallback_paths(cluster):
        normalized_path = os.path.normcase(os.path.abspath(path))
        if normalized_path == primary_path or not os.path.isfile(path):
            continue
        try:
            candidates.append(_load_font(path, int(primary.size)))
        except Exception:
            continue

    best_font = primary
    best_score = primary_score
    for font in candidates:
        score = sum(_supports_character(font, character) for character in characters)
        if score == len(characters):
            return font
        if score > best_score:
            best_font = font
            best_score = score

    # Keep unknown codepoints on the configured primary if every font reports
    # the same incomplete coverage; the original name remains untouched.
    return best_font


def font_runs(
    text: str, primary: ImageFont.FreeTypeFont
) -> List[Tuple[ImageFont.FreeTypeFont, str]]:
    """Group adjacent grapheme clusters that use the same font."""
    runs: List[Tuple[ImageFont.FreeTypeFont, str]] = []
    for cluster in grapheme_clusters(text):
        font = _choose_font(cluster, primary)
        if runs and _font_path(runs[-1][0]) == _font_path(font):
            previous_font, previous_text = runs[-1]
            runs[-1] = (previous_font, previous_text + cluster)
        else:
            runs.append((font, cluster))
    return runs


def text_width(text: str, primary: ImageFont.FreeTypeFont) -> int:
    """Measure using the same font runs and advances as :func:`draw_text`."""
    width = 0.0
    for font, run_text in font_runs(text, primary):
        render_text = _display_text(run_text)
        if not render_text:
            continue
        try:
            width += font.getlength(render_text)
        except AttributeError:
            bbox = font.getbbox(render_text)
            width += bbox[2] - bbox[0]
    # Layout callers use this result for Pillow drawing and integer geometry.
    # Round outward so it never underestimates the fractional pen advance.
    return math.ceil(width)


def draw_text(
    draw: ImageDraw.ImageDraw,
    position: Tuple[float, float],
    text: str,
    primary: ImageFont.FreeTypeFont,
    fill,
    *,
    embedded_color: bool = False,
) -> None:
    """Draw fallback runs with baseline alignment and matching advances."""
    runs = font_runs(text, primary)
    if not runs:
        return

    render_text = _display_text(text)
    if not render_text:
        return

    if len(runs) == 1 and runs[0][0] is primary:
        draw.text(position, render_text, fill=fill, font=primary, embedded_color=embedded_color)
        return

    x, y = position
    primary_ascent = primary.getmetrics()[0]
    for font, run_text in runs:
        render_text = _display_text(run_text)
        if not render_text:
            continue
        ascent = font.getmetrics()[0]
        draw.text(
            (x, y + primary_ascent - ascent),
            render_text,
            fill=fill,
            font=font,
            embedded_color=embedded_color,
        )
        try:
            x += font.getlength(render_text)
        except AttributeError:
            bbox = font.getbbox(render_text)
            x += bbox[2] - bbox[0]
