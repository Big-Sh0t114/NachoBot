"""Whole-image preprocessing and duplicate fingerprints, adapted from Mai_recog_self."""

from pathlib import Path
import base64
import hashlib
import io
import math
import warnings
import uuid

from PIL import Image, ImageOps, ImageStat

MAX_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 40_000_000


def judgment(score: float) -> str:
    return "是自己" if score >= 0.6 else "不是自己" if score <= 0.4 else "不确定"


def decode(raw: bytes) -> Image.Image:
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError("文件为空或超过20MiB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                if source.format not in {"JPEG", "PNG", "WEBP"} or getattr(source, "n_frames", 1) != 1:
                    raise ValueError("仅支持JPEG/PNG/静态WebP")
                if source.width * source.height > MAX_PIXELS:
                    raise ValueError("解码超过4000万像素")
                source.load()
                return ImageOps.exif_transpose(source).convert("RGB")
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise ValueError("图片解码尺寸超限") from exc


def whole_image(image: Image.Image, edge: int = 320) -> Image.Image:
    image = image.copy()
    image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (edge, edge), (124, 116, 104))
    canvas.paste(image, ((edge - image.width) // 2, (edge - image.height) // 2))
    return canvas


def fingerprint(image: Image.Image) -> dict:
    # DCT pHash is invariant to re-encoding and resizing; no extra SciPy dependency.
    import numpy as np

    pixels = np.asarray(image.convert("L").resize((32, 32)), dtype=float)
    n = np.arange(32)
    basis = np.cos(np.pi / 32 * (n + 0.5)[None, :] * n[:, None])
    dct = (basis @ pixels @ basis.T)[:8, :8].flatten()
    bits = dct > np.median(dct[1:])
    return {
        "decoded": hashlib.sha256(str(image.size).encode() + image.tobytes()).hexdigest(),
        "phash": f"{sum(int(bit) << i for i, bit in enumerate(bits)):016x}",
        "aspect": image.width / image.height,
        "width": image.width,
        "height": image.height,
        "solid": max(ImageStat.Stat(image.resize((64, 64))).stddev) < 2,
    }


def similar(a: dict, b: dict) -> bool:
    return a["decoded"] == b["decoded"] or (
        (int(a["phash"], 16) ^ int(b["phash"], 16)).bit_count() <= 4 and abs(math.log(a["aspect"] / b["aspect"])) < 0.12
    )


def thumbnail(path: Path, cache: Path | None = None) -> str:
    if cache is not None and cache.is_file():
        return "data:image/jpeg;base64," + base64.b64encode(cache.read_bytes()).decode()
    image = decode(path.read_bytes())
    # Keep a 24-image page below the gateway's 512KiB response budget.
    for edge in (512, 384, 320, 256, 192, 128):
        preview = image.copy()
        preview.thumbnail((edge, edge))
        for quality in (80, 65, 50):
            buffer = io.BytesIO()
            preview.save(buffer, format="JPEG", quality=quality, optimize=True)
            if buffer.tell() <= 12_000:
                if cache is not None:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    temp = cache.with_suffix("." + uuid.uuid4().hex + ".tmp")
                    temp.write_bytes(buffer.getvalue())
                    temp.replace(cache)
                return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()
    raise ValueError("缩略图超过响应大小预算")
