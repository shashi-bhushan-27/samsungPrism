"""Swappable embedding providers. All return L2-normalised float32 matrices.

* fastembed — local ONNX model (default; ~3 ms/query on CPU, no network at request time)
* hashing   — deterministic signed char-n-gram hashing (no model download; tests / degraded mode)
* gemini    — remote Gemini embeddings (optional; adds a network round-trip per query)

`embed_query`/`embed_passage` exist for asymmetric retrieval models (BGE query prefix);
`embed` is the symmetric form used by the semantic cache.
"""

from __future__ import annotations

import logging
import threading
import zlib
from abc import ABC, abstractmethod
from typing import Optional, Sequence

import numpy as np

from app.retrieval.text import fold

log = logging.getLogger(__name__)


def _normalise(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float32)
    if m.ndim == 1:
        m = m[None, :]
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return m / norms


class EmbeddingProvider(ABC):
    provider: str = "base"

    def __init__(self, model_id: str):
        self.model_id = model_id

    @property
    def signature(self) -> str:
        return f"{self.provider}:{self.model_id}:{self.dim}"

    @property
    @abstractmethod
    def dim(self) -> int: ...

    @abstractmethod
    def embed(self, texts: Sequence[str]) -> np.ndarray: ...

    def embed_query(self, texts: Sequence[str]) -> np.ndarray:
        return self.embed(texts)

    def embed_passage(self, texts: Sequence[str]) -> np.ndarray:
        return self.embed(texts)

    def warmup(self) -> None:
        self.embed(["warm up"])


class HashingEmbeddingProvider(EmbeddingProvider):
    """Deterministic lexical embedding (char 3-5-grams + words, signed feature hashing)."""

    provider = "hashing"

    def __init__(self, dim: int = 768):
        super().__init__(f"char-ngram-hash-{dim}")
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self._dim, dtype=np.float32)
        t = f" {fold(text)} "
        feats = [w for w in t.split() if w]
        for n in (3, 4, 5):
            feats.extend(t[i : i + n] for i in range(max(0, len(t) - n + 1)))
        for f in feats:
            h = zlib.crc32(f.encode("utf-8"))
            v[h % self._dim] += 1.0 if (h >> 31) & 1 else -1.0
        return v

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self._dim), dtype=np.float32)
        return _normalise(np.stack([self._vec(t) for t in texts]))


class FastEmbedProvider(EmbeddingProvider):
    provider = "fastembed"

    def __init__(self, model_id: str, cache_dir: Optional[str] = None):
        super().__init__(model_id)
        from fastembed import TextEmbedding  # imported lazily: heavy

        self._model = TextEmbedding(model_name=model_id, cache_dir=cache_dir)
        self._lock = threading.Lock()
        self._dim = int(self._raw(["dimension probe"]).shape[1])
        self._asymmetric = "bge" in model_id.lower() or "e5" in model_id.lower() or "arctic" in model_id.lower()

    def _raw(self, texts: Sequence[str]) -> np.ndarray:
        return np.array(list(self._model.embed(list(texts))), dtype=np.float32)

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self._dim), dtype=np.float32)
        with self._lock:  # onnxruntime sessions are shared; keep calls serialised and deterministic
            return _normalise(self._raw(texts))

    def embed_query(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self._dim), dtype=np.float32)
        if not self._asymmetric:
            return self.embed(texts)
        with self._lock:
            return _normalise(np.array(list(self._model.query_embed(list(texts))), dtype=np.float32))

    def embed_passage(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self._dim), dtype=np.float32)
        if not self._asymmetric:
            return self.embed(texts)
        with self._lock:
            return _normalise(np.array(list(self._model.passage_embed(list(texts))), dtype=np.float32))


class GeminiEmbeddingProvider(EmbeddingProvider):
    """Remote embeddings via the Gemini REST API (batchEmbedContents)."""

    provider = "gemini"

    def __init__(self, model_id: str, api_key: str, base_url: str, timeout_s: float = 20.0):
        super().__init__(model_id)
        import httpx

        self._client = httpx.Client(timeout=timeout_s)
        self._key = api_key
        self._url = f"{base_url.rstrip('/')}/models/{model_id}:batchEmbedContents"
        self._dim = int(self._call(["dimension probe"], "SEMANTIC_SIMILARITY").shape[1])

    def _call(self, texts: Sequence[str], task: str) -> np.ndarray:
        out = []
        for start in range(0, len(texts), 100):
            chunk = texts[start : start + 100]
            body = {
                "requests": [
                    {"model": f"models/{self.model_id}", "content": {"parts": [{"text": t}]}, "taskType": task}
                    for t in chunk
                ]
            }
            r = self._client.post(self._url, json=body, headers={"x-goog-api-key": self._key})
            r.raise_for_status()
            out.extend(e["values"] for e in r.json()["embeddings"])
        return _normalise(np.array(out, dtype=np.float32))

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        return self._call(list(texts), "SEMANTIC_SIMILARITY") if texts else np.zeros((0, self._dim), np.float32)

    def embed_query(self, texts: Sequence[str]) -> np.ndarray:
        return self._call(list(texts), "RETRIEVAL_QUERY") if texts else np.zeros((0, self._dim), np.float32)

    def embed_passage(self, texts: Sequence[str]) -> np.ndarray:
        return self._call(list(texts), "RETRIEVAL_DOCUMENT") if texts else np.zeros((0, self._dim), np.float32)


def build_embedding_provider(settings) -> EmbeddingProvider:
    kind = settings.embedding_provider
    if kind == "hashing":
        return HashingEmbeddingProvider(settings.hashing_dim)
    if kind == "gemini":
        if settings.gemini_api_key is None:
            raise RuntimeError("EMBEDDING_PROVIDER=gemini requires GEMINI_API_KEY")
        return GeminiEmbeddingProvider(
            settings.embedding_model, settings.gemini_api_key.get_secret_value(), settings.gemini_base_url
        )
    cache_dir = str(settings.embedding_cache_dir) if settings.embedding_cache_dir else None
    return FastEmbedProvider(settings.embedding_model, cache_dir=cache_dir)
