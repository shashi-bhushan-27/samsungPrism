"""Format-tolerant, validating loaders for the starter assets.

The PDF names the files and their content but not their exact JSON layout, so each loader
accepts the plausible shapes (list of records, dict keyed by id/URI, alternate key
spellings), reports every anomaly as a LoadIssue, and never mutates the source files.
Catalog URIs are kept byte-for-byte (no strip / case-fold / decode).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional

from app.models.catalog import CatalogEntry, ValidationRule
from app.models.internal import LoadIssue, QueryRecord, Sample, SiisDoc

URI_KEYS = ("deeplink", "deepLink", "deep_link", "uri", "url", "link", "actionableDeeplink")
DESCRIPTION_KEYS = ("description", "desc", "intent", "intent_description")
MESSAGE_KEYS = ("message", "msg")
QNA_KEYS = ("qna_description", "qnaDescription", "qna_desc", "qna", "faq_description")
CLASSES_KEYS = ("classes", "class", "classification")
ORIGINAL_TYPE_KEYS = ("originalType", "original_type")
CONTROL_KEYS = ("controlType", "control_type", "control", "widget", "widgetType", "ui_type", "uiType", "type")
VALIDATION_KEYS = (
    "validation", "validationDeeplink", "validation_deeplink", "validationDeepLink", "toggle_validation",
    "validationRule", "validation_rule",
)
LIST_CONTAINER_KEYS = ("deeplinks", "items", "data", "entries", "catalog", "records", "queries", "responses", "siis")
QUERY_TEXT_KEYS = ("query", "text", "question", "complaint", "utterance", "user_query")
SIIS_TEXT_KEYS = ("siis_response", "siis", "response", "text", "content", "answer", "body", "reference", "article")
ID_KEYS = ("id", "query_id", "queryId", "qid", "uid", "key")


def file_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _first(d: Mapping[str, Any], keys: Iterable[str]) -> tuple[Optional[str], Any]:
    for k in keys:
        if k in d and d[k] is not None:
            return k, d[k]
    return None, None


def _records(obj: Any, source: str, issues: list[LoadIssue]) -> list[tuple[Optional[str], Any]]:
    """Normalise a container into [(outer_key, record)] preserving file order."""
    if isinstance(obj, list):
        return [(None, r) for r in obj]
    if isinstance(obj, dict):
        for k in LIST_CONTAINER_KEYS:
            if isinstance(obj.get(k), list) and len(obj) <= 3:
                return [(None, r) for r in obj[k]]
        return [(str(k), v) for k, v in obj.items()]
    issues.append(LoadIssue(source, "unsupported_container", type(obj).__name__))
    return []


# ------------------------------------------------------------------------- catalog
def _coerce_classes(value: Any, source: str, idx: int, issues: list[LoadIssue]) -> Optional[Mapping[str, str]]:
    if value is None:
        return None
    if isinstance(value, dict):
        if all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
            return MappingProxyType(dict(value))
        issues.append(LoadIssue(source, "classes_not_str_map", f"record {idx}: classes values coerced to str"))
        return MappingProxyType({str(k): ("" if v is None else str(v)) for k, v in value.items()})
    issues.append(LoadIssue(source, "classes_not_dict", f"record {idx}: classes of type {type(value).__name__} not servable"))
    return None


def _parse_validation(
    rec: Mapping[str, Any], uri: str, source: str, idx: int, issues: list[LoadIssue]
) -> Optional[ValidationRule]:
    _, v = _first(rec, VALIDATION_KEYS)
    if v is None and "key" in rec and any(k in rec for k in ("resultType", "condition", "value")):
        v = {k: rec.get(k) for k in ("key", "resultType", "condition", "value")}
    if v is None:
        return None
    if not isinstance(v, dict):
        issues.append(LoadIssue(source, "validation_not_object", f"record {idx}"))
        return None
    key = v.get("key")
    if not isinstance(key, str) or not key:
        issues.append(LoadIssue(source, "validation_missing_key", f"record {idx}"))
        return None
    _, vuri = _first(v, URI_KEYS)
    vuri = vuri if isinstance(vuri, str) and vuri else uri
    rt = v.get("resultType", v.get("result_type"))
    cond = v.get("condition")
    val = v.get("value")
    if rt is not None and rt not in ("boolean", "integer", "str", "float"):
        issues.append(LoadIssue(source, "validation_bad_result_type", f"record {idx}: {rt!r}"))
        rt = None
    if cond is not None and cond not in ("greater", "equal", "less"):
        issues.append(LoadIssue(source, "validation_bad_condition", f"record {idx}: {cond!r}"))
        cond = None
    return ValidationRule(
        deeplink=vuri,
        key=key,
        result_type=rt,
        condition=cond,
        value=None if val is None else str(val),
    )


def parse_catalog(obj: Any, source: str = "deeplinks.json") -> tuple[list[CatalogEntry], list[LoadIssue]]:
    issues: list[LoadIssue] = []
    entries: list[CatalogEntry] = []
    for idx, (outer_key, rec) in enumerate(_records(obj, source, issues)):
        if not isinstance(rec, dict):
            if isinstance(rec, str) and outer_key and outer_key.startswith("bixby://"):
                rec = {"deeplink": outer_key, "description": rec}
            else:
                issues.append(LoadIssue(source, "record_not_object", f"record {idx}"))
                continue
        _, uri = _first(rec, URI_KEYS)
        if isinstance(uri, dict):
            _, uri = _first(uri, ("deeplink", "uri", "url"))
        if uri is None and outer_key and outer_key.startswith("bixby://"):
            uri = outer_key
        if not isinstance(uri, str) or not uri:
            issues.append(LoadIssue(source, "missing_uri", f"record {idx}"))
            continue
        if not uri.startswith("bixby://"):
            issues.append(LoadIssue(source, "malformed_uri", f"record {idx}: {uri[:80]!r}"))
            continue
        if uri != uri.strip():
            issues.append(LoadIssue(source, "uri_whitespace", f"record {idx}: kept verbatim"))
        _, desc = _first(rec, DESCRIPTION_KEYS)
        if not isinstance(desc, str) or not desc.strip():
            issues.append(LoadIssue(source, "missing_description", f"record {idx}: {uri}"))
            desc = ""
        _, msg = _first(rec, MESSAGE_KEYS)
        _, qna = _first(rec, QNA_KEYS)
        _, classes = _first(rec, CLASSES_KEYS)
        _, orig = _first(rec, ORIGINAL_TYPE_KEYS)
        ctl_key, ctl = _first(rec, CONTROL_KEYS)
        if isinstance(ctl, dict):
            ctl = ctl.get("type") or ctl.get("name")
        entries.append(
            CatalogEntry(
                uri=uri,
                description=desc,
                message=msg if isinstance(msg, str) else None,
                qna_description=qna if isinstance(qna, str) else None,
                classes=_coerce_classes(classes, source, idx, issues),
                original_type=orig if isinstance(orig, str) else None,
                control_type=ctl if isinstance(ctl, str) else None,
                validation=_parse_validation(rec, uri, source, idx, issues),
                index=idx,
                raw=MappingProxyType(dict(rec)),
            )
        )
    return entries, issues


def content_fingerprint(data: Any) -> str:
    """Hash of the parsed content: identical on every OS (CRLF checkouts, re-indented files)."""
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_catalog(path: Path) -> tuple[list[CatalogEntry], list[LoadIssue], str]:
    raw = read_json(path)
    entries, issues = parse_catalog(raw, path.name)
    return entries, issues, content_fingerprint(raw)


# ------------------------------------------------------------------------- queries
def parse_queries(obj: Any, source: str = "queries.json") -> tuple[list[QueryRecord], list[LoadIssue]]:
    issues: list[LoadIssue] = []
    out: list[QueryRecord] = []
    seen: set[str] = set()
    for idx, (outer_key, rec) in enumerate(_records(obj, source, issues)):
        if isinstance(rec, str):
            text, qid, domain, siis_id, raw = rec, outer_key, None, None, {"query": rec}
        elif isinstance(rec, dict):
            _, text = _first(rec, QUERY_TEXT_KEYS)
            _, qid = _first(rec, ID_KEYS)
            qid = qid if qid is not None else outer_key
            domain = rec.get("domain") or rec.get("category")
            siis_id = rec.get("siis_id") or rec.get("siis_response_id") or rec.get("response_id")
            raw = rec
        else:
            issues.append(LoadIssue(source, "record_not_object", f"record {idx}"))
            continue
        if not isinstance(text, str) or not text.strip():
            issues.append(LoadIssue(source, "missing_query_text", f"record {idx}"))
            continue
        qid = str(qid) if qid is not None else f"Q{idx + 1:03d}"
        if qid in seen:
            issues.append(LoadIssue(source, "duplicate_id", qid))
            qid = f"{qid}#{idx}"
        seen.add(qid)
        out.append(QueryRecord(qid, text, domain if isinstance(domain, str) else None,
                               str(siis_id) if siis_id is not None else None, MappingProxyType(dict(raw))))
    return out, issues


# ---------------------------------------------------------------------------- SIIS
def parse_siis(obj: Any, source: str = "siis_responses.json") -> tuple[list[SiisDoc], list[LoadIssue]]:
    issues: list[LoadIssue] = []
    out: list[SiisDoc] = []
    seen: set[str] = set()
    for idx, (outer_key, rec) in enumerate(_records(obj, source, issues)):
        if isinstance(rec, str):
            text, did, title, domain, qid, qtext, raw = rec, outer_key, None, None, outer_key, None, {"text": rec}
        elif isinstance(rec, dict):
            _, text = _first(rec, SIIS_TEXT_KEYS)
            if isinstance(text, (list, tuple)):
                text = "\n".join(str(t) for t in text)
            did = rec.get("id") or rec.get("siis_id") or rec.get("doc_id") or outer_key
            title = rec.get("title")
            domain = rec.get("domain") or rec.get("category")
            qid = rec.get("query_id") or rec.get("queryId") or rec.get("qid")
            qtext = rec.get("query") if isinstance(rec.get("query"), str) and rec.get("query") != text else None
            raw = rec
        else:
            issues.append(LoadIssue(source, "record_not_object", f"record {idx}"))
            continue
        if not isinstance(text, str) or not text.strip():
            issues.append(LoadIssue(source, "missing_text", f"record {idx}"))
            continue
        did = str(did) if did is not None else f"S{idx + 1:03d}"
        if did in seen:
            issues.append(LoadIssue(source, "duplicate_id", did))
            did = f"{did}#{idx}"
        seen.add(did)
        out.append(
            SiisDoc(did, text, title if isinstance(title, str) else None, domain if isinstance(domain, str) else None,
                    str(qid) if qid is not None else None, qtext, MappingProxyType(dict(raw)))
        )
    return out, issues


def link_queries_to_siis(queries: list[QueryRecord], docs: list[SiisDoc]) -> tuple[dict[str, str], list[LoadIssue]]:
    """query id -> siis doc id. Explicit ids first, then identical query text, then index alignment."""
    issues: list[LoadIssue] = []
    by_id = {d.id: d for d in docs}
    by_qid = {d.query_id: d for d in docs if d.query_id}
    by_qtext = {d.query_text.strip().lower(): d for d in docs if d.query_text}
    links: dict[str, str] = {}
    for q in queries:
        if q.siis_id and q.siis_id in by_id:
            links[q.id] = q.siis_id
        elif q.id in by_qid:
            links[q.id] = by_qid[q.id].id
        elif q.id in by_id:
            links[q.id] = q.id
        elif q.text.strip().lower() in by_qtext:
            links[q.id] = by_qtext[q.text.strip().lower()].id
    if not links and len(queries) == len(docs):
        issues.append(LoadIssue("siis_responses.json", "linked_by_index", "no explicit ids; aligned by position"))
        links = {q.id: d.id for q, d in zip(queries, docs)}
    for q in queries:
        if q.id not in links:
            issues.append(LoadIssue("queries.json", "unlinked_query", q.id))
    return links, issues


# ------------------------------------------------------------------------- samples
def _sample_from_obj(sid: str, obj: Any, files: tuple[str, ...]) -> Optional[Sample]:
    if not isinstance(obj, dict):
        return None
    inp = obj.get("input") or obj.get("request")
    out = obj.get("output") or obj.get("expected") or obj.get("expected_output") or obj.get("response_expected")
    if inp is None and "query" in obj:
        inp = {"query": obj["query"], "siis_response": obj.get("siis_response")}
        if out is None and "response" in obj:
            out = obj
    if out is None and isinstance(obj.get("response"), dict) and inp is not None:
        out = obj.get("response")
        if "contexts" in out:
            out = {"response": out}
    if not isinstance(inp, dict) or not isinstance(out, dict):
        return None
    query = inp.get("query")
    if not isinstance(query, str):
        return None
    siis = inp.get("siis_response")
    return Sample(sid, query, siis if isinstance(siis, str) else None, out, files)


def load_samples(directory: Path) -> tuple[list[Sample], list[LoadIssue]]:
    issues: list[LoadIssue] = []
    if not directory.is_dir():
        return [], [LoadIssue(str(directory), "missing_samples_dir", "")]
    files = sorted(p for p in directory.rglob("*.json") if p.is_file())
    samples: list[Sample] = []
    pending_in: dict[str, Path] = {}
    pending_out: dict[str, Path] = {}
    for p in files:
        stem = p.stem
        low = stem.lower()
        for tag in ("_input", ".input", "-input", "_in", ".in", "input_", "in_"):
            if low.endswith(tag) or low.startswith(tag):
                pending_in[low.replace(tag, "")] = p
                break
        else:
            for tag in ("_output", ".output", "-output", "_out", ".out", "_expected", "output_", "out_", "expected_"):
                if low.endswith(tag) or low.startswith(tag):
                    pending_out[low.replace(tag, "")] = p
                    break
            else:
                try:
                    s = _sample_from_obj(stem, read_json(p), (p.name,))
                except Exception as exc:
                    issues.append(LoadIssue(p.name, "invalid_json", str(exc)[:200]))
                    continue
                if s is None:
                    issues.append(LoadIssue(p.name, "unrecognised_sample_shape", ""))
                else:
                    samples.append(s)
    for key in sorted(set(pending_in) | set(pending_out)):
        if key not in pending_in or key not in pending_out:
            issues.append(LoadIssue(key, "unpaired_sample_file", ""))
            continue
        try:
            obj = {"input": read_json(pending_in[key]), "output": read_json(pending_out[key])}
        except Exception as exc:
            issues.append(LoadIssue(key, "invalid_json", str(exc)[:200]))
            continue
        s = _sample_from_obj(key, obj, (pending_in[key].name, pending_out[key].name))
        if s is None:
            issues.append(LoadIssue(key, "unrecognised_sample_shape", ""))
        else:
            samples.append(s)
    return samples, issues
