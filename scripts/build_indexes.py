#!/usr/bin/env python3
"""Build and persist the catalog + SIIS indexes (batch embedding). Safe to run repeatedly."""

from __future__ import annotations

import argparse
import json
import time

import _common  # noqa: F401  (sys.path)
from app.catalog.loaders import link_queries_to_siis, load_catalog, parse_queries, parse_siis, read_json
from app.catalog.registry import CatalogRegistry
from app.core.config import Settings
from app.retrieval.embeddings import build_embedding_provider
from app.retrieval.indexes import CatalogIndex, SiisIndex


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=None)
    args = ap.parse_args()
    s = Settings(**({"data_dir": args.data_dir} if args.data_dir else {}))
    d = s.resolved_data_dir
    t0 = time.perf_counter()
    entries, issues, fp = load_catalog(d / "deeplinks.json")
    reg = CatalogRegistry(entries, fingerprint=fp, issues=issues)
    queries, _ = parse_queries(read_json(d / "queries.json")) if (d / "queries.json").exists() else ([], [])
    docs, _ = parse_siis(read_json(d / "siis_responses.json")) if (d / "siis_responses.json").exists() else ([], [])
    links, _ = link_queries_to_siis(queries, docs) if queries and docs else ({}, [])
    q_by_doc: dict[str, list[str]] = {}
    for q in queries:
        if q.id in links:
            q_by_doc.setdefault(links[q.id], []).append(q.text)
    emb = build_embedding_provider(s)
    ci = CatalogIndex(reg, emb, index_dir=s.index_dir, k1=s.bm25_k1, b=s.bm25_b)
    si = SiisIndex(docs, q_by_doc, emb, index_dir=s.index_dir, k1=s.bm25_k1, b=s.bm25_b)
    print(json.dumps({
        "dataset": s.dataset_label, "catalog": reg.stats(), "catalog_issues": [i.__dict__ for i in reg.issues][:20],
        "catalog_index_rows": len(ci), "catalog_index_from_disk": ci.loaded_from_disk,
        "siis_index_rows": len(si), "siis_index_from_disk": si.loaded_from_disk,
        "embedding": emb.signature, "index_dir": str(s.index_dir), "seconds": round(time.perf_counter() - t0, 2),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
