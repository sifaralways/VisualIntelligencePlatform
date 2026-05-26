from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

from backend.config import settings

logger = logging.getLogger(__name__)

_CAPTION_TASK = "<MORE_DETAILED_CAPTION>"
_OCR_TASK = "<OCR>"
_REGION_TASK = "<DENSE_REGION_CAPTION>"

_CAPTION_MAX_CHARS = 600
_TEXT_LINE_MAX_CHARS = 240
_MAX_LINES = 8

_CAPTION_MAX_NEW_TOKENS = 256
_OCR_MAX_NEW_TOKENS = 128
_REGION_MAX_NEW_TOKENS = 192
_NUMERIC_ONLY_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")


@dataclass
class FlorenceResult:
    caption: str = ""
    ocr_lines: list[str] = field(default_factory=list)
    region_descriptions: list[str] = field(default_factory=list)


class FlorenceAnalyzer:
    """Optional Florence-2 enrichment for richer caption and OCR signals."""

    def __init__(self) -> None:
        self._available = False
        self._processor = None
        self._model = None
        self._device = "cpu"

    @property
    def available(self) -> bool:
        return self._available

    def load(self) -> None:
        if self._available:
            return

        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoProcessor

            self._device = "mps" if torch.backends.mps.is_available() else "cpu"
            logger.info("Loading Florence model %s (device=%s) ...", settings.florence_model, self._device)
            self._processor = AutoProcessor.from_pretrained(settings.florence_model, trust_remote_code=True)
            self._model = AutoModelForCausalLM.from_pretrained(
                settings.florence_model,
                trust_remote_code=True,
            ).to(self._device)
            self._model.eval()
            self._available = True
            logger.info("Florence analyzer ready")
        except ImportError as exc:
            logger.warning(
                "Florence disabled; missing dependency (%s). "
                "Install/verify transformers, sentencepiece, and einops.",
                exc,
            )
        except Exception as exc:
            logger.warning("Florence disabled; failed to load model: %s", exc)

    def analyze(self, image_path: Path) -> FlorenceResult:
        if not self._available or self._model is None or self._processor is None:
            return FlorenceResult()
        if not image_path.exists():
            return FlorenceResult()

        try:
            with Image.open(image_path).convert("RGB") as image:
                caption = self._normalize_caption(
                    self._run_task(
                        image,
                        _CAPTION_TASK,
                        max_new_tokens=_CAPTION_MAX_NEW_TOKENS,
                    )
                )
                ocr_lines = self._normalize_lines(
                    self._run_task(
                        image,
                        _OCR_TASK,
                        max_new_tokens=_OCR_MAX_NEW_TOKENS,
                    )
                )
                region_descriptions = self._normalize_lines(
                    self._run_task(
                        image,
                        _REGION_TASK,
                        max_new_tokens=_REGION_MAX_NEW_TOKENS,
                    ),
                    drop_numeric_only=True,
                )
            return FlorenceResult(
                caption=caption,
                ocr_lines=ocr_lines,
                region_descriptions=region_descriptions,
            )
        except Exception as exc:
            logger.warning("Florence inference failed for %s: %s", image_path.name, exc)
            return FlorenceResult()

    def analyze_batch(self, image_paths: list[Path]) -> list[FlorenceResult]:
        return [self.analyze(image_path) for image_path in image_paths]

    def _run_task(self, image: Image.Image, task_prompt: str, max_new_tokens: int = 128) -> Any:
        import torch

        inputs = self._processor(text=task_prompt, images=image, return_tensors="pt")
        device_inputs = {
            key: value.to(self._device) if hasattr(value, "to") else value
            for key, value in inputs.items()
        }
        with torch.no_grad():
            generated_ids = self._model.generate(
                input_ids=device_inputs.get("input_ids"),
                pixel_values=device_inputs.get("pixel_values"),
                max_new_tokens=max_new_tokens,
                num_beams=3,
                do_sample=False,
            )
        generated_text = self._processor.batch_decode(generated_ids, skip_special_tokens=False)[0]
        try:
            return self._processor.post_process_generation(
                generated_text,
                task=task_prompt,
                image_size=(image.width, image.height),
            )
        except Exception:
            return generated_text

    @staticmethod
    def _flatten_text(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            out: list[str] = []
            for inner in value.values():
                out.extend(FlorenceAnalyzer._flatten_text(inner))
            return out
        if isinstance(value, (list, tuple, set)):
            out: list[str] = []
            for inner in value:
                out.extend(FlorenceAnalyzer._flatten_text(inner))
            return out
        return [str(value)]

    def _normalize_caption(self, value: Any) -> str:
        lines = self._normalize_lines(value, max_chars=_CAPTION_MAX_CHARS)
        return lines[0] if lines else ""

    def _normalize_lines(
        self,
        value: Any,
        max_chars: int = _TEXT_LINE_MAX_CHARS,
        drop_numeric_only: bool = False,
    ) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in self._flatten_text(value):
            text = str(raw or "").strip()
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip(" .,:;|\n\t")
            if len(text) < 2:
                continue
            if drop_numeric_only and _NUMERIC_ONLY_RE.fullmatch(text):
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(text[:max_chars])
        return cleaned[:_MAX_LINES]