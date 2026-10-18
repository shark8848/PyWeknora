"""Embedding support: remote OpenAI-compatible API with local fallback."""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import Any

from .. import config
from . import model_runtime


LOCAL_DIMS = 256


def _tokens(text: str) -> list[str]:
    lowered = text.lower()
    latin = re.findall(r"[a-z0-9_]+", lowered)
    cjk = re.findall(r"[\u3400-\u9fff]", lowered)
    bigrams = [lowered[i:i + 2] for i in range(max(0, len(lowered) - 1)) if re.match(r"[\u3400-\u9fff]", lowered[i:i + 2])]
    return latin + cjk + bigrams


def local_embedding(text: str) -> list[float]:
    counts = Counter(_tokens(text))
    vector = [0.0] * LOCAL_DIMS
    for token, count in counts.items():
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % LOCAL_DIMS
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += sign * (1.0 + math.log(count))
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


def embed_texts(texts: list[str], runtime: dict[str, Any] | None = None) -> list[list[float]]:
    if runtime is None and config.EMBEDDING_BASE_URL:
        runtime = model_runtime._environment_runtime("Embedding")
    if model_runtime.available(runtime) and texts:
        try:
            result = model_runtime.embedding(runtime, texts)
            if result:
                return result
        except (RuntimeError, KeyError, ValueError, TimeoutError):
            pass
    return [local_embedding(text) for text in texts]


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    length = min(len(left), len(right))
    dot = sum(left[i] * right[i] for i in range(length))
    lnorm = math.sqrt(sum(v * v for v in left[:length])) or 1.0
    rnorm = math.sqrt(sum(v * v for v in right[:length])) or 1.0
    return dot / (lnorm * rnorm)
