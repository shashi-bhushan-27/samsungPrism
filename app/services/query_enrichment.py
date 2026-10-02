"""Query enrichment (pipeline stage 0): normalisation, canonical intent, query variations.

Everything here is deterministic. The cache key is derived from these outputs only — never
from free-form LLM text (the LLM may add extra variation *keys*, which are validated).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from dataclasses import replace
from functools import lru_cache
from typing import Iterable, Optional, Sequence

from app.core.constants import (
    DOMAINS,
    GOAL_KIND_CONFIGURATION,
    GOAL_KIND_TROUBLESHOOTING,
    VARIATIONS_MAX,
    VARIATIONS_MIN,
)
from app.models.internal import CanonicalIntent
from app.retrieval.text import fold
from app.services.intent_lexicon import (
    CONCEPTS,
    FILLER_PATTERNS,
    PROBLEM_INDICATORS,
    QUALIFIERS,
    THIRD_PERSON_PATTERNS,
    SETUP_INDICATORS,
    SLANG,
    Concept,
)
from app.validation.url_safety import sanitize_text

_COMMON_WORDS = frozenset(
    """
    about above after again against ages all almost along already also always am amount an and another any anymore
    anything anyway app application apps are around as ask at away back bad bar be because become becomes been before
    being below best better between big bit black blue both bottom box bright bring broken bus but buttons by call calls
    came camera can cannot car card case cause change changed changes changing charge charged charger charges charging
    check clean clear close closed closes closing code codes cold color colors colour colours come comes completely
    constantly cool could couple crazy daily dark data day days dead device did die died dies different display do does
    doing done down download downloaded downloading drain drained draining drains drop drops during each easy either
    else end enough error even ever every everything eye eyes face fact fail failed fails fall far fast faster few file
    files find fine finger first fix fixed flash flashing flicker flickering flickers flipped focus for forever found
    free friend friends from front full game games gaming get gets getting give given go goes going gone good got great
    green had half hand hands happen happened happens hard has have having heat heats help her here high him his hold
    home hot hour hours how however idea if image images in inside install installed installing instead into is issue
    issues it its itself just keep keeps kept kill killed killing know last lately later latest least left less let
    life light like line lines little load loading lock long longer look looking looks lose losing lost lot low lunch
    made main make makes making many max maybe me memory message messages might mind minute minutes mirror mode moment
    more morning most move moved much music must my myself need needed needs never new next night no normal not nothing
    notice now number of off often oh old on once one only open opened opens or other our out over own part people
    percent phone photo photos picture pictures play played playing please point power press pretty probably problem
    problems put quick quickly quite random randomly rather really reason recent recently record recorded recording red
    reset respond restart right run running runs said same save saved saving say says school screen second seconds see
    seem seems seen set setting settings several shake shaky should show shows side since slow slowly small smooth so
    some something sometimes soon sound speed start started starts stay still stop stopped stops storage strange stuck
    such suddenly super sure swipe swiping take takes taking tap taps terrible text than that the their them then there
    these they thing things think this those though through time times tired to today together too took top totally
    touch tried tries try trying turn turned turns twice two under until up update updated updates updating upgrade us
    use used useful uses using usual usually very video videos wait want wanted wants warm was watch way we week weeks
    weird well went were what when where whether which while white who why will with without work worked working works
    worse worst would wrong year yellow yes yesterday yet you your yourself
    annoying frustrating ridiculous horrible awful useless unusable nuts mad insane help
    hate hated damn trash junk garbage stupid crap pile fix now
    """.split()
)
_DOMAIN_WORDS = """
battery batteries drain drains draining drained charging charger charge cable port overheating overheats overheat hot heat
screen display flicker flickering flickers blinking brightness dim dark timeout touchscreen touch responsive unresponsive
protector tempered glass yellow yellowish tint colours colors scrolling scroll choppy smooth smoothness refresh rate
navigation swipe swipes swiping gesture gestures sideways vertical horizontal direction camera photos photo pictures
blurry blur focus fuzzy sharp crash crashes crashing crashed failed warning black shaky stabilization videos video
selfie selfies mirrored mirror flipped reversed backwards scan scanning codes storage card slow slower lag lagging laggy
sluggish frozen freeze freezes freezing restart restarts rebooting reboot update updated updating software apps app
animation animations transitions settings power saving percentage percent always display theme mode night light low
background sleeping sleep optimize memory performance profile gaming games playing randomly restarting location wifi
bluetooth ringtone keyboard contacts fingerprint microphone speaker notification notifications wallpaper
fast faster quickly quick slowly phone phones dies dying died keeps keep apps application applications pictures
galaxy samsung charged charges crashing working stopped suddenly sometimes constantly latest installed installing
downloaded downloading brightness adaptive smoothness protector sensitivity optimizer tracking autofocus hdr selfie
blank white cracked crack cracks shattered broken distorted floating circle transfer inner outer cover icons tablet
fold foldable activation carrier expand image half aspect ratio resolution startup
""".split()


def _osa_distance(a: str, b: str, max_d: int) -> int:
    """Optimal-string-alignment Damerau–Levenshtein distance with early exit."""
    if abs(len(a) - len(b)) > max_d:
        return max_d + 1
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        row_min = cur[0]
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
            row_min = min(row_min, cur[j])
        if row_min > max_d:
            return max_d + 1
        prev2, prev = prev, cur
    return prev[-1]


def _deletes(word: str, depth: int) -> set[str]:
    out = {word}
    frontier = {word}
    for _ in range(depth):
        nxt = set()
        for w in frontier:
            for i in range(len(w)):
                nxt.add(w[:i] + w[i + 1 :])
        out |= nxt
        frontier = nxt
    return out


class SpellCorrector:
    """SymSpell-style corrector.

    `targets` (curated domain words) are the only words a typo may be corrected *to*;
    `known` (targets + KB vocabulary + common English) are never "corrected", which keeps
    legitimate words such as "remote" from being rewritten to a nearby KB word.
    """

    def __init__(self, targets: Counter, known: Iterable[str] = ()):
        self.freq = targets
        self.known = frozenset(targets) | frozenset(known) | _COMMON_WORDS
        self._index: dict[str, set[str]] = {}
        for w in targets:
            if len(w) < 4 or not w.isalpha():
                continue
            for d in _deletes(w, 2 if len(w) >= 8 else 1):
                self._index.setdefault(d, set()).add(w)

    @lru_cache(maxsize=8192)
    def correct(self, word: str) -> str:
        if len(word) < 4 or not word.isalpha() or word in self.known:
            return word
        max_d = 2 if len(word) >= 8 else 1
        cands: set[str] = set()
        for d in _deletes(word, max_d):
            cands |= self._index.get(d, set())
        best: Optional[tuple[int, int, str]] = None
        for c in cands:
            dist = _osa_distance(word, c, max_d)
            if dist <= max_d:
                key = (dist, -self.freq[c], c)
                if best is None or key < best:
                    best = key
        return best[2] if best else word


_CONTRACTIONS = (
    (re.compile(r"\bcan't\b|\bcant\b"), "cannot"),
    (re.compile(r"\bwon't\b"), "will not"),
    (re.compile(r"\b(it|that|what|there|here)'s\b"), r"\1 is"),
    (re.compile(r"\b(i|you|we|they)'re\b"), r"\1 are"),
    (re.compile(r"\b(i|you|we|they)'ve\b"), r"\1 have"),
    (re.compile(r"\bi'm\b"), "i am"),
    (re.compile(r"\b(\w+)n't\b"), r"\1 not"),
    (re.compile(r"\b(\w+)'s\b"), r"\1"),
    (re.compile(r"\b(\w+)'ll\b"), r"\1 will"),
    (re.compile(r"\b(\w+)'d\b"), r"\1 would"),
)
_REPEAT_CHARS = re.compile(r"([a-z])\1{2,}")
_NON_WORD = re.compile(r"[^a-z0-9%' ]+")


def _title_case_words(text: str) -> str:
    return " ".join(w if w.isupper() else w[:1].upper() + w[1:] for w in text.split())


class QueryEnricher:
    def __init__(self, vocabulary_texts: Iterable[str] = ()):
        kb: Counter = Counter()
        for t in vocabulary_texts:
            kb.update(re.findall(r"[a-z]+", fold(t)))
        targets = Counter({w: 1 + kb.get(w, 0) for w in _DOMAIN_WORDS})
        self.corrector = SpellCorrector(targets, known=kb.keys())

    # ----------------------------------------------------------------- normalise
    def normalize_query(self, query: str) -> str:
        t, _ = sanitize_text(query or "")  # URLs never carry troubleshooting meaning
        t = unicodedata.normalize("NFKC", t).lower()
        t = t.replace("’", "'").replace("‘", "'")
        t = fold(t)
        for rx, rep in _CONTRACTIONS:
            t = rx.sub(rep, t)
        t = _REPEAT_CHARS.sub(r"\1\1", t)
        t = _NON_WORD.sub(" ", t)
        words: list[str] = []
        for w in t.split():
            w = w.strip("'")
            if not w:
                continue
            rep = SLANG.get(w, w)
            if rep:
                words.extend(rep.split())
        words = [self.corrector.correct(w) for w in words]
        text = " ".join(words)
        for rx, sub in THIRD_PERSON_PATTERNS:
            text = rx.sub(sub, text)
        for rx in FILLER_PATTERNS:
            text = rx.sub(" ", text)
        out: list[str] = []
        for w in text.split():
            if out and out[-1] == w:
                continue  # "very very slow" -> "very slow"
            out.append(w)
        return " ".join(out).strip()

    # ------------------------------------------------------------------- intent
    def _concepts(self, text: str) -> list[Concept]:
        hits = [c for c in CONCEPTS if any(p.search(text) for p in c.patterns)]
        ids = {c.id for c in hits}
        # Specific concepts subsume generic ones in the same text.
        if ids & {"camera_crash", "app_crash", "random_restart", "frozen"}:
            hits = [c for c in hits if c.id != "slow_performance" or "lag" in text or "slow" in text]
        if "camera_crash" in ids:
            hits = [c for c in hits if c.id != "app_crash"]
        if ids & {"app_crash", "camera_crash"}:
            hits = [c for c in hits if c.id != "frozen"]  # an app freezing is an app problem
        if "screen_color" in ids and not re.search(r"\b(?:hot|heat|overheat|burning|temperature)", text):
            hits = [c for c in hits if c.id != "overheating"]  # "warm colours" is not heat
        if ids & {"charging_issue", "choppy_scrolling", "slow_animation"} and not re.search(
            r"\blag\w*|\bsluggish\b|\b(?:phone|device|it|everything)\b.{0,15}\b(?:is|got|became|running|so|really)\b.{0,10}\bslow", text
        ):
            hits = [c for c in hits if c.id != "slow_performance"]
        if "camera_black_screen" in ids:
            hits = [c for c in hits if c.id not in ("dark_photo", "camera_crash")]
        if "dark_mode" in ids:
            hits = [c for c in hits if c.id not in ("screen_dim", "dark_photo")]
        if "background_drain" in ids or "aod_drain" in ids:
            hits = [c for c in hits if c.id != "battery_drain"] + (
                [c for c in hits if c.id == "battery_drain"] if "aod_drain" in ids and "background_drain" in ids else []
            )
        if "battery_percentage" in ids:
            hits = [c for c in hits if c.id != "battery_drain"]
        if "battery_limit" in ids:
            hits = [c for c in hits if c.id not in ("charging_issue",)]
        if "choppy_scrolling" in ids:
            hits = [c for c in hits if c.id != "slow_performance"]
        if "slow_animation" in ids:
            hits = [c for c in hits if c.id != "slow_performance"]
        if "touch_unresponsive" in ids:
            hits = [c for c in hits if c.id != "frozen"]
        if "storage_full" in ids:
            hits = [c for c in hits if c.id != "camera_storage" or "sd" in text or "card" in text]
        if "camera_storage" in ids and "sd" in text:
            hits = [c for c in hits if c.id != "storage_full"]
        if "shaky_video" in ids:
            hits = [c for c in hits if c.id != "blurry_photo"]
        return sorted(hits, key=lambda c: (-c.priority, CONCEPTS.index(c)))

    def analyze(self, query: str, *, _allow_split: bool = True) -> CanonicalIntent:
        normalized = self.normalize_query(query)
        concepts = self._concepts(normalized)
        qualifiers = tuple(q for q, rx, _ in QUALIFIERS if rx.search(normalized))
        scores = {d: 0.0 for d in DOMAINS}
        for c in concepts:
            for d, w in c.domains:
                if d in scores:
                    scores[d] += w
        if "while_charging" in qualifiers:
            scores["Battery"] += 0.3
        if "while_gaming" in qualifiers:
            scores["Performance"] += 0.3
        if not concepts:
            for d, kw in (("Battery", r"\bbattery\b|\bcharg"), ("Display", r"\bscreen\b|\bdisplay\b"),
                          ("Camera", r"\bcamera\b|\bphotos?\b|\bvideos?\b"), ("Performance", r"\bslow\b|\bapps?\b")):
                if re.search(kw, normalized):
                    scores[d] += 0.5
        best = max(scores.items(), key=lambda kv: (kv[1], -DOMAINS.index(kv[0])))
        domain = best[0] if best[1] > 0 else None
        primary = concepts[0] if concepts else None
        is_setup = bool(SETUP_INDICATORS.search(normalized)) and not PROBLEM_INDICATORS.search(normalized)
        if primary is not None and primary.kind == "setup" and not PROBLEM_INDICATORS.search(normalized):
            is_setup = True
        goal_kind = GOAL_KIND_CONFIGURATION if is_setup else GOAL_KIND_TROUBLESHOOTING
        qual_phrases = [p for q, _, p in QUALIFIERS if q in qualifiers]
        if concepts:
            canonical = " and ".join(c.phrase for c in concepts[:2])
            extra = [p for p in qual_phrases if p not in canonical]
            if extra:
                canonical += " " + " ".join(extra)
            topic = primary.topic
            title = primary.title
        else:
            canonical = normalized
            topic = _title_case_words(domain) if domain else "General"
            title = f"{domain} troubleshooting" if domain else "Phone troubleshooting"
        symptoms = tuple(sorted({c.id for c in concepts}))
        families = tuple(sorted({c.family for c in concepts}))
        signature = f"{domain or '-'}|{'+'.join(families) or '-'}|{'+'.join(sorted(qualifiers)) or '-'}"
        intent = CanonicalIntent(
            raw_query=query,
            normalized_query=normalized,
            canonical_query=canonical,
            domain=domain,
            domain_scores=scores,
            symptoms=symptoms,
            features=families,
            qualifiers=qualifiers,
            goal_kind=goal_kind,
            topic=topic,
            title_hint=title,
            signature=signature,
        )
        if _allow_split:
            subs = self._split(normalized, intent)
            if len(subs) > 1:
                intent = replace(intent, sub_intents=tuple(subs))
        return intent

    def _split(self, normalized: str, whole: CanonicalIntent) -> list[CanonicalIntent]:
        """Multi-intent decomposition: 'screen flickers and the battery dies fast'."""
        if len(whole.features) < 2:
            return []
        parts = [p.strip() for p in re.split(r"\b(?:and also|also|as well as|plus|and)\b|[,;]", normalized) if p.strip()]
        if len(parts) < 2:
            return []
        subs: list[CanonicalIntent] = []
        for p in parts:
            sub = self.analyze(p, _allow_split=False)
            if sub.features and all(set(sub.features) != set(s.features) for s in subs):
                subs.append(sub)
        # Merge qualifiers that belong to the whole complaint.
        if len(subs) >= 2 and len({s.domain for s in subs}) >= 2:
            return subs
        return []

    # --------------------------------------------------------------- variations
    def template_variations(self, query: str, intent: CanonicalIntent) -> list[str]:
        """Deterministic fallback paraphrases in ten registers (used when the LLM is unavailable)."""
        base = intent.canonical_query if intent.symptoms else intent.normalized_query or query.strip()
        base = base.strip().rstrip(".?!")
        base = re.sub(r"^(?:my|the|our|this)\s+", "", base)  # the templates add their own determiner
        qual = ""
        dom = (intent.domain or "phone").lower()
        variants = [
            f"My device is experiencing an issue where the {base}.".replace("the phone ", "the device "),
            f"so my {base.replace('phone ', '')}, any quick fix?",
            " ".join(w for w in re.findall(r"[a-z0-9]+", base) if w not in {"the", "a", "an", "is", "are", "my"})
            + f" {dom} fix",
            f"{base[:1].upper() + base[1:]} on my phone{qual}.",
            f"How do I fix it when my {base}?",
            f"Ugh, my {base} again and it's driving me crazy!",
            _typo(f"my {base} please help"),
            f"Hey, I noticed that my {base}. What should I do?",
            f"I'm not sure why, but lately my {base}.",
            f"{(intent.domain or 'Device')} troubleshooting required: {base} (Android phone or tablet).",
        ]
        return dedupe_variations(query, variants)[:VARIATIONS_MAX]


def _typo(text: str) -> str:
    """Deterministic typo injection: swap two inner letters of every 3rd long word."""
    out = []
    for i, w in enumerate(text.split()):
        if len(w) >= 5 and i % 3 == 1:
            w = w[:2] + w[3] + w[2] + w[4:]
        out.append(w)
    return " ".join(out)


def _norm_for_dedupe(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9]+", fold(text)))


def dedupe_variations(query: str, variations: Sequence[str], *, max_jaccard: float = 0.85) -> list[str]:
    """Drop empty, duplicate and near-identical variations (and copies of the query)."""
    out: list[str] = []
    seen: list[frozenset[str]] = [_norm_for_dedupe(query)]
    for v in variations:
        if not isinstance(v, str):
            continue
        v = re.sub(r"\s+", " ", v).strip()
        if not v:
            continue
        toks = _norm_for_dedupe(v)
        if not toks:
            continue
        dup = False
        for s in seen:
            inter = len(toks & s)
            union = len(toks | s) or 1
            if inter / union >= max_jaccard:
                dup = True
                break
        if not dup:
            out.append(v)
            seen.append(toks)
    return out


def finalize_variations(query: str, primary: Sequence[str], fallback: Sequence[str]) -> list[str]:
    """8–10 distinct variations: LLM ones first, topped up with deterministic templates."""
    merged = dedupe_variations(query, list(primary) + list(fallback))
    if len(merged) < VARIATIONS_MIN:
        extra = [f"{v} (variant {i + 1})" for i, v in enumerate(fallback)]
        merged = dedupe_variations(query, merged + extra, max_jaccard=0.99)
    return merged[:VARIATIONS_MAX]


def fingerprint_text(text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", (text or "").strip()).encode("utf-8")).hexdigest()
