"""Select one compatible GPT/SoVITS weight pair for managed inference."""

from dataclasses import dataclass
from pathlib import Path


FALLBACK_GPT = "gsv-v2final-pretrained/s1bert25hz-5kh-longer-epoch=12-step=369668.ckpt"
FALLBACK_SOVITS = "gsv-v2final-pretrained/s2G2333k.pth"


@dataclass(frozen=True)
class GPTWeights:
    gpt: Path
    sovits: Path
    fallback: bool


def select_gpt_weights(config_dir: Path, gpt: str | Path | None, sovits: str | Path | None,
                       pretrained_dir: Path) -> GPTWeights:
    """Fall back as a pair; never mix a custom generation with the default one."""
    def resolve(value):
        if not value or not str(value).strip():
            return None
        path = Path(value).expanduser()
        return path.resolve() if path.is_absolute() else (config_dir / path).resolve()

    configured_gpt, configured_sovits = resolve(gpt), resolve(sovits)
    if configured_gpt and configured_gpt.is_file() and configured_sovits and configured_sovits.is_file():
        return GPTWeights(configured_gpt, configured_sovits, False)
    base_gpt = pretrained_dir / FALLBACK_GPT
    base_sovits = pretrained_dir / FALLBACK_SOVITS
    missing = [str(path) for path in (base_gpt, base_sovits) if not path.is_file()]
    if missing:
        raise FileNotFoundError("自定义 GPT/SoVITS 权重不完整，默认底模也缺失: " + ", ".join(missing))
    return GPTWeights(base_gpt.resolve(), base_sovits.resolve(), True)
