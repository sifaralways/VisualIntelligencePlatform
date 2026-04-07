"""
VIP ML — Landmark recognition.

Primary (user-configured):
  EfficientNet-B4 classifier fine-tuned on Google Landmarks Dataset v2 (81k classes).
  To enable, set these environment variables before starting VIP:
    VIP_LANDMARK_HF_REPO=user/repo-name   (HuggingFace repo with model.pth + class_labels.json)
    VIP_LANDMARK_MODEL_PATH=/path/model.pth  (local)
    VIP_LANDMARK_LABELS_PATH=/path/labels.json  (local, optional — defaults to same dir)
  GLDv2 weights are NOT bundled with VIP; obtain them from Kaggle / Google Research.
  Requires: pip install timm huggingface_hub

Fallback (always available — no configuration needed):
  OpenCLIP ViT-L/14 pretrained on LAION-2B (laion2b_s32b_b82k).
  Upgrade from the previous ViT-B/32 OpenAI model — richer multilingual embeddings.
  Zero-shot matching against ~300 curated world landmark text descriptions.
  Requires: open-clip-torch (already in requirements.txt)

Both paths use Apple Silicon MPS via PyTorch.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# User-configurable GLDv2 settings (env vars — empty string means disabled)
# ---------------------------------------------------------------------------
_GLDV2_HF_REPO     = os.environ.get("VIP_LANDMARK_HF_REPO", "")
_GLDV2_LOCAL_MODEL = os.environ.get("VIP_LANDMARK_MODEL_PATH", "")
_GLDV2_LOCAL_LABELS = os.environ.get("VIP_LANDMARK_LABELS_PATH", "")
_GLDV2_MODEL_ID    = "tf_efficientnet_b4"
_GLDV2_IMG_SIZE    = 380
_TOP_K             = 3

# ---------------------------------------------------------------------------
# CLIP fallback
# Primary attempt: ViT-L/14 LAION-2B (~1.7 GB download on first use)
#   — Much better multilingual zero-shot than ViT-B/32.
#   — Downloaded to ~/.cache/huggingface/hub/ automatically.
# Secondary attempt: ViT-B/32 OpenAI (~350 MB, already cached from previous runs)
#   — Used if ViT-L/14 download hasn't completed yet.
# Override via env vars: VIP_LANDMARK_CLIP_MODEL, VIP_LANDMARK_CLIP_PRETRAINED
# ---------------------------------------------------------------------------
_CLIP_MODEL_NAME = os.environ.get("VIP_LANDMARK_CLIP_MODEL", "ViT-L-14")
_CLIP_PRETRAINED = os.environ.get("VIP_LANDMARK_CLIP_PRETRAINED", "laion2b_s32b_b82k")
_CLIP_FALLBACK_MODEL      = "ViT-B-32"
_CLIP_FALLBACK_PRETRAINED = "openai"

# ---------------------------------------------------------------------------
# Curated landmark list — ~300 entries covering all continents
# Format: (display_label, descriptive_text_for_CLIP_embedding)
# ---------------------------------------------------------------------------
_LANDMARKS: list[tuple[str, str]] = [
    # ── Europe ─────────────────────────────────────────────────────────────
    ("Eiffel Tower",            "Eiffel Tower iron lattice, Paris, France"),
    ("Notre-Dame Cathedral",    "Notre-Dame Cathedral Gothic facade, Paris, France"),
    ("Louvre Museum",           "Louvre Museum glass pyramid entrance, Paris"),
    ("Versailles Palace",       "Palace of Versailles ornate gardens, France"),
    ("Arc de Triomphe",         "Arc de Triomphe Champs-Elysees, Paris"),
    ("Mont Saint-Michel",       "Mont Saint-Michel tidal island abbey, Normandy France"),
    ("Colosseum",               "Roman Colosseum amphitheatre, Rome, Italy"),
    ("Leaning Tower of Pisa",   "Leaning Tower of Pisa white marble, Italy"),
    ("Trevi Fountain",          "Trevi Fountain baroque, Rome, Italy"),
    ("Pantheon Rome",           "Pantheon Rome ancient dome oculus"),
    ("St. Peter's Basilica",    "St. Peter's Basilica Vatican, Rome"),
    ("Pompei ruins",            "Pompeii ancient ruins, Mount Vesuvius, Italy"),
    ("Amalfi Coast",            "Amalfi Coast cliffs colourful houses, Italy"),
    ("Cinque Terre",            "Cinque Terre colourful fishing villages cliff, Italy"),
    ("Florence Duomo",          "Florence Cathedral Brunelleschi dome, Italy"),
    ("Venice Grand Canal",      "Venice Grand Canal gondolas, Italy"),
    ("Sagrada Familia",         "Sagrada Familia unfinished basilica spires, Barcelona"),
    ("Alhambra",                "Alhambra Moorish palace Granada, Spain"),
    ("Park Guell",              "Park Guell Gaudi mosaic terrace, Barcelona"),
    ("Seville Cathedral",       "Seville Cathedral La Giralda tower, Spain"),
    ("Acropolis Athens",        "Acropolis Parthenon Athens, Greece"),
    ("Santorini",               "Santorini whitewashed buildings blue domes, Greece"),
    ("Meteora",                 "Meteora monasteries atop rock pillars, Greece"),
    ("Hagia Sophia",            "Hagia Sophia dome minarets, Istanbul, Turkey"),
    ("Blue Mosque Istanbul",    "Sultan Ahmed Blue Mosque minarets, Istanbul"),
    ("Cappadocia",              "Cappadocia hot air balloons fairy chimneys, Turkey"),
    ("Ephesus",                 "Ephesus ancient Roman ruins library, Turkey"),
    ("Big Ben",                 "Big Ben clock tower Elizabeth Tower, London"),
    ("Tower Bridge London",     "Tower Bridge bascule bridge River Thames, London"),
    ("Buckingham Palace",       "Buckingham Palace royal residence, London"),
    ("Stonehenge",              "Stonehenge prehistoric standing stones, Wiltshire"),
    ("Edinburgh Castle",        "Edinburgh Castle volcanic rock fortification, Scotland"),
    ("Neuschwanstein Castle",   "Neuschwanstein Castle fairy-tale Bavaria, Germany"),
    ("Brandenburg Gate",        "Brandenburg Gate neoclassical columns, Berlin"),
    ("Cologne Cathedral",       "Cologne Cathedral twin Gothic spires, Germany"),
    ("Hallstatt",               "Hallstatt village lake Alps, Austria"),
    ("Vienna State Opera",      "Vienna State Opera House, Austria"),
    ("Schoenbrunn Palace",      "Schoenbrunn Palace baroque, Vienna Austria"),
    ("Prague Castle",           "Prague Castle Charles Bridge, Czech Republic"),
    ("Old Town Square Prague",  "Prague Old Town Square astronomical clock"),
    ("Dubrovnik old town",      "Dubrovnik old city walls Adriatic, Croatia"),
    ("Plitvice Lakes",          "Plitvice Lakes turquoise waterfalls, Croatia"),
    ("Bran Castle",             "Bran Castle Dracula castle, Transylvania Romania"),
    ("Bruges canals",           "Bruges medieval canals cobblestone, Belgium"),
    ("Atomium Brussels",        "Atomium steel sphere structure, Brussels"),
    ("Amsterdam canals",        "Amsterdam canal houses bicycles, Netherlands"),
    ("Kinderdijk windmills",    "Kinderdijk traditional windmills, Netherlands"),
    ("Cliffs of Moher",         "Cliffs of Moher sheer sea cliffs, Ireland"),
    ("Giant's Causeway",        "Giant's Causeway hexagonal basalt columns, Northern Ireland"),
    ("Pena Palace Sintra",      "Pena Palace colourful hilltop, Sintra Portugal"),
    ("Lisbon Belem Tower",      "Belem Tower Tagus River Lisbon, Portugal"),
    ("Porto Dom Luis bridge",   "Porto Dom Luis bridge Douro river, Portugal"),
    ("Matterhorn",              "Matterhorn pyramid peak Swiss Alps"),
    ("Mont Blanc",              "Mont Blanc glacier Chamonix, French Alps"),
    ("Aqueduct of Segovia",     "Roman Aqueduct of Segovia arches, Spain"),
    ("Loch Ness",               "Loch Ness dark water Scottish Highlands"),
    ("Cliffs of Moher Ireland", "Cliffs of Moher Atlantic cliff face, West Ireland"),
    ("Northern Lights Norway",  "Northern lights Aurora Borealis Norway fjord"),
    ("Trolltunga Norway",       "Trolltunga cliff jutting Hardanger fjord, Norway"),
    ("Preikestolen Norway",     "Preikestolen Pulpit Rock cliff, Lysefjord Norway"),
    ("Skogafoss Iceland",       "Skogafoss wide waterfall rainbow, Iceland"),
    ("Seljalandsfoss Iceland",  "Seljalandsfoss curtain waterfall walk-behind, Iceland"),
    ("Geysir Iceland",          "Geysir hot spring geyser erupting, Iceland"),
    ("Blue Lagoon Iceland",     "Blue Lagoon geothermal spa milky blue, Iceland"),
    ("Jokulsarlon glacier",     "Jokulsarlon glacier lagoon icebergs floating, Iceland"),
    ("Fairy Pools Skye",        "Fairy Pools clear blue waterfalls, Isle of Skye"),
    # ── Asia ────────────────────────────────────────────────────────────────
    ("Taj Mahal",               "Taj Mahal white marble mausoleum, Agra India"),
    ("Amber Fort Jaipur",       "Amber Fort hilltop Rajasthan, India"),
    ("Hawa Mahal Jaipur",       "Hawa Mahal Palace of Winds honeycomb facade, Jaipur"),
    ("India Gate Delhi",        "India Gate war memorial New Delhi, India"),
    ("Lotus Temple Delhi",      "Lotus Temple Bahai House of Worship, Delhi"),
    ("Varanasi Ghats",          "Varanasi Ganges ghats sunrise boat, India"),
    ("Ajanta Caves",            "Ajanta Buddhist rock-cut cave paintings, Maharashtra"),
    ("Great Wall of China",     "Great Wall of China watchtowers mountain ridge"),
    ("Forbidden City",          "Forbidden City red walls golden roofs, Beijing"),
    ("Temple of Heaven Beijing","Temple of Heaven circular blue dome, Beijing"),
    ("Terracotta Army",         "Terracotta warriors Xi'an, China"),
    ("Li River Karst",          "Li River karst mountains Guilin, China"),
    ("Zhangjiajie pillars",     "Zhangjiajie sandstone pillar mountains Avatar, China"),
    ("Shanghai skyline",        "Shanghai Pudong skyline Pearl Tower, China"),
    ("Hong Kong Victoria Peak", "Hong Kong Victoria Harbour skyline at night"),
    ("Potala Palace",           "Potala Palace whitewashed hilltop, Lhasa Tibet"),
    ("Mount Fuji",              "Mount Fuji snow-capped volcano, Japan"),
    ("Fushimi Inari gates",     "Fushimi Inari thousands of torii gates, Kyoto Japan"),
    ("Himeji Castle",           "Himeji White Heron Castle, Japan"),
    ("Kinkakuji Temple",        "Kinkakuji Golden Pavilion reflection, Kyoto Japan"),
    ("Senso-ji Temple",         "Senso-ji temple Nakamise-dori, Asakusa Tokyo"),
    ("Shibuya Crossing",        "Shibuya scramble crossing neon lights, Tokyo"),
    ("Hiroshima Peace Memorial","Hiroshima Genbaku Dome atomic bomb dome, Japan"),
    ("Angkor Wat",              "Angkor Wat temple towers moat, Cambodia"),
    ("Bayon Temple",            "Bayon Temple stone faces, Angkor Thom Cambodia"),
    ("Halong Bay",              "Halong Bay limestone islands emerald water, Vietnam"),
    ("Hoi An lanterns",         "Hoi An Ancient Town colourful lanterns, Vietnam"),
    ("Uluwatu Temple Bali",     "Uluwatu cliff temple Bali Indonesia sunset"),
    ("Borobudur",               "Borobudur Buddhist stupa bells, Java Indonesia"),
    ("Prambanan temple",        "Prambanan Hindu temple spires, Java Indonesia"),
    ("Petronas Towers",         "Petronas Twin Towers Kuala Lumpur, Malaysia"),
    ("Marina Bay Sands",        "Marina Bay Sands infinity pool Singapore skyline"),
    ("Sigiriya Rock",           "Sigiriya Lion Rock fortress aerial Sri Lanka"),
    ("Bagan temples",           "Bagan temples balloon Myanmar dawn"),
    ("Shwedagon Pagoda",        "Shwedagon Pagoda golden stupa Yangon, Myanmar"),
    ("Gyeongbokgung Palace",    "Gyeongbokgung Palace gate Seoul, South Korea"),
    ("Burj Khalifa",            "Burj Khalifa tallest skyscraper Dubai"),
    ("Burj Al Arab",            "Burj Al Arab sail-shaped hotel Dubai"),
    ("Sheikh Zayed Mosque",     "Sheikh Zayed Grand Mosque white domes, Abu Dhabi"),
    ("Petra Jordan",            "Petra Treasury rock-cut facade, Jordan"),
    ("Wadi Rum desert",         "Wadi Rum red desert sandstone, Jordan"),
    ("Jerusalem Old City",      "Jerusalem Western Wall, Old City Israel"),
    ("Dome of the Rock",        "Dome of the Rock gold dome, Jerusalem"),
    ("Persepolis",              "Persepolis ancient Persian ruins columns, Iran"),
    ("Dead Sea",                "Dead Sea salt crusts floating, Israel Jordan"),
    # ── Africa ──────────────────────────────────────────────────────────────
    ("Pyramids of Giza",        "Great Pyramids of Giza Sphinx, Egypt desert"),
    ("Abu Simbel",              "Abu Simbel colossal statues, Nile Egypt"),
    ("Karnak Temple",           "Karnak Temple columns, Luxor Egypt"),
    ("Sahara Desert dunes",     "Sahara Desert sand dunes camel, North Africa"),
    ("Victoria Falls",          "Victoria Falls Zambezi River spray, Zimbabwe Zambia"),
    ("Serengeti plains",        "Serengeti wildebeest migration, Tanzania Africa"),
    ("Kilimanjaro",             "Mount Kilimanjaro snow peak, Tanzania"),
    ("Table Mountain",          "Table Mountain flat top, Cape Town, South Africa"),
    ("Cape of Good Hope",       "Cape of Good Hope cliff coastline, South Africa"),
    ("Marrakech Djemaa el-Fna","Djemaa el-Fna square Marrakech, Morocco"),
    ("Ait Benhaddou",           "Ait Benhaddou ksar mud-brick fortified village, Morocco"),
    ("Sossusvlei dunes",        "Sossusvlei red dunes Namib desert, Namibia"),
    ("Dead Vlei",               "Dead Vlei petrified trees red dunes, Namibia"),
    # ── Americas ────────────────────────────────────────────────────────────
    ("Statue of Liberty",       "Statue of Liberty torch crown, New York USA"),
    ("Grand Canyon",            "Grand Canyon layered red rock, Arizona USA"),
    ("Golden Gate Bridge",      "Golden Gate Bridge suspension cables, San Francisco USA"),
    ("Niagara Falls",           "Niagara Falls Horseshoe Falls mist, Canada USA"),
    ("Yosemite Valley",         "Yosemite Valley El Capitan Half Dome, California"),
    ("Yellowstone geysers",     "Yellowstone Old Faithful geyser erupting, Wyoming USA"),
    ("Glacier National Park",   "Glacier National Park Going-to-the-Sun Road, Montana"),
    ("Zion Canyon",             "Zion National Park Angel's Landing narrow canyon, Utah"),
    ("Bryce Canyon",            "Bryce Canyon hoodoos orange formations, Utah"),
    ("Arches National Park",    "Arches National Park Delicate Arch, Utah"),
    ("Monument Valley",         "Monument Valley Navajo Mittens buttes, Arizona"),
    ("Antelope Canyon",         "Antelope Canyon slot canyon light beams, Arizona"),
    ("Mount Rushmore",          "Mount Rushmore presidential faces, South Dakota USA"),
    ("Manhattan skyline",       "Manhattan skyline skyscrapers Empire State, New York"),
    ("Times Square",            "Times Square neon billboards night, New York City"),
    ("Capitol Building DC",     "United States Capitol Building dome, Washington DC"),
    ("Machu Picchu",            "Machu Picchu Incan citadel cloud forest, Peru"),
    ("Huayna Picchu",           "Huayna Picchu mountain peak above Machu Picchu"),
    ("Easter Island Moai",      "Easter Island Moai stone statues coast, Chile"),
    ("Atacama Desert",          "Atacama Desert salt flat Moon Valley, Chile"),
    ("Patagonia Torres del Paine","Torres del Paine granite towers Patagonia, Chile"),
    ("Iguazu Falls",            "Iguazu Falls wide cataracts, Argentina Brazil"),
    ("Christ the Redeemer",     "Christ the Redeemer arms outstretched, Rio de Janeiro"),
    ("Sugarloaf Mountain",      "Sugarloaf Mountain cable car, Rio de Janeiro"),
    ("Amazon rainforest",       "Amazon River rainforest canopy aerial, Brazil"),
    ("Salar de Uyuni",          "Salar de Uyuni salt flat sky reflection, Bolivia"),
    ("Galapagos Islands",       "Galapagos Islands giant tortoise landscape, Ecuador"),
    ("Chichen Itza",            "Chichen Itza El Castillo pyramid, Mexico"),
    ("Teotihuacan",             "Teotihuacan Pyramid of the Sun, Mexico"),
    ("Havana streetscape",      "Havana Malecon classic cars colourful buildings, Cuba"),
    # ── Oceania ─────────────────────────────────────────────────────────────
    ("Sydney Opera House",      "Sydney Opera House white sail shells Harbour Bridge"),
    ("Sydney Harbour Bridge",   "Sydney Harbour Bridge steel arch, Australia"),
    ("Uluru",                   "Uluru Ayers Rock red monolith, Australian outback"),
    ("Great Barrier Reef",      "Great Barrier Reef coral reef underwater, Queensland"),
    ("Great Ocean Road",        "Twelve Apostles sea stacks Great Ocean Road, Australia"),
    ("Blue Mountains",          "Three Sisters Blue Mountains, New South Wales Australia"),
    ("Milford Sound",           "Milford Sound fiord Mitre Peak, New Zealand"),
    ("Bora Bora",               "Bora Bora overwater bungalows turquoise lagoon"),
    # ── Natural wonders ─────────────────────────────────────────────────────
    ("Pamukkale terraces",      "Pamukkale cotton castle white travertine pools, Turkey"),
    ("Jiuzhaigou valley",       "Jiuzhaigou valley multi-coloured lakes, Sichuan China"),
    ("Angel Falls",             "Angel Falls world's highest waterfall, Venezuela"),
    ("Mount Everest",           "Mount Everest Himalaya highest summit, Nepal"),
    ("Chocolate Hills Bohol",   "Chocolate Hills conical hills Bohol, Philippines"),
    ("Banaue Rice Terraces",    "Banaue Rice Terraces mountain Ifugao, Philippines"),
    ("Palawan lagoon",          "Palawan turquoise lagoon limestone cliffs, Philippines"),
    ("Reed Flute Cave",         "Reed Flute Cave stalactites coloured lights, Guilin"),
    ("Wave Arizona",            "The Wave sandstone swirls Coyote Buttes, Arizona"),
    ("White Sands dunes",       "White Sands gypsum dunes, New Mexico USA"),
]

_LANDMARK_LABELS = [name for name, _ in _LANDMARKS]
_LANDMARK_TEXTS  = [text for _, text in _LANDMARKS]


@dataclass
class LandmarkTag:
    label: str
    confidence: float


class LandmarkRecogniser:
    """
    Two-stage landmark recogniser.

    Stage 1 — GLDv2 EfficientNet-B4 (optional, user-configured via env vars):
      Set VIP_LANDMARK_HF_REPO or VIP_LANDMARK_MODEL_PATH to enable.
      Requires: timm, huggingface_hub.

    Stage 2 — OpenCLIP ViT-L/14 LAION-2B zero-shot (always available):
      ~300 curated landmarks, upgraded from the old ViT-B/32 OpenAI model.
      Requires: open-clip-torch (already in requirements.txt).
    """

    def __init__(self) -> None:
        self._gldv2_model     = None
        self._gldv2_transform = None
        self._gldv2_labels: list[str] = []
        self._clip_model        = None
        self._clip_preprocess   = None
        self._clip_text_features = None
        self._device            = None
        self._mode: str         = "none"   # "gldv2" | "clip" | "none"

    # ── Loading ──────────────────────────────────────────────────────────────

    def load(self) -> None:
        if self._mode != "none":
            return
        if _GLDV2_HF_REPO or _GLDV2_LOCAL_MODEL:
            if self._try_load_gldv2():
                return
        self._try_load_clip()

    def _try_load_gldv2(self) -> bool:
        try:
            import torch
            import timm
            from torchvision import transforms

            device_str = "mps" if torch.backends.mps.is_available() else "cpu"
            self._device = torch.device(device_str)

            if _GLDV2_LOCAL_MODEL:
                weights_path = Path(_GLDV2_LOCAL_MODEL)
                if not weights_path.exists():
                    raise FileNotFoundError(f"Model not found: {weights_path}")
                labels_path = (
                    Path(_GLDV2_LOCAL_LABELS) if _GLDV2_LOCAL_LABELS
                    else weights_path.with_name("class_labels.json")
                )
            else:
                from huggingface_hub import hf_hub_download
                labels_path  = Path(hf_hub_download(repo_id=_GLDV2_HF_REPO, filename="class_labels.json"))
                weights_path = Path(hf_hub_download(repo_id=_GLDV2_HF_REPO, filename="model.pth"))

            with open(labels_path) as f:
                raw_labels: dict[str, str] = json.load(f)
            self._gldv2_labels = [raw_labels[str(i)] for i in range(len(raw_labels))]

            logger.info("Loading GLDv2 EfficientNet-B4 (%d classes, device=%s) ...",
                        len(self._gldv2_labels), device_str)
            model = timm.create_model(
                _GLDV2_MODEL_ID,
                pretrained=False,
                num_classes=len(self._gldv2_labels),
            )
            state = torch.load(weights_path, map_location="cpu", weights_only=True)
            model.load_state_dict(state, strict=False)
            model.eval().to(self._device)
            self._gldv2_model = model

            self._gldv2_transform = transforms.Compose([
                transforms.Resize(_GLDV2_IMG_SIZE + 20),
                transforms.CenterCrop(_GLDV2_IMG_SIZE),
                transforms.ToTensor(),
                transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                     std=[0.229, 0.224, 0.225]),
            ])

            self._mode = "gldv2"
            logger.info("GLDv2 landmark recogniser ready (%d classes)", len(self._gldv2_labels))
            return True

        except ImportError as e:
            logger.info("timm/huggingface_hub not available for GLDv2: %s", e)
            return False
        except Exception as e:
            logger.warning("GLDv2 load failed -- using CLIP fallback: %s", e)
            return False

    def _try_load_clip(self) -> bool:
        """
        Load OpenCLIP for zero-shot landmark matching.
        Tries ViT-L/14 LAION-2B first (best quality, 1.7 GB), then falls back
        to ViT-B/32 OpenAI (already cached on most systems, 350 MB).
        """
        candidates = [
            (_CLIP_MODEL_NAME, _CLIP_PRETRAINED),
        ]
        # Add B/32 fallback only if the primary is the default (avoid infinite fallback loops)
        if _CLIP_MODEL_NAME != _CLIP_FALLBACK_MODEL or _CLIP_PRETRAINED != _CLIP_FALLBACK_PRETRAINED:
            candidates.append((_CLIP_FALLBACK_MODEL, _CLIP_FALLBACK_PRETRAINED))

        for model_name, pretrained in candidates:
            try:
                import torch
                import open_clip

                device_str = "mps" if torch.backends.mps.is_available() else "cpu"
                self._device = torch.device(device_str)

                logger.info("Loading OpenCLIP %s/%s (device=%s) ...",
                            model_name, pretrained, device_str)
                model, _, preprocess = open_clip.create_model_and_transforms(
                    model_name, pretrained=pretrained
                )
                model.eval().to(self._device)
                self._clip_model      = model
                self._clip_preprocess = preprocess

                tokenizer = open_clip.get_tokenizer(model_name)
                tokens = tokenizer(_LANDMARK_TEXTS).to(self._device)
                with torch.no_grad():
                    text_features = model.encode_text(tokens)
                    text_features /= text_features.norm(dim=-1, keepdim=True)
                self._clip_text_features = text_features

                self._mode = "clip"
                logger.info("CLIP landmark recogniser ready (%d landmarks, %s/%s)",
                            len(_LANDMARK_LABELS), model_name, pretrained)
                return True

            except Exception as e:
                logger.warning("CLIP %s/%s load failed: %s", model_name, pretrained, e)
                continue

        logger.error("All CLIP model candidates failed — landmark recognition disabled")
        return False

    # ── Inference ────────────────────────────────────────────────────────────

    def recognise(self, image_path: Path, threshold: float = 0.28) -> list[LandmarkTag]:
        """
        Identify the landmark(s) in an image.

        GLDv2 mode:  threshold is a softmax probability (recommended: 0.05-0.30).
        CLIP mode:   threshold is cosine similarity (recommended: 0.28 for ViT-L/14).
        Returns [] when no model is loaded or nothing clears the threshold.
        """
        if self._mode == "gldv2":
            return self._recognise_gldv2(image_path, threshold)
        if self._mode == "clip":
            return self._recognise_clip(image_path, threshold)
        return []

    def _recognise_gldv2(self, image_path: Path, threshold: float) -> list[LandmarkTag]:
        try:
            import torch
            from PIL import Image

            img    = Image.open(image_path).convert("RGB")
            tensor = self._gldv2_transform(img).unsqueeze(0).to(self._device)

            with torch.no_grad():
                logits = self._gldv2_model(tensor)[0]
                probs  = torch.softmax(logits, dim=-1)

            top_probs, top_idx = probs.topk(_TOP_K)
            results: list[LandmarkTag] = []
            for prob, idx in zip(top_probs.tolist(), top_idx.tolist()):
                if prob < threshold:
                    break
                results.append(LandmarkTag(
                    label=self._gldv2_labels[idx],
                    confidence=round(prob, 4),
                ))
            return results

        except Exception as e:
            logger.warning("GLDv2 recognition error on %s: %s", image_path.name, e)
            return []

    def _recognise_clip(self, image_path: Path, threshold: float) -> list[LandmarkTag]:
        if self._clip_model is None or self._clip_text_features is None:
            return []
        try:
            import torch
            from PIL import Image

            img    = Image.open(image_path).convert("RGB")
            tensor = self._clip_preprocess(img).unsqueeze(0).to(self._device)

            with torch.no_grad():
                img_features = self._clip_model.encode_image(tensor)
                img_features /= img_features.norm(dim=-1, keepdim=True)
                sims = (img_features @ self._clip_text_features.T)[0]

            results = [
                LandmarkTag(label=_LANDMARK_LABELS[i], confidence=round(float(s), 4))
                for i, s in enumerate(sims.tolist())
                if s >= threshold
            ]
            return sorted(results, key=lambda t: t.confidence, reverse=True)[:2]

        except Exception as e:
            logger.warning("CLIP recognition error on %s: %s", image_path.name, e)
            return []

    @property
    def active_mode(self) -> str:
        """'gldv2' | 'clip' | 'none' -- which backend is in use."""
        return self._mode
