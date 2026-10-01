#!/usr/bin/env python3
"""Generate FRESH held-out paraphrases with an LLM *after* the cache threshold was frozen (one file per round).

Uses a different model/prompt than the one that produced the cache's variation keys, and drops any
paraphrase whose normalised text equals an existing cache key (so every item is genuinely unseen).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json

import _common
from app.core.config import Settings
from app.core.constants import PIPELINE_VERSION
from app.core.container import build_components, build_llm
from app.llm.parsing import parse_json_loose

SYSTEM = (
    "You rewrite a smartphone complaint the way different real customers would type it. "
    "Keep the same problem and context. Do not answer. No URLs."
)
# Each round uses its own registers (round 2 was generated after round 1 had been used to fix the lexicon).
ROUNDS = {
    1: {"support_chat": "a message in a support chat", "search_phrase": "a short web-search phrase",
        "indirect_description": "an indirect description of the problem"},
    2: {"spoken": "said aloud to a voice assistant: no punctuation, run-on, filler words",
        "short_angry": "a short, angry message",
        "non_native": "written by a non-native English speaker, with small grammar mistakes",
        "ticket_summary": "a one-sentence support-ticket summary written by an agent in the third person"},
    # Round 3 was generated after the screen-domain lexicon was added for the official data.
    3: {"text_message": "a casual text message with abbreviations and no capital letters",
        "question": "a question that starts with 'why' or 'how do I'",
        "forum_post": "the first two sentences of a detailed forum post",
        "keywords": "three to six keywords typed into a help search box"},
}


def schema_for(styles: dict[str, str]) -> dict:
    return {"type": "OBJECT", "properties": {k: {"type": "STRING", "description": v} for k, v in styles.items()},
            "required": list(styles)}


async def main_async(args) -> int:
    _common.load_env_file(args.env_file)
    s = Settings()
    comps = build_components(s, llm=None)
    gen_settings = Settings(llm_model=args.model, llm_fallback_models=args.fallbacks, llm_hedge_after_s=0)
    llm = build_llm(gen_settings)
    keys = {k.normalized for r in comps.cache.records() for k in r.keys}
    styles = ROUNDS[args.round]
    prompt_styles = "\n".join(f"- {k}: {v}" for k, v in styles.items())
    items, dropped = [], 0
    for q in comps.queries:
        try:
            resp = await llm.generate_structured(system=SYSTEM, prompt=f"COMPLAINT: {q.text}\nWRITE ONE VERSION PER STYLE:\n"
                                                 f"{prompt_styles}", schema=schema_for(styles),
                                                 temperature=args.temperature, max_output_tokens=500)
            data = parse_json_loose(resp.text)
        except Exception as exc:
            print(json.dumps({"id": q.id, "error": type(exc).__name__}))
            await asyncio.sleep(args.pace)
            continue
        for style in styles:
            text = str(data.get(style, "")).strip()
            if not text:
                continue
            if comps.enricher.normalize_query(text) in keys:
                dropped += 1
                continue
            items.append({"query_id": q.id, "style": style, "text": text, "model": resp.model})
        print(json.dumps({"id": q.id, "n": len(items)}), flush=True)
        await asyncio.sleep(args.pace)
    out = {
        "_notice": "LLM-generated held-out paraphrases (synthetic). Generated after the semantic threshold was "
                   "frozen; items identical to a cache key were removed.",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "round": args.round,
        "styles": styles,
        "generator_model": args.model,
        "temperature": args.temperature,
        "pipeline_version": PIPELINE_VERSION,
        "frozen_threshold": s.semantic_cache_threshold,
        "dropped_as_seen": dropped,
        "items": items,
    }
    name = "paraphrases_llm_heldout.json" if args.round == 1 else f"paraphrases_llm_heldout_r{args.round}.json"
    path = s.resolved_data_dir / "eval" / name
    _common.write_json(path, out)
    print(json.dumps({"written": str(path), "items": len(items), "dropped_as_seen": dropped}))
    await llm.aclose()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--model", default="gemini-3.5-flash-lite")
    ap.add_argument("--fallbacks", default="gemini-3.1-flash-lite")
    ap.add_argument("--pace", type=float, default=1.0)
    ap.add_argument("--round", type=int, default=2, choices=sorted(ROUNDS))
    ap.add_argument("--temperature", type=float, default=0.9)
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
