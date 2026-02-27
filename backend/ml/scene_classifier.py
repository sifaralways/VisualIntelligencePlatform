"""
VIP ML — Scene / geography classification via Places365.

Model: ResNet-50 trained on Places365-Standard (MIT, free use)
       Weights auto-downloaded to ~/.cache/vip/models/ on first use
Backend: Apple Silicon MPS via PyTorch

Maps 365 scene categories to three output groups:
  geography — outdoor natural scenes  (Mountain, Ocean, Forest …)
  place     — recognisable built environments  (Cathedral, Stadium, Castle …)
  (indoor and ambiguous scenes are silently dropped)
"""

from __future__ import annotations

import logging
import urllib.request
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_MODEL_URL = (
    "http://places2.csail.mit.edu/models_places365/"
    "resnet50_places365.pth.tar"
)
_CATS_URL = (
    "https://raw.githubusercontent.com/CSAILVision/places365/master/"
    "categories_places365.txt"
)
_CACHE_DIR = Path.home() / ".cache" / "vip" / "models"
_MODEL_PATH = _CACHE_DIR / "resnet50_places365.pth.tar"
_CATS_PATH  = _CACHE_DIR / "categories_places365.txt"

# Places365 categories that map to geography descriptors
_GEOGRAPHY_MAP: dict[str, str] = {
    "mountain": "Mountain", "mountain_snowy": "Snowy Mountain",
    "valley": "Valley", "canyon": "Canyon", "cliff": "Cliff",
    "beach": "Beach", "ocean": "Ocean", "lake/natural": "Lake",
    "river": "River", "waterfall": "Waterfall", "glacier": "Glacier",
    "forest_path": "Forest", "forest/broadleaf": "Forest",
    "rainforest": "Rainforest", "bamboo_forest": "Bamboo Forest",
    "desert/sand": "Desert", "desert/vegetation": "Desert",
    "field/cultivated": "Field", "field/wild": "Field",
    "trench": "Trench", "tundra": "Tundra", "swamp": "Wetland",
    "sky": "Open Sky", "snowfield": "Snowfield",
    "volcanic_crater": "Volcano", "butte": "Desert Butte",
    "badlands": "Badlands", "islet": "Islet",
}

# Places365 categories that map to built-environment place names
_BUILT_MAP: dict[str, str] = {
    "cathedral/outdoor": "Cathedral", "church/outdoor": "Church",
    "castle": "Castle", "palace": "Palace", "tower": "Tower",
    "stadium/outdoor": "Stadium", "amphitheater": "Amphitheatre",
    "amusement_park": "Amusement Park", "campsite": "Campsite",
    "harbor": "Harbour", "lighthouse": "Lighthouse",
    "bridge": "Bridge", "dam": "Dam",
    "airport/terminal": "Airport",
    "train_station/outdoor": "Train Station",
    "plaza": "Plaza", "market/outdoor": "Market",
    "ski_resort": "Ski Resort",
}


@dataclass
class SceneTag:
    label: str
    confidence: float
    category: str   # "geography" | "place"


class SceneClassifier:
    """Places365 ResNet-50 scene classifier."""

    def __init__(self) -> None:
        self._model = None
        self._categories: list[str] = []
        self._transform = None
        self._device_str = "cpu"

    def load(self) -> None:
        """Lazy-load Places365 model. Safe to call multiple times."""
        if self._model is not None:
            return

        try:
            import torch
            import torchvision.models as models
            import torchvision.transforms as T

            self._device_str = "mps" if torch.backends.mps.is_available() else "cpu"
            self._device = torch.device(self._device_str)

            _CACHE_DIR.mkdir(parents=True, exist_ok=True)

            # Download weights if missing
            if not _MODEL_PATH.exists():
                logger.info("Downloading Places365 weights (~100 MB) …")
                urllib.request.urlretrieve(_MODEL_URL, _MODEL_PATH)
                logger.info("Places365 weights saved to %s", _MODEL_PATH)

            # Download categories if missing
            if not _CATS_PATH.exists():
                urllib.request.urlretrieve(_CATS_URL, _CATS_PATH)

            # Parse categories — each line: "/a/abbey 0"
            raw = _CATS_PATH.read_text().splitlines()
            self._categories = [line.split(" ")[0].replace("/", "", 1) for line in raw]

            # Build model
            logger.info("Loading Places365 ResNet-50 (device=%s) …", self._device_str)
            checkpoint = torch.load(_MODEL_PATH, map_location="cpu", weights_only=False)
            state_dict = {
                k.replace("module.", ""): v
                for k, v in checkpoint["state_dict"].items()
            }
            model = models.resnet50(num_classes=365)
            model.load_state_dict(state_dict)
            model.eval()
            self._model = model.to(self._device)

            self._transform = T.Compose([
                T.Resize(256),
                T.CenterCrop(224),
                T.ToTensor(),
                T.Normalize(mean=[0.485, 0.456, 0.406],
                            std=[0.229, 0.224, 0.225]),
            ])

            logger.info("✅  Places365 ready")
        except ImportError as e:
            logger.warning("Places365 unavailable — install torch + torchvision: %s", e)
        except Exception as e:
            logger.error("Failed to load Places365: %s", e)

    def classify(self, image_path: Path, top_k: int = 5) -> list[SceneTag]:
        """
        Classify a JPEG image into scene categories.

        Returns:
            List of SceneTag (geography or built-place), may be empty.
        """
        if self._model is None:
            return []

        try:
            import torch
            from PIL import Image

            img = Image.open(image_path).convert("RGB")
            tensor = self._transform(img).unsqueeze(0).to(self._device)

            with torch.no_grad():
                logits = self._model(tensor)
                probs = torch.nn.functional.softmax(logits, dim=1)[0]
                topk = probs.topk(top_k)

        except Exception as e:
            logger.warning("Places365 error on %s: %s", image_path.name, e)
            return []

        results: list[SceneTag] = []
        for idx, conf in zip(topk.indices.tolist(), topk.values.tolist()):
            cat_raw = self._categories[idx] if idx < len(self._categories) else ""
            # Try geography first, then built places
            label = _GEOGRAPHY_MAP.get(cat_raw)
            category = "geography"
            if label is None:
                label = _BUILT_MAP.get(cat_raw)
                category = "place"
            if label is None:
                continue
            results.append(SceneTag(label=label, confidence=float(conf), category=category))

        return results
