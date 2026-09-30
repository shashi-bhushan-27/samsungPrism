"""Deterministic text normalisation shared by BM25, intent analysis and the target resolver."""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

try:  # shipped with fastembed; a light fallback keeps the module dependency-free
    from py_rust_stemmers import SnowballStemmer

    _STEMMER = SnowballStemmer("english")

    def _stem(word: str) -> str:
        return _STEMMER.stem_word(word)

except Exception:  # pragma: no cover - exercised only without the optional package

    def _stem(word: str) -> str:
        for suf in ("ations", "ation", "ings", "ing", "ers", "er", "ies", "es", "s", "ed", "ly", "ness"):
            if len(word) > len(suf) + 3 and word.endswith(suf):
                return word[: -len(suf)] + ("i" if suf == "ies" else "")
        return word


STOPWORDS = frozenset(
    """
    a an the and or but if then so of to in on at by for with from into onto over as is are was were be been being
    it its this that these those i me my we our you your he she they them their there here what which who whom
    how why when where do does did doing have has had can could should would will may might must shall just
    very really quite too also again only about out up down off all any some each every both few more most other
    such no nor not own same than s t don now please hi hello hey thanks thank ok okay im ive id dont cant wont
    phone galaxy samsung device mobile smartphone
    """.split()
)
# Tokens that must survive stop-wording because they change the meaning of a Settings target.
KEEP = frozenset({"on", "off", "up", "down", "not", "no", "more", "all", "sd", "hd"})

_BRIT = {
    "optimise": "optimize", "optimised": "optimized", "optimising": "optimizing", "optimisation": "optimization",
    "colour": "color", "colours": "colors", "centre": "center", "centres": "centers", "favourite": "favorite",
    "favourites": "favorites", "customise": "customize", "recognise": "recognize", "recognised": "recognized",
    "stabilise": "stabilize", "stabilisation": "stabilization", "minimise": "minimize", "maximise": "maximize",
    "behaviour": "behavior", "licence": "license", "analyse": "analyze", "organise": "organize", "dialogue": "dialog",
    "grey": "gray", "programme": "program", "authorised": "authorized", "personalise": "personalize",
}
_COMPOUNDS = (
    (re.compile(r"\bwi[\s\-]?fi\b"), "wifi"),
    (re.compile(r"\be[\s\-]mail\b"), "email"),
    (re.compile(r"\bauto[\s\-]focus\b"), "autofocus"),
    (re.compile(r"\bre[\s\-]?boot\b"), "reboot"),
    (re.compile(r"\bmicro[\s\-]?sd\b"), "sd"),
    (re.compile(r"\bqr[\s\-]?codes?\b"), "qr code"),
    (re.compile(r"\bhot[\s\-]?spot\b"), "hotspot"),
    (re.compile(r"\bset[\s\-]up\b"), "setup"),
    (re.compile(r"\blow[\s\-]light\b"), "low light"),
    (re.compile(r"\balways[\s\-]on[\s\-]display\b"), "always on display aod"),
    (re.compile(r"\bnav[\s\-]?bar\b"), "navigation bar"),
    (re.compile(r"\b(\d+)\s*%"), r"\1 percent"),
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def fold(text: str) -> str:
    """NFKC, lower-case, unify quotes/dashes, expand compounds, British → American spelling."""
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = t.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"').replace("–", "-").replace("—", "-")
    for rx, rep in _COMPOUNDS:
        t = rx.sub(rep, t)
    return " ".join(_BRIT.get(w, w) for w in t.split())


@lru_cache(maxsize=65536)
def stem(word: str) -> str:
    return _stem(word)


def tokens(text: str, *, keep_stopwords: bool = False) -> list[str]:
    """Stemmed content tokens."""
    out: list[str] = []
    for w in _TOKEN_RE.findall(fold(text)):
        if not keep_stopwords and w in STOPWORDS and w not in KEEP:
            continue
        out.append(stem(w))
    return out


def token_set(text: str) -> frozenset[str]:
    return frozenset(tokens(text))


def overlap_f1(a: frozenset[str] | set[str], b: frozenset[str] | set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    p, r = inter / len(a), inter / len(b)
    return 2 * p * r / (p + r)


def containment(small: frozenset[str] | set[str], big: frozenset[str] | set[str]) -> float:
    """Fraction of `small` present in `big`."""
    if not small:
        return 0.0
    return len(small & big) / len(small)
