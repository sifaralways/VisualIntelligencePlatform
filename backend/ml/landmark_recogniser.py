"""
VIP ML — Landmark recognition via CLIP zero-shot matching.

Model: OpenCLIP ViT-B/32 (openai weights, ~350 MB)
Backend: Apple Silicon MPS via PyTorch

Compares the image embedding against a curated list of world-famous
landmark text descriptions. Returns a match if cosine similarity
exceeds the confidence threshold (default 0.26).

Text embeddings are precomputed once at load time and cached — inference
is just one image forward pass + dot product.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Curated list of world-famous landmarks with enough distinctive visual context.
# Format: "Landmark Name, Country/City"  — the extra context helps CLIP.
LANDMARKS: list[tuple[str, str]] = [
    ("Eiffel Tower", "Eiffel Tower, Paris, France"),
    ("Taj Mahal", "Taj Mahal, Agra, India"),
    ("Sydney Opera House", "Sydney Opera House, Australia"),
    ("Colosseum", "Colosseum, Rome, Italy"),
    ("Statue of Liberty", "Statue of Liberty, New York, USA"),
    ("Big Ben", "Big Ben clock tower, London, England"),
    ("Sagrada Familia", "Sagrada Familia basilica, Barcelona, Spain"),
    ("Machu Picchu", "Machu Picchu ruins, Peru"),
    ("Great Wall of China", "Great Wall of China"),
    ("Angkor Wat", "Angkor Wat temple, Cambodia"),
    ("Parthenon", "Parthenon, Acropolis, Athens, Greece"),
    ("Hagia Sophia", "Hagia Sophia, Istanbul, Turkey"),
    ("Burj Khalifa", "Burj Khalifa skyscraper, Dubai"),
    ("Petra", "Petra ancient city, Jordan"),
    ("Chichen Itza", "Chichen Itza pyramid, Mexico"),
    ("Christ the Redeemer", "Christ the Redeemer statue, Rio de Janeiro, Brazil"),
    ("Kremlin", "Kremlin and Red Square, Moscow, Russia"),
    ("St. Peter's Basilica", "St. Peter's Basilica, Vatican City"),
    ("Golden Gate Bridge", "Golden Gate Bridge, San Francisco, USA"),
    ("Niagara Falls", "Niagara Falls, Canada USA border"),
    ("Mount Fuji", "Mount Fuji, Japan"),
    ("Uluru", "Uluru Ayers Rock, Australia"),
    ("Leaning Tower of Pisa", "Leaning Tower of Pisa, Italy"),
    ("Tower Bridge", "Tower Bridge, London, England"),
    ("Notre-Dame Cathedral", "Notre-Dame Cathedral, Paris, France"),
    ("Alhambra", "Alhambra palace, Granada, Spain"),
    ("Neuschwanstein Castle", "Neuschwanstein Castle, Bavaria, Germany"),
    ("Stonehenge", "Stonehenge prehistoric monument, England"),
    ("Acropolis", "Acropolis hill, Athens, Greece"),
    ("Louvre Museum", "Louvre Museum glass pyramid, Paris, France"),
    ("Versailles Palace", "Palace of Versailles gardens, France"),
    ("Edinburgh Castle", "Edinburgh Castle, Scotland"),
    ("Buckingham Palace", "Buckingham Palace, London, England"),
    ("Pyramids of Giza", "Pyramids of Giza, Egypt"),
    ("Abu Simbel", "Abu Simbel temples, Egypt"),
    ("Victoria Falls", "Victoria Falls waterfall, Zimbabwe Zambia"),
    ("Iguazu Falls", "Iguazu Falls, Argentina Brazil"),
    ("Santorini", "Santorini white buildings blue domes, Greece"),
    ("Amalfi Coast", "Amalfi Coast cliffs and sea, Italy"),
    ("Cappadocia", "Cappadocia hot air balloons rock formations, Turkey"),
    ("Halong Bay", "Halong Bay limestone islands, Vietnam"),
    ("Bali Uluwatu Temple", "Uluwatu Temple cliff, Bali, Indonesia"),
    ("Borobudur", "Borobudur Buddhist temple, Java, Indonesia"),
    ("Forbidden City", "Forbidden City imperial palace, Beijing, China"),
    ("Temple of Heaven", "Temple of Heaven, Beijing, China"),
    ("Potala Palace", "Potala Palace, Lhasa, Tibet"),
    ("Himeji Castle", "Himeji Castle, Japan"),
    ("Kyoto Fushimi Inari", "Fushimi Inari torii gates, Kyoto, Japan"),
    ("Inca Trail", "Inca Trail mountain path, Peru"),
    ("Glacier National Park", "Glacier National Park mountains, Montana, USA"),
    ("Grand Canyon", "Grand Canyon, Arizona, USA"),
    ("Yellowstone", "Yellowstone geyser Old Faithful, USA"),
    ("Yosemite", "Yosemite Valley, California, USA"),
    ("Matterhorn", "Matterhorn mountain, Swiss Alps"),
    ("Santorini Oia", "Oia village sunset, Santorini Greece"),
    ("Dubrovnik", "Dubrovnik old city walls and sea, Croatia"),
    ("Prague Castle", "Prague Castle and Charles Bridge, Czech Republic"),
    ("Hallstatt", "Hallstatt village lake, Austria"),
    ("Harbour Bridge", "Sydney Harbour Bridge, Australia"),
]

# Label and query text as parallel lists (built at module load)
_LANDMARK_LABELS = [name for name, _ in LANDMARKS]
_LANDMARK_TEXTS  = [text for _, text in LANDMARKS]


@dataclass
class LandmarkTag:
    label: str
    confidence: float


class LandmarkRecogniser:
    """CLIP-based zero-shot landmark recogniser."""

    def __init__(self) -> None:
        self._model = None
        self._preprocess = None
        self._text_features = None
        self._device = None

    def load(self) -> None:
        """
        Lazy-load OpenCLIP ViT-B/32. Precomputes landmark text embeddings.
        Safe to call multiple times.
        """
        if self._model is not None:
            return

        try:
            import torch
            import open_clip

            device_str = "mps" if torch.backends.mps.is_available() else "cpu"
            self._device = torch.device(device_str)

            logger.info("Loading OpenCLIP ViT-B/32 (device=%s) …", device_str)
            model, _, preprocess = open_clip.create_model_and_transforms(
                "ViT-B-32", pretrained="openai"
            )
            model.eval().to(self._device)
            self._model = model
            self._preprocess = preprocess

            tokenizer = open_clip.get_tokenizer("ViT-B-32")
            tokens = tokenizer(_LANDMARK_TEXTS).to(self._device)

            with torch.no_grad():
                text_features = model.encode_text(tokens)
                text_features /= text_features.norm(dim=-1, keepdim=True)

            self._text_features = text_features
            logger.info("✅  Landmark recogniser ready (%d landmarks)", len(LANDMARKS))

        except ImportError as e:
            logger.warning("CLIP unavailable — install open-clip-torch: %s", e)
        except Exception as e:
            logger.error("Failed to load landmark recogniser: %s", e)

    def recognise(self, image_path: Path, threshold: float = 0.26) -> list[LandmarkTag]:
        """
        Attempt to match the image to a known landmark.

        Returns:
            List of up to 2 LandmarkTag above threshold, best match first.
            Usually 0 or 1 results — only specific landmark photos qualify.
        """
        if self._model is None or self._text_features is None:
            return []

        try:
            import torch
            from PIL import Image

            img = Image.open(image_path).convert("RGB")
            tensor = self._preprocess(img).unsqueeze(0).to(self._device)

            with torch.no_grad():
                image_features = self._model.encode_image(tensor)
                image_features /= image_features.norm(dim=-1, keepdim=True)
                similarities = (image_features @ self._text_features.T)[0]

        except Exception as e:
            logger.warning("Landmark recognition error on %s: %s", image_path.name, e)
            return []

        results: list[LandmarkTag] = []
        for idx, sim in enumerate(similarities.tolist()):
            if sim >= threshold:
                results.append(LandmarkTag(
                    label=_LANDMARK_LABELS[idx],
                    confidence=float(sim),
                ))

        return sorted(results, key=lambda t: t.confidence, reverse=True)[:2]
