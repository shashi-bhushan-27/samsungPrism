"""Exact + semantic plan cache (pipeline stage 3: fast path).

Scope rules (cache-poisoning guard):
* no siis_response  → only knowledge-base-grounded plans (origin "kb") are eligible;
* siis_response     → only plans built from byte-identical (whitespace-normalised) SIIS text.
A caller-supplied SIIS text can therefore never change what other users receive.

A semantic hit must pass: similarity ≥ threshold, concept-family compatibility (no disjoint
symptom families, no conflicting discriminative qualifiers, no domain conflict), and full
re-validation of the stored plan (schema, rules, catalog membership, URL gate).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import numpy as np

from app.cache.models import ORIGIN_KB, CacheHit, CacheKey, CacheRecord
from app.cache.store import CacheStore
from app.models.internal import CanonicalIntent
from app.retrieval.embeddings import EmbeddingProvider
from app.services.intent_lexicon import DISCRIMINATIVE_QUALIFIERS

log = logging.getLogger(__name__)

PLAN_ONLY_QUALIFIER_PENALTY = 0.02


def intent_summary(intent: CanonicalIntent) -> dict[str, Any]:
    return {
        "domain": intent.domain,
        "symptoms": list(intent.symptoms),
        "families": list(intent.features),
        "qualifiers": list(intent.qualifiers),
        "signature": intent.signature,
        "canonical_query": intent.canonical_query,
        "goal_kind": intent.goal_kind,
    }


def compatibility(query: dict[str, Any], plan: dict[str, Any]) -> tuple[bool, str]:
    qf, pf = set(query.get("families") or []), set(plan.get("families") or [])
    if qf and pf and not (qf & pf):
        return False, "concept_conflict"
    qd, pd = query.get("domain"), plan.get("domain")
    if qd and pd and qd != pd and not (qf & pf):
        return False, "domain_conflict"
    dq = set(query.get("qualifiers") or []) & DISCRIMINATIVE_QUALIFIERS
    dp = set(plan.get("qualifiers") or []) & DISCRIMINATIVE_QUALIFIERS
    if dq and dp and not (dq & dp):
        return False, "qualifier_conflict"
    return True, "ok"


@dataclass
class LookupResult:
    hit: Optional[CacheHit]
    best_similarity: float = 0.0
    rejected: Optional[list[dict[str, Any]]] = None


class PlanCache:
    def __init__(
        self,
        store: CacheStore,
        embedder: EmbeddingProvider,
        versions: dict[str, str],
        *,
        threshold: float,
        margin: float = 0.0,
        top_k: int = 8,
        key_min_similarity: float = 0.55,
        validator: Optional[Callable[[dict[str, Any]], bool]] = None,
        kb_fingerprints: Optional[dict[str, str]] = None,
    ):
        self.store = store
        self.embedder = embedder
        self.versions = dict(versions)
        self.threshold = threshold
        self.margin = margin
        self.top_k = top_k
        self.key_min_similarity = key_min_similarity
        self.validator = validator
        self.kb_fingerprints = kb_fingerprints or {}
        self._lock = threading.RLock()
        self._records: dict[str, CacheRecord] = {}
        self._vectors: dict[str, np.ndarray] = {}
        self._exact: dict[str, list[str]] = {}
        self._key_plan: list[str] = []
        self._key_text: list[str] = []
        self._matrix = np.zeros((0, embedder.dim), dtype=np.float32)
        self.stats = {"exact_hits": 0, "semantic_hits": 0, "misses": 0, "rejected_hits": 0, "writes": 0,
                      "invalidated": 0, "reembedded": 0}
        self.ready = False

    # ---------------------------------------------------------------- lifecycle
    def warm_load(self) -> dict[str, int]:
        """Load persisted plans, dropping stale ones and re-embedding keys after a model change."""
        loaded = dropped = reembedded = 0
        with self._lock:
            self._records.clear()
            vectors: dict[str, np.ndarray] = {}
            for rec, vec, sig in self.store.load_all():
                why = self._stale_reason(rec)
                if why:
                    self.store.delete(rec.plan_id)
                    dropped += 1
                    log.info("cache drop plan=%s reason=%s", rec.plan_id, why)
                    continue
                if vec is None or sig != self.embedder.signature or vec.shape[0] != len(rec.keys):
                    vec = self._embed_keys(rec.keys)
                    self.store.put(rec, vec, self.embedder.signature)
                    reembedded += 1
                self._records[rec.plan_id] = rec
                vectors[rec.plan_id] = vec
                loaded += 1
            self._vectors = vectors
            self._rebuild()
            self.stats["invalidated"] += dropped
            self.stats["reembedded"] += reembedded
            self.ready = True
        return {"loaded": loaded, "dropped": dropped, "reembedded": reembedded}

    def _stale_reason(self, rec: CacheRecord) -> Optional[str]:
        for k in ("catalog", "pipeline"):
            if rec.versions.get(k) != self.versions.get(k):
                return f"{k}_version_changed"
        if rec.origin == ORIGIN_KB and rec.source_id is not None and self.kb_fingerprints:
            current = self.kb_fingerprints.get(rec.source_id)
            if current is None:
                return "kb_document_removed"
            if current != rec.source_fp:
                return "kb_document_changed"
        if self.validator is not None and not self.validator(rec.payload):
            return "failed_revalidation"
        return None

    def _embed_keys(self, keys: list[CacheKey]) -> np.ndarray:
        if not keys:
            return np.zeros((0, self.embedder.dim), dtype=np.float32)
        return self.embedder.embed([k.normalized or k.text for k in keys])

    def _rebuild(self) -> None:
        vectors = self._vectors
        exact: dict[str, list[str]] = {}
        key_plan: list[str] = []
        key_text: list[str] = []
        mats: list[np.ndarray] = []
        for pid in sorted(self._records, key=lambda p: (self._records[p].created_at, p)):
            rec = self._records[pid]
            for k in rec.keys:
                exact.setdefault(k.normalized, [])
                if pid not in exact[k.normalized]:
                    exact[k.normalized].append(pid)
                key_plan.append(pid)
                key_text.append(k.normalized)
            mats.append(vectors[pid])
        matrix = np.vstack(mats) if mats else np.zeros((0, self.embedder.dim), dtype=np.float32)
        # Atomic swap: readers see either the old or the new snapshot.
        self._exact, self._key_plan, self._key_text, self._matrix = exact, key_plan, key_text, matrix

    # -------------------------------------------------------------------- reads
    def _eligible(self, rec: CacheRecord, scope_fp: Optional[str]) -> bool:
        if scope_fp is None:
            return rec.origin == ORIGIN_KB
        return rec.source_fp == scope_fp

    def _valid(self, rec: CacheRecord) -> bool:
        if self.validator is None:
            return True
        ok = self.validator(rec.payload)
        if not ok:
            self.stats["rejected_hits"] += 1
            self.evict(rec.plan_id, reason="failed_revalidation_on_hit")
        return ok

    def lookup_exact(self, intent: CanonicalIntent, scope_fp: Optional[str]) -> Optional[CacheHit]:
        for pid in list(self._exact.get(intent.normalized_query, [])):
            rec = self._records.get(pid)
            if rec is None or not self._eligible(rec, scope_fp):
                continue
            ok, why = compatibility(intent_summary(intent), rec.intent)
            if not ok:
                continue
            if self._valid(rec):
                self.stats["exact_hits"] += 1
                return CacheHit(rec, "exact", 1.0, intent.normalized_query)
        return None

    def lookup_semantic(
        self, intent: CanonicalIntent, qvec: np.ndarray, scope_fp: Optional[str], *, count_miss: bool = True
    ) -> LookupResult:
        matrix, key_plan, key_text = self._matrix, self._key_plan, self._key_text
        if matrix.shape[0] == 0:
            if count_miss:
                self.stats["misses"] += 1
            return LookupResult(None)
        sims = matrix @ np.asarray(qvec, dtype=np.float32).reshape(-1)
        k = min(len(sims), max(self.top_k * 8, 32))
        idx = np.argpartition(-sims, k - 1)[:k] if k < len(sims) else np.arange(len(sims))
        order = sorted(idx.tolist(), key=lambda i: (-float(sims[i]), i))
        qsum = intent_summary(intent)
        best_by_plan: dict[str, tuple[float, int]] = {}
        for i in order:
            pid = key_plan[i]
            if pid not in best_by_plan:
                best_by_plan[pid] = (float(sims[i]), i)
        ranked: list[tuple[float, str, int]] = []
        for pid, (sim, i) in best_by_plan.items():
            rec = self._records.get(pid)
            if rec is None or not self._eligible(rec, scope_fp):
                continue
            plan_q = set(rec.intent.get("qualifiers") or []) & DISCRIMINATIVE_QUALIFIERS
            if plan_q and not (plan_q & set(intent.qualifiers)):
                sim -= PLAN_ONLY_QUALIFIER_PENALTY
            ranked.append((sim, pid, i))
        ranked.sort(key=lambda t: (-t[0], t[1]))
        rejected: list[dict[str, Any]] = []
        best_sim = ranked[0][0] if ranked else 0.0
        for pos, (sim, pid, i) in enumerate(ranked[: self.top_k]):
            if sim < self.threshold:
                break
            rec = self._records[pid]
            ok, why = compatibility(qsum, rec.intent)
            if not ok:
                rejected.append({"plan_id": pid, "similarity": round(sim, 4), "reason": why})
                continue
            if self.margin > 0:
                rival = next(
                    (r for r in ranked[pos + 1 :] if self._records[r[1]].intent.get("signature") != rec.intent.get("signature")),
                    None,
                )
                if rival is not None and sim - rival[0] < self.margin:
                    rejected.append({"plan_id": pid, "similarity": round(sim, 4), "reason": "margin"})
                    break
            if not self._valid(rec):
                continue
            self.stats["semantic_hits"] += 1
            return LookupResult(
                CacheHit(rec, "semantic", sim, key_text[i], {"rejected": rejected}), best_sim, rejected
            )
        if count_miss:
            self.stats["misses"] += 1
        return LookupResult(None, best_sim, rejected)

    def best_similarity(self, qvec: np.ndarray) -> list[tuple[str, float]]:
        """Per-plan best similarity (diagnostics / threshold calibration)."""
        matrix, key_plan = self._matrix, self._key_plan
        if matrix.shape[0] == 0:
            return []
        sims = matrix @ np.asarray(qvec, dtype=np.float32).reshape(-1)
        best: dict[str, float] = {}
        for i, pid in enumerate(key_plan):
            if float(sims[i]) > best.get(pid, -2.0):
                best[pid] = float(sims[i])
        return sorted(best.items(), key=lambda kv: -kv[1])

    # ------------------------------------------------------------------- writes
    def put(self, record: CacheRecord, *, admit_keys: Optional[Callable[[CacheKey], bool]] = None) -> bool:
        """Store a plan. The payload is re-validated here: invalid plans are never cached."""
        if self.validator is not None and not self.validator(record.payload):
            log.warning("cache refused invalid plan=%s", record.plan_id)
            return False
        keys: list[CacheKey] = []
        seen: set[str] = set()
        for k in record.keys:
            if not k.normalized or k.normalized in seen:
                continue
            if admit_keys is not None and k.kind == "variation" and not admit_keys(k):
                continue
            seen.add(k.normalized)
            keys.append(k)
        if not keys:
            return False
        vecs = self._embed_keys(keys)
        if len(keys) > 1:
            # Variation keys must stay semantically close to the source query.
            anchor = vecs[0]
            sims = vecs @ anchor
            kept = [i for i, k in enumerate(keys) if k.kind != "variation" or sims[i] >= self.key_min_similarity]
            keys = [keys[i] for i in kept]
            vecs = vecs[kept]
        record.keys = keys
        record.versions = dict(self.versions, **{k: v for k, v in record.versions.items() if k not in self.versions})
        with self._lock:
            self.store.put(record, vecs, self.embedder.signature)
            self._records[record.plan_id] = record
            self._vectors[record.plan_id] = vecs
            self._rebuild()
            self.stats["writes"] += 1
        return True

    def evict(self, plan_id: str, *, reason: str = "") -> None:
        with self._lock:
            if plan_id not in self._records:
                return
            self._records.pop(plan_id, None)
            self._vectors.pop(plan_id, None)
            self.store.delete(plan_id)
            self._rebuild()
            log.info("cache evict plan=%s reason=%s", plan_id, reason)

    def clear(self) -> None:
        with self._lock:
            self.store.clear()
            self._records.clear()
            self._vectors.clear()
            self._rebuild()

    # --------------------------------------------------------------- import/export
    def export_jsonl(self, path: Path, *, origin: Optional[str] = ORIGIN_KB) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        recs = [r for r in self._records.values() if origin is None or r.origin == origin]
        recs.sort(key=lambda r: (r.source_id or "", r.plan_id))
        with path.open("w", encoding="utf-8") as fh:
            for r in recs:
                fh.write(json.dumps(r.to_json(), ensure_ascii=False, sort_keys=True) + "\n")
        return len(recs)

    def import_jsonl(self, path: Path) -> dict[str, int]:
        imported = skipped = 0
        if not path.exists():
            return {"imported": 0, "skipped": 0}
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = CacheRecord.from_json(json.loads(line))
                if rec.plan_id in self._records or self._stale_reason(rec):
                    skipped += 1
                    continue
                if self.put(rec):
                    imported += 1
                else:
                    skipped += 1
        return {"imported": imported, "skipped": skipped}

    # ----------------------------------------------------------------- inspect
    def __len__(self) -> int:
        return len(self._records)

    @property
    def key_count(self) -> int:
        return len(self._key_plan)

    def records(self) -> Iterable[CacheRecord]:
        return list(self._records.values())

    def size_bytes(self) -> Optional[int]:
        return self.store.size_bytes()
