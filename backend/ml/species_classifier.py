"""
VIP ML — Species-level animal classification via BioCLIP.

Model: BioCLIP (imageomics/bioclip on HuggingFace)
       Trained on iNaturalist 10k+ species
Backend: Apple Silicon MPS via PyTorch / open-clip-torch

Only runs when YOLO has already detected an animal in the image.
Returns the most likely species name (e.g. "Golden Retriever", "Bengal Tiger").

BioCLIP uses the same architecture as OpenCLIP (ViT-B/16) but with
domain-specific pretraining on biological taxa.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Common species in photography — subset for zero-shot matching.
# Start with dogs (AKC breeds), cats (popular breeds), big cats, marine, birds.
_SPECIES: list[str] = [
    # Dogs
    "Labrador Retriever", "Golden Retriever", "German Shepherd",
    "French Bulldog", "Bulldog", "Poodle", "Beagle", "Rottweiler",
    "Dachshund", "German Shorthaired Pointer", "Siberian Husky",
    "Great Dane", "Border Collie", "Australian Shepherd", "Chihuahua",
    "Shih Tzu", "Boxer", "Doberman Pinscher", "Cavalier King Charles Spaniel",
    "Miniature Schnauzer", "Golden Doodle", "Labradoodle",
    # Cats
    "Domestic Shorthair Cat", "Persian Cat",
    "Maine Coon", "Siamese Cat", "Bengal Cat", "British Shorthair",
    "Ragdoll Cat", "Sphynx Cat", "Scottish Fold",
    # Wild cats
    "Lion", "Tiger", "Leopard", "Cheetah", "Jaguar", "Snow Leopard",
    # Birds
    "Bald Eagle", "Golden Eagle", "Barn Owl", "Great Horned Owl",
    "Mallard Duck", "Canada Goose", "Flamingo", "Pelican", "Toucan",
    "Peacock", "Macaw", "African Grey Parrot", "Robin", "Sparrow",
    "Cockatoo", "Cockatiel", "Budgerigar", "Kookaburra",
    "Red-tailed Hawk", "Peregrine Falcon", "Ostrich", "Emu",
    "King Penguin", "Emperor Penguin",
    # Marine
    "Bottlenose Dolphin", "Orca", "Humpback Whale", "Great White Shark",
    "Sea Turtle", "Clownfish", "Manta Ray", "Seahorse",
    # Other mammals
    "African Elephant", "Asian Elephant", "Giraffe", "Zebra",
    "Hippopotamus", "Rhinoceros", "Gorilla", "Chimpanzee", "Orangutan",
    "Red Fox", "Arctic Fox", "Wolf", "Brown Bear", "Polar Bear",
    "Giant Panda", "Red Panda", "Koala", "Kangaroo", "Wallaby",
    "Deer", "Moose", "American Bison", "Mountain Goat",
    "Horse", "Donkey", "Camel", "Alpaca", "Llama",
    # Reptiles
    "Komodo Dragon", "Iguana", "Chameleon", "Nile Crocodile",
    "Ball Python", "King Cobra", "Gila Monster",
]

_SPECIES_PROMPTS = [f"a photo of a {s}" for s in _SPECIES]


@dataclass
class SpeciesTag:
    label: str
    confidence: float


class SpeciesClassifier:
    """BioCLIP-based species-level animal classifier."""

    def __init__(self) -> None:
        self._model = None
        self._preprocess = None
        self._text_features = None
        self._device = None

    def load(self) -> None:
        """
        Lazy-load BioCLIP. Precomputes species text embeddings.
        Safe to call multiple times.
        """
        if self._model is not None:
            return

        try:
            import torch
            import open_clip

            device_str = "mps" if torch.backends.mps.is_available() else "cpu"
            self._device = torch.device(device_str)

            logger.info("Loading BioCLIP (device=%s) …", device_str)
            model, _, preprocess = open_clip.create_model_and_transforms(
                "hf-hub:imageomics/bioclip"
            )
            model.eval().to(self._device)
            self._model = model
            self._preprocess = preprocess

            tokenizer = open_clip.get_tokenizer("hf-hub:imageomics/bioclip")
            tokens = tokenizer(_SPECIES_PROMPTS).to(self._device)

            with torch.no_grad():
                text_features = model.encode_text(tokens)
                text_features /= text_features.norm(dim=-1, keepdim=True)

            self._text_features = text_features
            logger.info("✅  BioCLIP ready (%d species)", len(_SPECIES))

        except ImportError as e:
            logger.warning("BioCLIP unavailable — install open-clip-torch: %s", e)
        except Exception as e:
            logger.error("Failed to load BioCLIP: %s", e)

    def classify(self, image_path: Path, threshold: float = 0.30) -> SpeciesTag | None:
        """
        Identify the most likely animal species in the image.

        Args:
            image_path:  Path to a JPEG image (ideally cropped to the animal).
            threshold:   Minimum cosine similarity to accept a match.

        Returns:
            Best-matching SpeciesTag, or None if nothing exceeds threshold.
        """
        if self._model is None or self._text_features is None:
            return None

        try:
            import torch
            from PIL import Image

            img = Image.open(image_path).convert("RGB")
            tensor = self._preprocess(img).unsqueeze(0).to(self._device)

            with torch.no_grad():
                image_features = self._model.encode_image(tensor)
                image_features /= image_features.norm(dim=-1, keepdim=True)
                similarities = (image_features @ self._text_features.T)[0]

            best_idx = int(similarities.argmax())
            best_sim = float(similarities[best_idx])

            if best_sim < threshold:
                return None

            return SpeciesTag(label=_SPECIES[best_idx], confidence=best_sim)

        except Exception as e:
            logger.warning("BioCLIP error on %s: %s", image_path.name, e)
            return None
