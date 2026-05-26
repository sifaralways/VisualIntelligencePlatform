from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

from backend.config import settings
from backend.database.settings_store import get as get_setting

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
_DEFAULT_INFERENCE_BATCH_SIZE = 8
_DEFAULT_NUM_BEAMS = 1


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
        if not image_paths:
            return []
        if not self._available or self._model is None or self._processor is None:
            return [FlorenceResult() for _ in image_paths]

        results = [FlorenceResult() for _ in image_paths]
        valid_entries = self._load_valid_entries(image_paths)

        if not valid_entries:
            return results

        batch_size = max(1, int(get_setting("florence_inference_batch_size") or _DEFAULT_INFERENCE_BATCH_SIZE))
        num_beams = max(1, int(get_setting("florence_num_beams") or _DEFAULT_NUM_BEAMS))
        for start in range(0, len(valid_entries), batch_size):
            chunk = valid_entries[start : start + batch_size]
            self._analyze_chunk(chunk, image_paths, results, num_beams)

        return results

    def _load_valid_entries(self, image_paths: list[Path]) -> list[tuple[int, Image.Image, tuple[int, int]]]:
        valid_entries: list[tuple[int, Image.Image, tuple[int, int]]] = []
        for idx, image_path in enumerate(image_paths):
            if not image_path.exists():
                continue
            try:
                with Image.open(image_path).convert("RGB") as img:
                    copy = img.copy()
                    valid_entries.append((idx, copy, (copy.width, copy.height)))
            except Exception as exc:
                logger.warning("Florence image load failed for %s: %s", image_path.name, exc)
        return valid_entries

    def _analyze_chunk(
        self,
        chunk: list[tuple[int, Image.Image, tuple[int, int]]],
        image_paths: list[Path],
        results: list[FlorenceResult],
        num_beams: int,
    ) -> None:
        indices = [entry[0] for entry in chunk]
        images = [entry[1] for entry in chunk]
        sizes = [entry[2] for entry in chunk]
        try:
            caption_values = self._run_task_batch(
                images,
                sizes,
                _CAPTION_TASK,
                max_new_tokens=_CAPTION_MAX_NEW_TOKENS,
                num_beams=num_beams,
            )
            ocr_values = self._run_task_batch(
                images,
                sizes,
                _OCR_TASK,
                max_new_tokens=_OCR_MAX_NEW_TOKENS,
                num_beams=num_beams,
            )
            region_values = self._run_task_batch(
                images,
                sizes,
                _REGION_TASK,
                max_new_tokens=_REGION_MAX_NEW_TOKENS,
                num_beams=num_beams,
            )
            for i, idx in enumerate(indices):
                results[idx] = FlorenceResult(
                    caption=self._normalize_caption(caption_values[i]),
                    ocr_lines=self._normalize_lines(ocr_values[i]),
                    region_descriptions=self._normalize_lines(
                        region_values[i],
                        drop_numeric_only=True,
                    ),
                )
        except Exception as exc:
            names = ", ".join(image_paths[i].name for i in indices)
            logger.warning("Florence batch inference failed for [%s]: %s", names, exc)
        finally:
            for image in images:
                image.close()

    def _run_task_batch(
        self,
        images: list[Image.Image],
        image_sizes: list[tuple[int, int]],
        task_prompt: str,
        max_new_tokens: int = 128,
        num_beams: int = _DEFAULT_NUM_BEAMS,
    ) -> list[Any]:
        import torch

        if not images:
            return []

        prompts = [task_prompt] * len(images)
        inputs = self._processor(text=prompts, images=images, return_tensors="pt", padding=True)
        device_inputs = {
            key: value.to(self._device) if hasattr(value, "to") else value
            for key, value in inputs.items()
        }
        with torch.inference_mode():
            generated_ids = self._model.generate(
                **device_inputs,
                max_new_tokens=max_new_tokens,
                num_beams=num_beams,
                early_stopping=bool(num_beams > 1),
                do_sample=False,
            )
        generated_texts = self._processor.batch_decode(generated_ids, skip_special_tokens=False)

        outputs: list[Any] = []
        for generated_text, image_size in zip(generated_texts, image_sizes):
            try:
                outputs.append(
                    self._processor.post_process_generation(
                        generated_text,
                        task=task_prompt,
                        image_size=image_size,
                    )
                )
            except Exception:
                outputs.append(generated_text)
        return outputs

    def _run_task(self, image: Image.Image, task_prompt: str, max_new_tokens: int = 128) -> Any:
        import torch

        num_beams = max(1, int(get_setting("florence_num_beams") or _DEFAULT_NUM_BEAMS))
        inputs = self._processor(text=task_prompt, images=image, return_tensors="pt")
        device_inputs = {
            key: value.to(self._device) if hasattr(value, "to") else value
            for key, value in inputs.items()
        }
        with torch.no_grad():
            generated_ids = self._model.generate(
                **device_inputs,
                max_new_tokens=max_new_tokens,
                num_beams=num_beams,
                early_stopping=bool(num_beams > 1),
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