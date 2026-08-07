"""情感分类器 — 基于 zero-shot classification 的文本情感自动判定

使用多语言 NLI 模型（mDeBERTa-v3-base）将文本分类到用户自定义的情感标签中。
模型在首次 classify() 调用时惰性加载，支持 FP16 半精度推理以节省显存。
"""

import logging
import os
from typing import List, Tuple

# Use the official endpoint by default; allow an explicit deployment override.
os.environ["HF_ENDPOINT"] = os.getenv("NACHOBOT_HF_ENDPOINT", "https://huggingface.co")

import torch
from transformers import pipeline as hf_pipeline
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

    def _ensure_loaded(self):
        """确保分类器已加载"""
        if self._classifier is not None:
            return

        logger.info(
            f"正在加载情感分类模型: {self._model_name} "
            f"(device={self._device}, fp16={self._use_fp16})"
        )

        # 确定是否启用 FP16
        use_fp16_effective = self._use_fp16 and self._device != "cpu"

        kwargs = {
            "task": "zero-shot-classification",
            "model": self._model_name,
            "device": self._device,
        }

        if use_fp16_effective:
            kwargs["dtype"] = torch.float16
            logger.info("已启用 FP16 半精度推理")
        elif self._use_fp16 and self._device == "cpu":
            logger.warning("CPU 设备不支持 FP16，自动回退到 FP32")

        model_short_name = self._model_name.split("/")[-1]
        logger.info(f"情感分类模型{model_short_name}将使用{self._device}")

        self._classifier = hf_pipeline(**kwargs)
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
