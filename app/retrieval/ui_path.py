"""Deterministic parse of UI steps into a navigation path.

    ["Open Settings.", "Tap Display.", "Tap Navigation bar.", "Select ... Buttons and Swipe gestures.",
     "Optionally toggle on Gesture hint ..."]
      → root=Settings, screens=[Display, Navigation bar], values=[navigation type], controls=[Gesture hint]

The resolver uses this to know which Settings screen is the exact target (deepest screen or
the primary control) and which catalog candidates are merely parent menus on the way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from app.retrieval.text import overlap_f1, token_set
from app.validation.text_rules import IMPERATIVE_VERBS, core_clause, navigation_path

ROOT_SETTINGS = "Settings"
KNOWN_APPS = (
    "camera", "gallery", "galaxy store", "play store", "samsung members", "phone", "messages", "contacts",
    "clock", "calendar", "my files", "internet", "chrome", "youtube", "settings", "samsung health",
)
GENERIC_BUTTONS = frozenset(
    {"ok", "okay", "apply", "done", "confirm", "add", "save", "allow", "continue", "next", "back", "cancel", "yes",
     "got it", "start", "finish", "accept", "agree", "close"}
)

_ROOT_RE = re.compile(
    r"(?i)^(?:navigate\s+to\s+and\s+open|navigate\s+to|open|launch|go\s+to|start|head\s+to)\s+(?:the\s+|your\s+)?"
    r"(?P<app>[A-Za-z][\w\- ]{0,40}?)(?:\s+app(?:lication)?)?(?:\s+(?:on|from)\s+.+)?[.!]?$"
)
_TAP_RE = re.compile(
    r"(?i)^(?:tap|touch|press|click|go\s+into)\s+(?:on\s+)?(?:the\s+)?(?P<label>.+?)"
    r"(?:\s+(?:option|menu|tab|icon|button|entry|item))?(?:\s+(?:to|so|if|until|and\s+then|when|for)\s+.*)?[.!]?$"
)
_TOGGLE_RE = re.compile(
    r"(?i)^(?:turn|toggle|switch)\s+(?P<state>on|off)\s+(?:the\s+)?(?P<label>.+?)"
    r"(?:\s+(?:to|so|if|until|when|while|for|in\s+order\s+to)\s+.*)?[.!]?$"
)
# "Tap the switch next to Touch sensitivity to disable it" operates the Touch sensitivity control.
_SWITCH_NEXT_RE = re.compile(
    r"(?i)^(?:tap|touch|press|click)\s+(?:on\s+)?(?:the\s+)?(?:switch|toggle)\s+(?:next\s+to|beside|for)\s+(?:the\s+)?"
    r"(?P<label>.+?)(?:\s+to\s+(?P<verb>enable|disable|turn\s+(?:it\s+)?on|turn\s+(?:it\s+)?off)\b.*)?(?:\s+(?:to|so|if|until|when)\s+.*)?[.!]?$"
)
_ENABLE_RE = re.compile(
    r"(?i)^(?P<verb>enable|activate|disable|deactivate)\s+(?:the\s+)?(?P<label>.+?)(?:\s+(?:to|so|if|until|when|while)\s+.*)?[.!]?$"
)
_HARDWARE_RE = re.compile(
    r"(?i)^(?:press|hold|push|touch\s+and\s+hold|press\s+and\s+hold|tap\s+and\s+hold)\s+(?:and\s+hold\s+)?(?:the\s+)?"
    r"(?:side|power|volume|home|bixby|back)\b|^(?:touch|press)\s+and\s+hold\s+power\s+off\b"
)
_DEVICE_LABEL = re.compile(r"(?i)^(?:the\s+|your\s+)?(?:phone|device|mobile|galaxy)\b")
_FULL_TAIL = re.compile(r"[.!]+$")
_SLIDER_RE = re.compile(r"(?i)^(?:drag|slide|move|adjust)\s+(?:the\s+)?(?P<label>.+?)\s+slider\b")
_SELECT_RE = re.compile(
    r"(?i)^(?:select|choose|pick|set)\s+(?:your\s+preferred\s+|your\s+|the\s+|a\s+|an\s+)?(?P<label>.+?)"
    r"(?:\s+(?:as|to|for|between|from|under|in|on|such\s+as|or)\s+.*|,.*)?[.!]?$"
)


@dataclass(frozen=True)
class UiElement:
    label: str
    kind: str  # root | screen | button | control | slider | value | dynamic | other
    state: Optional[str] = None
    step_index: int = -1
    # Uncut label ("Put unused apps to sleep") when `label` was cut at a purpose clause.
    full_label: Optional[str] = None

    @property
    def tokens(self) -> frozenset[str]:
        return token_set(self.label)

    @property
    def label_variants(self) -> tuple[str, ...]:
        if self.full_label and self.full_label != self.label:
            return (self.label, self.full_label)
        return (self.label,)


@dataclass
class UiPath:
    root: Optional[str] = None
    elements: list[UiElement] = field(default_factory=list)

    @property
    def screens(self) -> list[UiElement]:
        return [e for e in self.elements if e.kind in ("screen", "dynamic")]

    @property
    def interactions(self) -> list[UiElement]:
        return [e for e in self.elements if e.kind in ("control", "slider", "button", "value")]

    @property
    def deepest_screen(self) -> Optional[UiElement]:
        s = self.screens
        return s[-1] if s else None

    @property
    def is_settings_navigation(self) -> bool:
        """A path that opens a Settings screen (system Settings or an app's settings page)."""
        if self.root is None:
            return False
        if self.root.lower() == "settings":
            return bool(self.screens or self.interactions)
        return any(e.label.lower() == "settings" for e in self.screens)

    def context_labels(self) -> list[str]:
        return [e.label for e in self.elements if e.kind != "other"]


def _clean_label(label: str) -> str:
    label = re.sub(r"\s+", " ", label).strip(" .,:;'\"")
    label = re.sub(r"(?i)^(?:the|your|a|an)\s+", "", label)
    return label[:80]


def _app_root(clause: str) -> Optional[str]:
    m = _ROOT_RE.match(clause)
    if not m:
        return None
    app = _clean_label(m.group("app"))
    low = app.lower()
    if low in KNOWN_APPS or clause.lower().rstrip(".").endswith(" app") or low.endswith("settings"):
        return "Settings" if low == "settings" else app
    return None


def parse_ui_path(steps: Sequence[str]) -> UiPath:
    path = UiPath()
    expanded: list[tuple[int, str]] = []
    for i, step in enumerate(steps):
        segs = navigation_path(step)
        if segs:
            expanded.append((i, f"Open {segs[0]}."))
            expanded.extend((i, f"Tap {s}.") for s in segs[1:])
        else:
            expanded.append((i, step))
    for i, step in expanded:
        clause = core_clause(step)
        if not clause:
            continue
        root = _app_root(clause)
        if root is not None:
            if path.root is None or not path.elements:
                path.root = root
            else:
                path.elements.append(UiElement(root, "root", step_index=i))
            continue
        if _HARDWARE_RE.match(clause):
            path.elements.append(UiElement(_clean_label(clause), "other", None, i))
            continue
        m = _SWITCH_NEXT_RE.match(clause)
        if m:
            verb = (m.group("verb") or "").lower()
            state = "off" if ("disable" in verb or verb.endswith("off")) else ("on" if verb else None)
            path.elements.append(UiElement(_clean_label(m.group("label")), "control", state, i))
            continue
        m = _TOGGLE_RE.match(clause)
        if m:
            if _DEVICE_LABEL.match(m.group("label")):  # "Turn off the phone" is not a setting
                path.elements.append(UiElement(_clean_label(clause), "other", None, i))
                continue
            full = _clean_label(_FULL_TAIL.sub("", re.sub(r"(?i)^(?:turn|toggle|switch)\s+(?:on|off)\s+", "", clause)))
            path.elements.append(
                UiElement(_clean_label(m.group("label")), "control", m.group("state").lower(), i, full)
            )
            continue
        m = _ENABLE_RE.match(clause)
        if m:
            state = "on" if m.group("verb").lower() in ("enable", "activate") else "off"
            path.elements.append(UiElement(_clean_label(m.group("label")), "control", state, i))
            continue
        m = _SLIDER_RE.match(clause)
        if m:
            path.elements.append(UiElement(_clean_label(m.group("label")), "slider", None, i))
            continue
        m = _TAP_RE.match(clause)
        if m:
            label = _clean_label(m.group("label"))
            first = label.split(" ", 1)[0].lower() if label else ""
            if label.lower() in GENERIC_BUTTONS:
                path.elements.append(UiElement(label, "other", None, i))
            elif first in IMPERATIVE_VERBS:
                path.elements.append(UiElement(label, "button", None, i))
            else:
                path.elements.append(UiElement(label, "screen", None, i))
            continue
        m = _SELECT_RE.match(clause)
        if m:
            full = _clean_label(_FULL_TAIL.sub("", re.sub(r"(?i)^(?:select|choose|pick|set)\s+(?:your\s+preferred\s+|your\s+)?", "", clause)))
            label = _clean_label(m.group("label"))
            if len(full.split()) <= 4 and full[:1].isupper():
                label = full  # short capitalised option label such as "Tap to show"
            path.elements.append(UiElement(label, "value", None, i, full))
            continue
        path.elements.append(UiElement(_clean_label(clause), "other", None, i))
    # A selected value followed by a real navigation tap opens a dynamic screen (e.g. an app's info
    # page); a verb-initial label that is navigated *through* ("Lock screen and AOD", "Reset") is a screen.
    fixed: list[UiElement] = []
    for idx, el in enumerate(path.elements):
        later = [x for x in path.elements[idx + 1 :] if x.kind in ("screen", "button", "control", "slider")]
        if el.kind == "value" and any(x.kind in ("screen", "button") for x in later):
            fixed.append(UiElement(el.label, "dynamic", None, el.step_index, el.full_label))
        elif el.kind == "button" and later:
            fixed.append(UiElement(el.label, "screen", None, el.step_index, el.full_label))
        else:
            fixed.append(el)
    path.elements = fixed
    return path


def primary_interaction(path: UiPath, action_name: str, description: str = "") -> Optional[UiElement]:
    """The control/value/button the action is about.

    * exactly one toggle/slider/button → it is the target, whatever the action is called
      (an LLM may name the action after the parent screen, e.g. "Camera Settings");
    * otherwise the interaction best named by the actionName/description (overlap ≥ 0.5).
    """
    candidates = [e for e in path.interactions if e.label.lower() not in GENERIC_BUTTONS and e.tokens]
    operations = [e for e in candidates if e.kind in ("control", "slider", "button")]
    if len(operations) == 1 and not any(e.kind == "value" for e in candidates):
        return operations[0]
    name_tokens = token_set(action_name) | token_set(description)
    best, best_score = None, 0.0
    for el in candidates:
        score = 0.0
        for variant in el.label_variants:
            t = token_set(variant)
            if t:
                score = max(score, overlap_f1(t, name_tokens), len(t & name_tokens) / len(t))
        if score > best_score + 1e-9:
            best, best_score = el, score
    return best if best_score >= 0.5 else None


def all_labels(paths: Iterable[UiPath]) -> list[str]:
    out: list[str] = []
    for p in paths:
        out.extend(p.context_labels())
    return out
