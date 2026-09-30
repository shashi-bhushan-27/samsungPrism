"""Deterministic text rules: word counts, sentence/Title Case, imperative steps, one interaction per step.

These are the programmatic replacements for "prompt-only constraints" (PDF §7.5).
"""

from __future__ import annotations

import re
from typing import Iterable

# --------------------------------------------------------------------------- words
_ALNUM = re.compile(r"[A-Za-z0-9]")


def words(text: str) -> list[str]:
    """Whitespace tokens containing at least one letter or digit."""
    return [w for w in (text or "").split() if _ALNUM.search(w)]


def count_words(text: str) -> int:
    return len(words(text))


def _core(token: str) -> str:
    return token.strip(".,;:!?\"'()[]{}")


# -------------------------------------------------------------------------- casing
# Proper nouns / product names that stay capitalised inside sentence-case text.
PROPER_NOUNS = frozenset(
    {
        "Galaxy", "Samsung", "Bluetooth", "Wi-Fi", "WiFi", "Android", "Google", "Bixby", "DeX", "Knox",
        "SmartThings", "Gemini", "Chrome", "YouTube", "Play", "Store", "Members", "Pen", "Watch", "Buds",
    }
)
# Multi-word names whose later tokens are capitalised: ("One", "UI"), ("S", "Pen"), ...
_PROPER_PAIRS = {("One", "UI"), ("S", "Pen"), ("Smart", "Switch"), ("Play", "Store"), ("Samsung", "Members")}

MINOR_WORDS = frozenset(
    {
        "a", "an", "the", "and", "but", "or", "nor", "for", "so", "yet", "at", "by", "in", "of", "on", "to",
        "up", "as", "off", "per", "via", "with", "from", "into", "onto", "over", "vs",
    }
)


_LONG_ACRONYMS = frozenset({"AMOLED", "OLED", "LCD", "HDR10", "MMS", "SMS", "APN", "VPN", "USB-C"})


def is_acronym(token: str) -> bool:
    core = _core(token)
    if core in _LONG_ACRONYMS:
        return True
    letters = re.sub(r"[^A-Za-z]", "", core)
    if any(ch.isdigit() for ch in core) and letters:  # 5G, 120Hz, S23, 4K
        return True
    return 2 <= len(letters) <= 4 and letters.isupper()


def _is_proper(tokens: list[str], i: int) -> bool:
    core = _core(tokens[i])
    if core in PROPER_NOUNS:
        return True
    if i > 0 and (_core(tokens[i - 1]), core) in _PROPER_PAIRS:
        return True
    if i + 1 < len(tokens) and (core, _core(tokens[i + 1])) in _PROPER_PAIRS:
        return True
    return False


def _first_alpha(token: str) -> str:
    for ch in token:
        if ch.isalpha():
            return ch
    return ""


def _is_shouting(toks: list[str]) -> bool:
    letters = re.sub(r"[^A-Za-z]", "", "".join(toks))
    return len(toks) > 1 and len(letters) > 5 and letters.isupper()


def is_sentence_case(text: str) -> bool:
    toks = words(text)
    if not toks or _is_shouting(toks):
        return False
    first = _first_alpha(toks[0])
    if first and not first.isupper():
        return False
    core0 = re.sub(r"[^A-Za-z]", "", _core(toks[0]))
    if len(core0) > 5 and core0.isupper():  # SHOUTING
        return False
    for i, tok in enumerate(toks[1:], start=1):
        ch = _first_alpha(tok)
        if not ch or not ch.isupper():
            continue
        if is_acronym(tok) or _is_proper(toks, i):
            continue
        return False
    return True


def to_sentence_case(text: str) -> str:
    toks = (text or "").split()
    if _is_shouting(toks):
        toks = [t.lower() for t in toks]
    out: list[str] = []
    for i, tok in enumerate(toks):
        if is_acronym(tok) or _is_proper(toks, i):
            out.append(tok)
        elif i == 0:
            low = tok.lower()
            idx = next((k for k, ch in enumerate(low) if ch.isalpha()), None)
            out.append(low if idx is None else low[:idx] + low[idx].upper() + low[idx + 1 :])
        else:
            out.append(tok.lower())
    return " ".join(out)


def is_title_case(text: str) -> bool:
    toks = words(text)
    if not toks:
        return False
    for i, tok in enumerate(toks):
        ch = _first_alpha(tok)
        if not ch:
            continue
        if ch.isupper() or is_acronym(tok) or _is_proper(toks, i):
            continue
        if i > 0 and _core(tok).lower() in MINOR_WORDS:
            continue
        return False
    return True


def _cap_word(tok: str) -> str:
    parts = tok.split("-")
    fixed = []
    for p in parts:
        idx = next((k for k, ch in enumerate(p) if ch.isalpha()), None)
        fixed.append(p if idx is None else p[:idx] + p[idx].upper() + p[idx + 1 :])
    return "-".join(fixed)


def to_title_case(text: str) -> str:
    toks = (text or "").split()
    out: list[str] = []
    for i, tok in enumerate(toks):
        if is_acronym(tok) or _is_proper(toks, i):
            out.append(tok)
        elif i > 0 and _core(tok).lower() in MINOR_WORDS:
            out.append(tok.lower())
        else:
            out.append(_cap_word(tok if any(c.isupper() for c in tok[1:]) and not tok.isupper() else tok.lower()))
    return " ".join(out)


# ---------------------------------------------------------------------- imperative
IMPERATIVE_VERBS = frozenset(
    """
    open tap touch press hold select choose pick toggle turn switch swipe slide drag scroll go navigate return
    launch enable disable activate deactivate allow deny block grant revoke check verify review view find locate
    search look clear delete remove uninstall install reinstall update download upgrade restart reboot reset restore
    back sign log set adjust change lower reduce decrease raise increase limit put keep leave let make use try wait
    charge recharge plug unplug connect reconnect disconnect pair unpair forget insert eject clean wipe dry cool move
    place take bring visit contact call close exit quit stop force end start run perform follow confirm accept agree
    enter type input save apply edit add create rename manage configure customize calibrate test compare repeat
    release power flip rotate shake uncheck tick untick mark unmark opt avoid do ensure replace swap deselect expand
    collapse pull push pinch zoom record capture shoot focus dismiss skip share send sync archive free empty optimize
    scan boot shut lock unlock mute unmute silence re-enable re-pair double-tap long-press note hit drag-and-drop
    disconnect keep uncheck cancel resume pause refresh reopen relaunch reconnect retry defragment toggle-off
    tap-and-hold clean-up tidy wipe-off inspect examine remove detach attach reattach reinsert re-insert allow
    upload backup back-up transfer copy paste highlight scan reapply
    """.split()
)
_NEGATIVE_IMPERATIVE = re.compile(r"(?i)^(?:do\s+not|don't|never|avoid)\b")
_LEADING_ADVERBS = (
    "optionally", "then", "next", "finally", "now", "again", "first", "also", "additionally", "afterwards",
    "subsequently", "lastly", "once more", "if needed", "if necessary", "if prompted", "if available",
    "if desired", "if required", "where available", "when prompted", "if applicable",
)
_LEADING_CLAUSE = re.compile(
    r"(?i)^(?:if|when|once|after|before|to|while|where|for|on|in|from|at|under|within|inside|using|with)\b[^,]{1,80},\s*"
)
NON_IMPERATIVE_STARTERS = frozenset(
    """
    you your the this that these those it its there a an we i my our is are was were can could should would will
    may might must if when because since although though however which who what how why
    """.split()
)


def core_clause(step: str) -> str:
    """Strip numbering, 'Please', leading adverbs and a leading subordinate clause."""
    s = (step or "").strip()
    s = re.sub(r"^\s*(?:step\s*\d+\s*[:.)-]|\d+\s*[.)]|[-*•]\s+)\s*", "", s, flags=re.I)
    s = re.sub(r"(?i)^please\s+", "", s)
    changed = True
    while changed:
        changed = False
        low = s.lower()
        for adv in _LEADING_ADVERBS:
            if low.startswith(adv + " ") or low.startswith(adv + ","):
                s = s[len(adv) :].lstrip(" ,")
                changed = True
                break
        m = _LEADING_CLAUSE.match(s)
        if m and len(s) > m.end():
            s = s[m.end() :]
            changed = True
        toks = s.split(None, 2)
        # "Gently clean ...", "Carefully remove ..." — skip a leading -ly adverb before a verb.
        if len(toks) >= 2 and toks[0].lower().endswith("ly") and _core(toks[1]).lower() in IMPERATIVE_VERBS:
            s = s[len(toks[0]) :].lstrip()
            changed = True
    return s.strip()


def first_word(text: str) -> str:
    toks = words(text)
    return _core(toks[0]).lower() if toks else ""


def is_imperative(step: str) -> tuple[bool, str]:
    clause = core_clause(step)
    if not clause:
        return False, "empty_step"
    if _NEGATIVE_IMPERATIVE.match(clause):
        return True, "negative_imperative"
    if re.match(r"(?i)^(?:make|be)\s+sure\b", clause):
        return True, "make_sure"
    w = first_word(clause)
    if w in NON_IMPERATIVE_STARTERS:
        return False, f"non_imperative_start:{w}"
    if w in IMPERATIVE_VERBS:
        return True, "verb"
    return False, f"unrecognized_verb:{w}"


# ------------------------------------------------------- one interaction per step
_HOLD_PAIRS = re.compile(r"(?i)\b(?:press|tap|touch|click)\s+and\s+hold\b|\bdrag\s+and\s+drop\b")
_BOUNDARY = re.compile(
    r"(?P<sep>,\s*and\s+then\s+|,\s*then\s+|\s+and\s+then\s+|;\s*then\s+|;\s*|\s+then\s+|,\s*and\s+|\s+and\s+|,\s+|\.\s+)"
)
_PATH_SEP = re.compile(r"\s*(?:>|→|->|»|›)\s*")
_TRAILING_PREPOSITIONS = frozenset({"to", "into", "onto", "on", "at", "in", "from", "with", "and"})
# One UI labels that contain "and" followed by a verb-like word.
_AND_LABELS = re.compile(
    r"(?i)\b(?:download\s+and\s+install|back\s*up\s+and\s+restore|backup\s+and\s+restore|search\s+and\s+replace"
    r"|drag\s+and\s+drop|scan\s+and\s+fix|check\s+and\s+update|pause\s+and\s+resume)\b"
)


def _is_verb_token(tok: str) -> bool:
    return _core(tok).lower() in IMPERATIVE_VERBS


_CLAUSE_FOLLOWERS = frozenset(
    """
    the a an your it its them this that these those all any each every on off up down back out to again until
    for in from with into away about over my our both some
    """.split()
)


def _starts_clause(rest: str) -> bool:
    """True when `rest` reads like '<verb> <object>' ("tap Battery", "turn on X", "select the apps")."""
    toks = rest.split()
    if len(toks) < 2:
        return False
    follower = _core(toks[1])
    return follower[:1].isupper() or follower.lower() in _CLAUSE_FOLLOWERS or follower[:1].isdigit()


def split_interactions(step: str) -> list[str]:
    """Split a step into single physical interactions (conservatively).

    A boundary splits only when the next clause starts with a *lowercase* imperative verb
    (capitalised words mid-sentence are UI labels, e.g. "Buttons and Swipe gestures"), or when
    it is a "then" boundary. Shared-object coordination ("Navigate to and open Settings") and
    simultaneous presses ("Press and hold") are kept whole.
    """
    text = (step or "").strip()
    if not text:
        return []
    # A leading adverb / subordinate clause ("If prompted, ...") belongs to the first interaction.
    core = core_clause(text)
    prefix = ""
    if core and text.endswith(core) and len(core) < len(text):
        prefix = text[: len(text) - len(core)]
        text = core
    masked = _HOLD_PAIRS.sub(lambda m: m.group(0).replace(" ", " "), text)
    masked = _AND_LABELS.sub(lambda m: m.group(0).replace(" ", " "), masked)
    parts: list[str] = []
    cursor = 0
    for m in _BOUNDARY.finditer(masked):
        left = masked[cursor : m.start()].strip()
        rest = masked[m.end() :]
        nxt = rest.split(" ", 1)[0] if rest else ""
        sep = m.group("sep")
        is_then = "then" in sep
        is_sentence = sep.strip().startswith(".")
        left_words = words(left)
        if not left_words:
            continue
        if _core(left_words[-1]).lower() in _TRAILING_PREPOSITIONS:
            continue  # "Navigate to | and open Settings"
        if is_sentence:
            split_here = bool(nxt) and _is_verb_token(nxt)
        elif is_then:
            split_here = bool(nxt)
        else:
            split_here = bool(nxt) and nxt[:1].islower() and _is_verb_token(nxt)
            if split_here and sep.strip() == "," and not _starts_clause(rest):
                split_here = False  # "a soft, dry cloth" — adjective list, not a new clause
            if split_here and re.fullmatch(r",?\s*and", sep.strip()):
                # "Tap Download and install." — an object-less verb right after a Capitalised
                # word continues a UI label rather than starting a new interaction.
                tail = words(rest.split(",")[0].split(";")[0])
                prev_word = _core(left_words[-1])
                if len(tail) == 1 and len(left_words) >= 2 and prev_word[:1].isupper():
                    split_here = False
        if len(left_words) == 1 and _is_verb_token(left_words[0]) and not is_then:
            split_here = False  # "Press | and hold" style verb coordination
        if split_here:
            parts.append(left)
            cursor = m.end()
    parts.append(masked[cursor:].strip())
    parts = [p.replace(" ", " ").strip(" ,;") for p in parts if p.strip(" ,;.")]
    if not parts:
        return [prefix + text] if prefix else [text]
    if prefix:
        parts[0] = prefix + parts[0]
    return parts


def navigation_path(step: str) -> list[str]:
    """Extract an explicit 'Settings > Display > Navigation bar' path from a step."""
    if not _PATH_SEP.search(step or ""):
        return []
    body = core_clause(step)
    body = re.sub(r"(?i)^(?:go\s+to|navigate\s+to|open|tap|select|head\s+to)\s+", "", body)
    body = body.rstrip(".!")
    segs = [s.strip(" .,'\"") for s in _PATH_SEP.split(body)]
    segs = [s for s in segs if s]
    return segs if len(segs) >= 2 else []


def count_interactions(step: str) -> int:
    path = navigation_path(step)
    base = len(split_interactions(step))
    if path:
        base += len(path) - 1
    return base


def normalise_step_text(step: str) -> str:
    s = re.sub(r"\s+", " ", (step or "").strip())
    s = re.sub(r"^\s*(?:step\s*\d+\s*[:.)-]|\d+\s*[.)]|[-*•]\s+)\s*", "", s, flags=re.I)
    s = re.sub(r"(?i)^please\s+", "", s)
    s = re.sub(r"(?i)^you\s+(?:should|can|need\s+to|must|may|will\s+need\s+to|have\s+to)\s+", "", s)
    if s and s[0].islower():
        s = s[0].upper() + s[1:]
    if s and s[-1] not in ".!?":
        s += "."
    return s


def _expand_path(piece: str) -> list[str]:
    path = navigation_path(piece)
    if not path:
        return [piece]
    out = [f"Open {path[0]}."]
    out.extend(f"Tap {seg}." for seg in path[1:])
    return out


def expand_step(step: str) -> list[str]:
    """Repair helper: turn one compound step into atomic imperative steps."""
    step = normalise_step_text(step)
    pieces = split_interactions(step)
    result: list[str] = []
    last_verb = ""
    for piece in pieces:
        p = piece.strip().rstrip(".")
        if not p:
            continue
        w = first_word(core_clause(p))
        if w in IMPERATIVE_VERBS:
            last_verb = w
        elif last_verb and len(pieces) > 1:
            p = f"{last_verb} {p}"
        for atom in _expand_path(normalise_step_text(p)):
            result.append(normalise_step_text(atom))
    return result or [step]


def dedupe_consecutive(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    for it in items:
        if out and out[-1].strip().lower() == it.strip().lower():
            continue
        out.append(it)
    return out
