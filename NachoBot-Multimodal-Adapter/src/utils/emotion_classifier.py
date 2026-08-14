"""情感分类器 — 基于 zero-shot classification 的文本情感自动判定

使用多语言 NLI 模型（mDeBERTa-v3-base）将文本分类到用户自定义的情感标签中。
模型在首次 classify() 调用时惰性加载，支持 FP16 半精度推理以节省显存。
"""

import logging
import os
import threading
from typing import List, Tuple

# Configure Hugging Face before Transformers is imported. Prefer predictable
# HTTP downloads on mainland-China networks and honour existing deployment
# overrides instead of forcing the official Hub.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "10")
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")


def _hf_endpoints() -> list[str]:
    endpoints: list[str] = []
    for env_name in ("NACHOBOT_HF_ENDPOINT", "HF_ENDPOINT"):
        endpoint = os.environ.get(env_name, "").strip().rstrip("/")
        if endpoint:
            endpoints.append(endpoint)
    endpoints.extend(("https://hf-mirror.com", "https://huggingface.co"))
    return list(dict.fromkeys(endpoints))

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer, pipeline as hf_pipeline
from nachobot_multimodal.logger import logger

# 抑制 transformers pipeline 默认在 CPU 时的警告 "Device set to use cpu"
logging.getLogger("transformers.pipelines.base").setLevel(logging.ERROR)


class EmotionClassifier:
    """基于 zero-shot classification 的情感分类器。

    使用多语言 NLI 模型将文本分类到用户自定义的情感标签中。
    模型在首次 classify() 调用时惰性加载。

    Args:
        model_name: HuggingFace 模型 ID 或本地路径
        device: 推理设备 ("cpu" / "cuda:0" 等)
        use_fp16: 是否使用 FP16 半精度推理（仅 CUDA 设备有效）
    """

    def __init__(
        self,
        model_name: str = "MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7",
        device: str = "cpu",
        use_fp16: bool = True,
    ):
        self._model_name = model_name
        self._device = device
        self._use_fp16 = use_fp16
        self._classifier = None  # 惰性加载
        self._load_lock = threading.Lock()

    def _ensure_loaded(self):
        """确保分类器已加载；Hub 不可达时自动回退本地缓存。"""
        if self._classifier is not None:
            return

        with self._load_lock:
            if self._classifier is not None:
                return

            logger.info(
                f"正在加载情感分类模型: {self._model_name} "
                f"(device={self._device}, fp16={self._use_fp16})"
            )

            use_fp16_effective = self._use_fp16 and self._device != "cpu"
            model_kwargs = {}
            if use_fp16_effective:
                model_kwargs["dtype"] = torch.float16
                logger.info("已启用 FP16 半精度推理")
            elif self._use_fp16 and self._device == "cpu":
                logger.warning("CPU 设备不支持 FP16，自动回退到 FP32")

            model_short_name = self._model_name.split("/")[-1]
            logger.info(f"情感分类模型{model_short_name}将使用{self._device}")

            try:
                logger.info("优先从本地缓存加载情感分类模型")
                tokenizer = AutoTokenizer.from_pretrained(
                    self._model_name,
                    local_files_only=True,
                )
                model = AutoModelForSequenceClassification.from_pretrained(
                    self._model_name,
                    local_files_only=True,
                    **model_kwargs,
                )
                logger.info("情感分类模型已从本地缓存加载")
            except Exception as cache_exc:
                logger.warning("本地情感分类模型缓存不可用: %s", cache_exc)

                from huggingface_hub import snapshot_download

                failures: list[str] = []
                snapshot_dir = None
                for endpoint in _hf_endpoints():
                    try:
                        logger.info("尝试通过 %s 下载情感分类模型", endpoint)
                        snapshot_dir = snapshot_download(
                            repo_id=self._model_name,
                            endpoint=endpoint,
                        )
                        logger.info("情感分类模型已通过 %s 下载完成", endpoint)
                        break
                    except Exception as exc:
                        failures.append(f"{endpoint}: {exc}")
                        logger.warning("通过 %s 下载情感分类模型失败: %s", endpoint, exc)

                if snapshot_dir is None:
                    raise RuntimeError(
                        "无法下载情感分类模型；已尝试自定义端点、hf-mirror.com 和 huggingface.co。"
                        + " | ".join(failures)
                    )

                tokenizer = AutoTokenizer.from_pretrained(
                    snapshot_dir,
                    local_files_only=True,
                )
                model = AutoModelForSequenceClassification.from_pretrained(
                    snapshot_dir,
                    local_files_only=True,
                    **model_kwargs,
                )
                logger.info("情感分类模型已从下载完成的本地快照加载")

            self._classifier = hf_pipeline(
                task="zero-shot-classification",
                model=model,
                tokenizer=tokenizer,
                device=self._device,
            )
            logger.info("情感分类模型加载完成")

    def classify(
        self,
        text: str,
        available_tags: List[str],
        hypothesis_template: str = "这段文字表达的情感是{}。",
    ) -> Tuple[str, float]:
        """对文本进行情感分类。

        Args:
            text: 待分类的文本
            available_tags: 可用的情感标签列表
            hypothesis_template: NLI 假设模板（{} 会被替换为标签名）

        Returns:
            (best_tag, confidence): 最佳匹配的标签和置信度 (0.0~1.0)
        """
        self._ensure_loaded()

        result = self._classifier(
            text,
            candidate_labels=available_tags,
            hypothesis_template=hypothesis_template,
        )

        best_tag = result["labels"][0]
        confidence = result["scores"][0]

        logger.debug(
            f"情感分类: '{text[:50]}...' → {best_tag} ({confidence:.3f}) "
            f"| 全部: {dict(zip(result['labels'], [f'{s:.3f}' for s in result['scores']], strict=False))}"
        )

        return best_tag, confidence
