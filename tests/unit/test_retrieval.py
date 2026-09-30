from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from app.catalog.loaders import load_catalog, parse_catalog
from app.catalog.registry import CatalogRegistry
from app.retrieval.bm25 import BM25Index
from app.retrieval.embeddings import HashingEmbeddingProvider
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.indexes import CatalogIndex, SiisIndex, candidate_label, make_doc
from app.retrieval.text import fold, tokens

DEV = Path(__file__).resolve().parents[2] / "data" / "dev_fixtures"


def test_tokenizer_normalises_case_spelling_and_compounds():
    assert tokens("Optimise the COLOURS on my Wi-Fi!") == tokens("optimize the colors on my wifi")
    assert "wifi" in fold("WI-FI")
    assert tokens("flickering screens") == tokens("flicker screen")


def test_bm25_ranks_exact_terms_and_is_deterministic():
    docs = [tokens("open display settings"), tokens("turn on power saving"), tokens("power saving options"), []]
    bm = BM25Index(docs)
    top = bm.top_k(tokens("power saving"), 3)
    assert [h.index for h in top][:2] in ([1, 2], [2, 1])
    assert bm.top_k(tokens("power saving"), 3) == top
    assert bm.top_k(tokens("zzz unknown"), 3) == []


def test_hashing_embeddings_are_deterministic_and_normalised():
    p = HashingEmbeddingProvider(256)
    a = p.embed(["battery drains fast", "battery drains fast"])
    assert np.allclose(a[0], a[1])
    assert np.allclose(np.linalg.norm(a, axis=1), 1.0)


def test_candidate_labels():
    entries, _ = parse_catalog(
        [
            {"deeplink": "bixby://m/1", "description": "Open Display settings"},
            {"deeplink": "bixby://m/2", "description": "Turn on Adaptive brightness"},
            {"deeplink": "bixby://m/3", "description": "Optimize now"},
        ]
    )
    assert [candidate_label(e) for e in entries] == ["Display settings", "Adaptive brightness", "Optimize now"]
    doc = make_doc(entries[0])
    assert doc.is_top_level and doc.label_tokens == frozenset({"display"})


def test_catalog_index_persists_and_invalidates(tmp_path):
    entries, issues, fp = load_catalog(DEV / "deeplinks.json")
    reg = CatalogRegistry(entries, fingerprint=fp, issues=issues)
    p = HashingEmbeddingProvider()
    first = CatalogIndex(reg, p, index_dir=tmp_path)
    second = CatalogIndex(reg, p, index_dir=tmp_path)
    assert not first.loaded_from_disk and second.loaded_from_disk
    assert np.allclose(first.dense.matrix, second.dense.matrix)
    # a changed catalog must not reuse the stale index
    raw = json.loads((DEV / "deeplinks.json").read_text())
    raw[0]["description"] = "Open the Settings application"
    e2, i2 = parse_catalog(raw)
    third = CatalogIndex(CatalogRegistry(e2, issues=i2), p, index_dir=tmp_path)
    assert not third.loaded_from_disk


def test_hybrid_union_contains_bm25_only_and_dense_only_hits():
    entries, issues, fp = load_catalog(DEV / "deeplinks.json")
    reg = CatalogRegistry(entries, fingerprint=fp, issues=issues)
    p = HashingEmbeddingProvider()
    idx = CatalogIndex(reg, p, index_dir=None)
    r = HybridRetriever(idx, top_k_bm25=5, top_k_dense=5, w_bm25=0.5, w_dense=0.5)
    q = "turn on power saving"
    cands = r.retrieve(q, p.embed_query([q])[0])
    assert 5 <= len(cands) <= 10
    # Retrieval is for recall; choosing toggle vs. screen is the resolver's job (test_resolver.py).
    top3 = {c.doc.entry.description for c in cands[:3]}
    assert {"Turn on Power saving", "Open Power saving options"} <= top3
    assert all(0.0 <= c.fused <= 1.0 for c in cands)
    assert r.retrieve(q, p.embed_query([q])[0])[0].uri == cands[0].uri


def test_siis_index_builds_over_source_text():
    from app.catalog.loaders import parse_siis

    docs, _ = parse_siis(json.loads((DEV / "siis_responses.json").read_text()))
    idx = SiisIndex(docs, {}, HashingEmbeddingProvider(), index_dir=None)
    top = idx.bm25.top_k(tokens("battery drains quickly"), 3)
    assert docs[top[0].index].id == "S-B01"
    assert all(d.text for d in idx.docs)  # source text preserved verbatim
