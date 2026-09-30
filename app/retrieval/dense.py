"""Exact cosine search over a persisted, pre-normalised embedding matrix."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np


@dataclass(frozen=True)
class DenseHit:
    index: int
    score: float
    rank: int


class DenseIndex:
    def __init__(self, matrix: np.ndarray, manifest: dict):
        self.matrix = np.ascontiguousarray(matrix, dtype=np.float32)
        self.manifest = manifest

    def __len__(self) -> int:
        return int(self.matrix.shape[0])

    def similarities(self, qvec: np.ndarray) -> np.ndarray:
        if not len(self):
            return np.zeros(0, dtype=np.float32)
        return self.matrix @ np.asarray(qvec, dtype=np.float32).reshape(-1)

    def top_k(self, qvec: np.ndarray, k: int) -> list[DenseHit]:
        sims = self.similarities(qvec)
        if sims.size == 0:
            return []
        order = sorted(range(len(sims)), key=lambda i: (-float(sims[i]), i))[:k]
        return [DenseHit(i, float(sims[i]), r + 1) for r, i in enumerate(order)]

    # ---------------------------------------------------------------- persistence
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp.npy")
        np.save(tmp, self.matrix)
        tmp.replace(path.with_suffix(".npy"))
        path.with_suffix(".json").write_text(json.dumps(self.manifest, indent=2, sort_keys=True), encoding="utf-8")

    @classmethod
    def load(cls, path: Path, expected: dict) -> Optional["DenseIndex"]:
        """Load only when the manifest matches `expected` (catalog + model fingerprint)."""
        mpath, npath = path.with_suffix(".json"), path.with_suffix(".npy")
        if not (mpath.exists() and npath.exists()):
            return None
        try:
            manifest = json.loads(mpath.read_text(encoding="utf-8"))
        except Exception:
            return None
        if any(manifest.get(k) != v for k, v in expected.items()):
            return None
        matrix = np.load(npath)
        if matrix.shape[0] != manifest.get("rows"):
            return None
        return cls(matrix, manifest)
