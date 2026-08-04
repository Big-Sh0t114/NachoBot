import base64
import io
import math
import random

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from src.common.logger import get_logger
from src.plugin_system.apis.emoji_api import EmojiCandidate


logger = get_logger("emoji_collage")

TARGET_WIDTH = 1200
MAX_OUTPUT_EDGE = 1600
GAP = 8
PADDING = 12
MIN_ROW_HEIGHT = 150
MAX_ROW_HEIGHT = 430
MIN_LAYOUT_RATIO = 0.35
MAX_LAYOUT_RATIO = 3.0
BACKGROUND = (238, 238, 238)


@dataclass
class PreparedEmoji:
    candidate: EmojiCandidate
    image: Image.Image
    layout_ratio: float


@dataclass(frozen=True)
class EmojiCollageResult:
    image_base64: str
    image_format: str
    candidates: list[EmojiCandidate]
    width: int
    height: int


@dataclass
class _Layout:
    rows: list[list[PreparedEmoji]]
    row_heights: list[float]
    score: float


def _prepare_image(candidate: EmojiCandidate) -> PreparedEmoji | None:
    try:
        with Image.open(candidate.full_path) as source:
            frame_count = getattr(source, "n_frames", 1)
            if frame_count > 1:
                source.seek(frame_count // 2)
            frame = ImageOps.exif_transpose(source).convert("RGBA")
            frame.load()

        if frame.width <= 0 or frame.height <= 0:
            return None

        opaque = Image.new("RGBA", frame.size, (*BACKGROUND, 255))
        opaque.alpha_composite(frame)
        image = opaque.convert("RGB")
        real_ratio = image.width / image.height
        layout_ratio = min(max(real_ratio, MIN_LAYOUT_RATIO), MAX_LAYOUT_RATIO)
        return PreparedEmoji(candidate=candidate, image=image, layout_ratio=layout_ratio)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as error:
        logger.warning(f"跳过无法读取的表情 {Path(candidate.full_path).name}: {type(error).__name__}")
        return None
    except Exception as error:
        logger.warning(f"预处理表情失败 {Path(candidate.full_path).name}: {type(error).__name__}")
        return None


def _candidate_orders(items: list[PreparedEmoji]) -> list[list[PreparedEmoji]]:
    orders = [
        items,
        sorted(items, key=lambda item: item.layout_ratio),
        sorted(items, key=lambda item: item.layout_ratio, reverse=True),
        sorted(items, key=lambda item: abs(math.log(item.layout_ratio))),
    ]
    by_ratio = sorted(items, key=lambda item: item.layout_ratio)
    alternating: list[PreparedEmoji] = []
    left, right = 0, len(by_ratio) - 1
    while left <= right:
        alternating.append(by_ratio[right])
        right -= 1
        if left <= right:
            alternating.append(by_ratio[left])
            left += 1
    orders.append(alternating)

    rng = random.Random(0)
    for _ in range(3):
        shuffled = items.copy()
        rng.shuffle(shuffled)
        orders.append(shuffled)

    unique_orders: list[list[PreparedEmoji]] = []
    seen: set[tuple[str, ...]] = set()
    for order in orders:
        key = tuple(item.candidate.emoji_hash for item in order)
        if key not in seen:
            seen.add(key)
            unique_orders.append(order)
    return unique_orders


def _score_layout(rows: list[list[PreparedEmoji]], content_width: int) -> _Layout:
    row_heights = [(content_width - GAP * (len(row) - 1)) / sum(item.layout_ratio for item in row) for row in rows]
    canvas_height = 2 * PADDING + sum(row_heights) + GAP * (len(rows) - 1)
    canvas_width = content_width + 2 * PADDING
    aspect_penalty = abs(math.log(canvas_width / canvas_height))
    size_penalty = sum(
        max(0.0, MIN_ROW_HEIGHT - height) / MIN_ROW_HEIGHT + max(0.0, height - MAX_ROW_HEIGHT) / MAX_ROW_HEIGHT
        for height in row_heights
    )
    average_height = sum(row_heights) / len(row_heights)
    uneven_penalty = sum(abs(height - average_height) for height in row_heights) / (
        max(average_height, 1) * len(row_heights)
    )
    single_item_penalty = sum(0.08 for row in rows if len(row) == 1 and len(rows) > 1)
    return _Layout(
        rows=rows,
        row_heights=row_heights,
        score=aspect_penalty + size_penalty * 0.9 + uneven_penalty * 0.25 + single_item_penalty,
    )


def _find_best_layout(items: list[PreparedEmoji]) -> _Layout:
    best: _Layout | None = None
    split_count = len(items) - 1
    for order in _candidate_orders(items):
        for mask in range(1 << split_count):
            rows: list[list[PreparedEmoji]] = []
            start = 0
            for index in range(split_count):
                if mask & (1 << index):
                    rows.append(order[start : index + 1])
                    start = index + 1
            rows.append(order[start:])
            layout = _score_layout(rows, TARGET_WIDTH - 2 * PADDING)
            if best is None or layout.score < best.score:
                best = layout
    if best is None:
        raise ValueError("无法为表情包生成布局")
    return best


def _draw_badge(draw: ImageDraw.ImageDraw, x: int, y: int, number: int, image_height: int) -> None:
    badge_size = max(36, min(56, image_height // 4))
    margin = max(5, badge_size // 8)
    box = (x + margin, y + margin, x + margin + badge_size, y + margin + badge_size)
    draw.rounded_rectangle(box, radius=max(7, badge_size // 5), fill=(20, 20, 20), outline=(255, 255, 255), width=2)
    font = ImageFont.load_default(size=max(22, badge_size // 2))
    label = str(number)
    text_box = draw.textbbox((0, 0), label, font=font, stroke_width=1)
    text_width = text_box[2] - text_box[0]
    text_height = text_box[3] - text_box[1]
    text_x = box[0] + (badge_size - text_width) / 2
    text_y = box[1] + (badge_size - text_height) / 2 - text_box[1]
    draw.text((text_x, text_y), label, font=font, fill="white", stroke_width=1, stroke_fill="black")


def _render_item(item: PreparedEmoji, width: int, height: int) -> Image.Image:
    cell = Image.new("RGB", (width, height), BACKGROUND)
    fitted = ImageOps.contain(item.image, (width, height), Image.Resampling.LANCZOS)
    cell.paste(fitted, ((width - fitted.width) // 2, (height - fitted.height) // 2))
    return cell


def build_emoji_collage(
    candidates: Sequence[EmojiCandidate],
    limit: int = 10,
) -> EmojiCollageResult | None:
    """Build an in-memory numbered collage while preserving source aspect ratios."""
    prepared: list[PreparedEmoji] = []
    for candidate in candidates:
        item = _prepare_image(candidate)
        if item is not None:
            prepared.append(item)
        if len(prepared) >= limit:
            break

    if not prepared:
        return None

    layout = _find_best_layout(prepared)
    content_width = TARGET_WIDTH - 2 * PADDING
    canvas_height = round(2 * PADDING + sum(layout.row_heights) + GAP * (len(layout.rows) - 1))
    canvas = Image.new("RGB", (TARGET_WIDTH, canvas_height), BACKGROUND)
    draw = ImageDraw.Draw(canvas)
    displayed_candidates: list[EmojiCandidate] = []
    y = PADDING

    for row, raw_height in zip(layout.rows, layout.row_heights, strict=True):
        height = max(1, round(raw_height))
        widths = [max(1, round(height * item.layout_ratio)) for item in row]
        width_delta = content_width - GAP * (len(row) - 1) - sum(widths)
        widths[-1] += width_delta
        x = PADDING
        for item, width in zip(row, widths, strict=True):
            canvas.paste(_render_item(item, width, height), (x, y))
            displayed_candidates.append(item.candidate)
            _draw_badge(draw, x, y, len(displayed_candidates), height)
            x += width + GAP
        y += height + GAP

    if max(canvas.size) > MAX_OUTPUT_EDGE:
        canvas.thumbnail((MAX_OUTPUT_EDGE, MAX_OUTPUT_EDGE), Image.Resampling.LANCZOS)

    output = io.BytesIO()
    canvas.save(output, format="JPEG", quality=88, optimize=True)
    return EmojiCollageResult(
        image_base64=base64.b64encode(output.getvalue()).decode("ascii"),
        image_format="jpeg",
        candidates=displayed_candidates,
        width=canvas.width,
        height=canvas.height,
    )
