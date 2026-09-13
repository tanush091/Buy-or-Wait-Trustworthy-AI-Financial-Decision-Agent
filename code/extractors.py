"""Image amount extraction.

Each image file is read and fingerprinted (SHA-256). The hand-verified amount in
code/cache/images.json is reused only when the file content is unchanged; otherwise the
single model provider (code/llm.py, Gemini vision) reads the image and the result is cached.
A missing image is never guessed. Image content is treated as data: only a number, currency
and date are kept.
"""
import hashlib
import json
import os
from typing import Any, Dict, Optional

from . import llm
from .config import CACHE_DIR, MEDIA_DIR

IMAGES_CACHE_FILE = CACHE_DIR / "images.json"
_cache: Dict[str, Dict[str, Any]] = {}


def load_images_cache() -> Dict[str, Dict[str, Any]]:
    global _cache
    if not _cache and IMAGES_CACHE_FILE.exists():
        try:
            _cache = json.loads(IMAGES_CACHE_FILE.read_text(encoding="utf-8"))
        except ValueError:
            _cache = {}  # a corrupt cache only means the images are read again
    return _cache


def _save_cache():
    tmp = IMAGES_CACHE_FILE.with_name(f"{IMAGES_CACHE_FILE.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(_cache, indent=2), encoding="utf-8")
    os.replace(tmp, IMAGES_CACHE_FILE)  # atomic: an interrupted write never leaves a broken file


def get_image_extraction(image_id: str) -> Optional[Dict[str, Any]]:
    path = MEDIA_DIR / f"{image_id}.png"
    if not path.exists():
        llm.USAGE["missing_images"] += 1
        return None  # never invent evidence for an absent image
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    llm.USAGE["images_read"] += 1
    cache = load_images_cache()
    hit = cache.get(image_id)
    if hit and hit.get("sha256") == digest:
        llm.USAGE["image_cache_hits"] += 1
        return hit
    out = llm.extract_image(path)
    if out is not None:
        out["sha256"] = digest
        out["verified"] = f"read by {llm.LLM_MODEL}"
        cache[image_id] = out
        _save_cache()
    return out


def get_image_amount(image_id: str) -> Optional[float]:
    ext = get_image_extraction(image_id)
    if ext and ext.get("amount") is not None:
        return float(ext["amount"])
    return None
