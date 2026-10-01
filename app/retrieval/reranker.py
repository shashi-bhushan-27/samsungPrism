"""Exact target-screen resolution with parent-menu protection (PDF §7.2 / §6.2).

For one action:
  1. parse its steps into a UI path (root → screens → primary control / buttons / values)
  2. retrieve hybrid BM25 + dense candidates over catalog metadata
  3. score every candidate against the *exact* target (primary control, else deepest screen),
     flag parents (candidates that only match a screen on the way), entity mismatches
     ("Camera app storage" for a generic app), and control-type compatibility
  4. pick the best exact candidate; ties between genuinely different screens → no guess
  5. no exact candidate → bixby://dummy_positive for an auto action that opens a valid
     Settings screen (PDF §3), otherwise no deeplink.
The URI is never generated: it is the `.uri` of a registry entry, copied verbatim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np

from app.core.constants import CATEGORY_AUTO, CATEGORY_CRITICAL, CATEGORY_MANUAL
from app.models.internal import ExtractedAction
from app.retrieval.embeddings import EmbeddingProvider
from app.retrieval.hybrid import Candidate, HybridRetriever
from app.retrieval.indexes import CatalogDoc, CatalogIndex, label_tokens
from app.retrieval.text import containment, token_set
from app.retrieval.ui_path import UiElement, UiPath, parse_ui_path, primary_interaction
from app.validation.text_rules import is_acronym

# Label exactness dominates; fused retrieval evidence breaks ties and gates weak matches.
W_FUSED = 0.25
W_PRIMARY = 0.45
W_SCREEN = 0.45
W_PRIMARY_EXT = 0.10
W_PATH = 0.05
W_CONTROL = 0.05
P_PARENT = 0.50
P_ENTITY = 0.40
P_ROOT = 0.15
P_DOMAIN = 0.03


@dataclass
class MappingDecision:
    kind: str  # catalog | dummy | none
    doc: Optional[CatalogDoc]
    confidence: float
    reason: str
    path: UiPath
    primary: Optional[UiElement]
    candidates: list[Candidate] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)
    dummy_description: Optional[str] = None
    dummy_message: Optional[str] = None

    @property
    def uri(self) -> Optional[str]:
        return self.doc.uri if self.doc else None

    def trace(self, limit: int = 5) -> dict:
        return {
            "kind": self.kind,
            "uri": self.uri,
            "confidence": round(self.confidence, 4),
            "reason": self.reason,
            "primary": self.primary.label if self.primary else None,
            "target_screen": self.path.deepest_screen.label if self.path.deepest_screen else None,
            "ambiguous": self.ambiguous,
            "candidates": [c.trace() for c in self.candidates[:limit]],
        }


def _match(lbl: frozenset[str], el: frozenset[str], ctx: frozenset[str]) -> float:
    """Candidate label vs. UI element: the element must be covered and the label explained."""
    if not lbl or not el:
        return 0.0
    cover = len(el & lbl) / len(el)
    prec = len(lbl & (el | ctx)) / len(lbl)
    if cover >= 0.75 and prec > 0.5:
        return 0.5 * (cover + prec)
    return 0.0


def _compat_primary(primary: UiElement, control: str) -> float:
    if primary.kind == "control":
        return 1.0 if control == "toggle" else 0.4 if control in ("screen", "list") else 0.2
    if primary.kind == "slider":
        return 1.0 if control == "slider" else 0.4 if control in ("screen", "list") else 0.2
    if primary.kind == "button":
        return 1.0 if control == "button" else 0.6 if control == "screen" else 0.3
    return 1.0 if control in ("list", "toggle") else 0.6 if control == "screen" else 0.3  # value selection


def _lower_label(label: str) -> str:
    toks = label.split()
    out = []
    for i, t in enumerate(toks):
        out.append(t if is_acronym(t) or t in ("Wi-Fi", "Bluetooth", "Galaxy", "Samsung") else t.lower())
    return " ".join(out)


_NAME_STATE = re.compile(
    r"(?i)^(?P<verb>enable|disable|activate|deactivate|turn on|turn off|switch on|switch off)\s+(?:the\s+)?(?P<label>.+)$"
)


def dummy_texts(path: UiPath, primary: Optional[UiElement]) -> tuple[str, str]:
    """Deterministic target description for bixby://dummy_positive (Appendix B style)."""
    screens = path.screens
    target = screens[-1] if screens else None
    parent = screens[-2] if len(screens) >= 2 else None
    root = path.root if path.root and path.root.lower() != "settings" else None
    if target is None:
        name = primary.label if primary else (root or "Settings")
        desc = f"Open {_lower_label(name)} settings" + (f" in {root}" if root else "")
    elif target.kind == "dynamic":
        where = parent.label if parent else (root or "Settings")
        desc = f"Open {where} and select the {_lower_label(target.label)}"
    elif parent is not None and parent.kind == "dynamic":
        desc = f"Open {_lower_label(target.label)} settings for the selected {_lower_label(parent.label)}"
    elif parent is not None:
        desc = f"Open {_lower_label(target.label)} settings under {parent.label}"
    else:
        desc = f"Open {_lower_label(target.label)} settings" + (f" in {root}" if root else "")
    where = parent.label if (parent is not None and parent.kind != "dynamic") else (target.label if target else (root or "Settings"))
    focus = primary or next((e for e in path.interactions if e.kind == "value"), None)
    if focus is None:
        msg = f"Open {target.label if target else where}"
    elif focus.kind == "control":
        msg = f"Turn {focus.state or 'on'} {focus.label} in {where} settings"
    elif focus.kind == "slider":
        msg = f"Adjust {focus.label} in {where} settings"
    elif focus.kind == "button":
        msg = f"Tap {focus.label} in {where} settings"
    else:
        msg = f"Choose {_lower_label(focus.label)} in {where} settings"
    return desc, msg


class TargetResolver:
    def __init__(
        self,
        index: CatalogIndex,
        retriever: HybridRetriever,
        provider: EmbeddingProvider,
        *,
        top_k_rerank: int = 12,
        min_score: float = 0.5,
        min_margin: float = 0.03,
        use_bm25: bool = True,
        use_dense: bool = True,
    ):
        self.index = index
        self.retriever = retriever
        self.provider = provider
        self.top_k_rerank = top_k_rerank
        self.min_score = min_score
        self.min_margin = min_margin
        self.use_bm25 = use_bm25
        self.use_dense = use_dense

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _path_with_hints(action: ExtractedAction) -> UiPath:
        path = parse_ui_path(action.steps)
        if not path.elements and action.navigation_path:
            # LLM hint used only when the steps themselves carry no parsable navigation.
            nav = [p for p in action.navigation_path if p]
            if nav:
                path.root = "Settings" if nav[0].lower() == "settings" else nav[0]
                path.elements = [UiElement(p, "screen") for p in nav[1:]]
        # "Enable Touch Sensitivity" whose last step only taps "Touch sensitivity": the tapped item is the
        # control the name switches on/off (the catalog may hold only its on/off entries).
        m = _NAME_STATE.match(action.action_name or "")
        if m and path.elements and all(e is path.elements[-1] for e in path.interactions):
            last = path.elements[-1]
            if last.kind in ("screen", "button") and last.tokens and last.tokens <= token_set(m.group("label")):
                state = "off" if m.group("verb").lower() in ("disable", "turn off", "deactivate", "switch off") else "on"
                path.elements[-1] = UiElement(last.label, "control", state, last.step_index, last.full_label)
        return path

    @staticmethod
    def query_text(action: ExtractedAction, path: UiPath, primary: Optional[UiElement]) -> str:
        parts = [action.action_name]
        if primary is not None:
            parts += list(primary.label_variants) * 2
        deep = path.deepest_screen
        if deep is not None and deep.kind != "dynamic":
            parts += [deep.label] * 2
        parts += [e.label for e in path.elements if e.kind in ("screen", "control", "slider", "button", "value")]
        if path.root and path.root.lower() != "settings":
            parts.append(path.root)
        if action.target_screen:
            parts.append(action.target_screen)
        return ". ".join(p for p in parts if p)

    # ------------------------------------------------------------------ resolve
    def resolve(self, action: ExtractedAction, *, domain: Optional[str] = None) -> MappingDecision:
        path = self._path_with_hints(action)
        primary = primary_interaction(path, action.action_name, action.description)
        if action.category == CATEGORY_MANUAL:
            return MappingDecision("none", None, 0.0, "manual_action", path, primary)
        qtext = self.query_text(action, path, primary)
        qvec = self.provider.embed_query([qtext])[0] if self.use_dense else None
        cands = self.retriever.retrieve(qtext, qvec, use_bm25=self.use_bm25, use_dense=self.use_dense)
        cands = cands[: self.top_k_rerank]
        return self._decide(action, path, primary, cands, domain)

    def _decide(
        self,
        action: ExtractedAction,
        path: UiPath,
        primary: Optional[UiElement],
        cands: list[Candidate],
        domain: Optional[str],
    ) -> MappingDecision:
        root_tokens = token_set(path.root) if path.root and path.root.lower() != "settings" else frozenset()
        ctx = token_set(action.action_name) | root_tokens
        for e in path.elements:
            if e.kind != "other":
                ctx |= token_set(e.label)
        prim_variants = [label_tokens(v) for v in primary.label_variants] if primary else []
        prim_variants = [p for p in prim_variants if p]
        deep = path.deepest_screen
        screen_tokens: Optional[frozenset[str]] = None
        if deep is not None and deep.kind != "dynamic":
            screen_tokens = label_tokens(deep.label) or root_tokens or None
        screens = path.screens
        shallower = [label_tokens(e.label) or root_tokens for e in screens[:-1] if e.kind == "screen"]
        shallower = [s for s in shallower if s]
        # A target screen below a dynamic item (an app picked from a list) is item-specific.
        dyn_parent: Optional[frozenset[str]] = None
        for i, e in enumerate(screens):
            if e.kind == "dynamic" and i > 0:
                dyn_parent = label_tokens(screens[i - 1].label)
        other_path = [label_tokens(e.label) for e in screens[:-1] if e.kind == "screen" and label_tokens(e.label)]

        primary_found = False
        for c in cands:
            d = c.doc
            m_prim = max((_match(d.label_tokens, pt, ctx) for pt in prim_variants), default=0.0)
            m_prim_ext = max((containment(pt, d.ext_tokens) for pt in prim_variants), default=0.0)
            m_screen = _match(d.label_tokens, screen_tokens, ctx) if screen_tokens else 0.0
            if dyn_parent is not None and not (dyn_parent & d.ext_tokens):
                m_screen = 0.0
            m_parent = max((_match(d.label_tokens, st, ctx) for st in shallower), default=0.0)
            entity_mismatch = bool(d.entities - ctx)
            path_cons = (
                sum(1 for t in other_path if t & d.ext_tokens) / len(other_path) if other_path else 0.0
            )
            compat_primary = _compat_primary(primary, d.control) if (primary is not None and prim_variants) else 0.0
            compat_screen = 1.0 if d.control in ("screen", "list") else 0.5 if d.control == "button" else 0.3
            domain_mismatch = bool(domain and d.domain and d.domain not in (domain, "General"))
            # Inside an app (e.g. Camera > Settings) the target must belong to that app.
            root_inconsistent = bool(root_tokens) and not (
                root_tokens <= d.ext_tokens or (d.domain is not None and token_set(d.domain) & root_tokens)
            )
            c.signals.update(
                {
                    "m_primary": m_prim,
                    "m_primary_ext": m_prim_ext,
                    "m_screen": m_screen,
                    "m_parent": m_parent,
                    "path_consistency": path_cons,
                    "compat_primary": compat_primary,
                    "compat_screen": compat_screen,
                    "entity_mismatch": float(entity_mismatch),
                    "domain_mismatch": float(domain_mismatch),
                    "root_inconsistent": float(root_inconsistent),
                }
            )
            if m_prim > 0:
                primary_found = True
            if entity_mismatch:
                c.flags.append("entity_mismatch")
        for c in cands:
            s = c.signals
            # Control compatibility is judged against whichever target actually matched.
            s["control_compat"] = s["compat_primary"] if primary_found else s["compat_screen"]
            screen_w = W_SCREEN * (0.6 if primary_found else 1.0)
            c.exact = s["m_primary"] > 0 or (s["m_screen"] > 0 and not primary_found)
            c.parent = (not c.exact) and (s["m_parent"] > 0 or c.doc.is_root or (c.doc.is_top_level and (deep is not None or primary is not None)))
            if c.parent:
                c.flags.append("parent_menu")
            c.final = (
                W_FUSED * c.fused
                + W_PRIMARY * s["m_primary"]
                + screen_w * s["m_screen"]
                + W_PRIMARY_EXT * s["m_primary_ext"]
                + W_PATH * s["path_consistency"]
                + W_CONTROL * s["control_compat"]
                - P_PARENT * float(c.parent)
                - P_ENTITY * s["entity_mismatch"]
                - P_ROOT * s["root_inconsistent"]
                - P_DOMAIN * s["domain_mismatch"]
            )
        cands.sort(key=lambda c: (-c.final, c.doc.entry.index))
        viable = [c for c in cands if c.exact and not c.parent and "entity_mismatch" not in c.flags]
        if viable:
            top = viable[0]
            # Same label, different control type (toggle vs. its screen): the control type that
            # fits the interaction decides, not retrieval noise.
            same_label = [c for c in viable if c.doc.label_tokens == top.doc.label_tokens]
            if len(same_label) > 1:
                top = max(same_label, key=lambda c: (c.signals["control_compat"], c.final, -c.doc.entry.index))
            if top.final < self.min_score:
                return self._fallback(action, path, primary, cands, f"low_confidence:{top.final:.3f}")
            rivals = [
                c for c in viable
                if c is not top
                and c.doc.label_tokens != top.doc.label_tokens
                and c.uri not in self.index.registry.ambiguous_with(top.uri)
            ]
            if rivals:
                second = rivals[0]
                close = abs(top.final - second.final) < self.min_margin
                if close and second.signals["control_compat"] >= top.signals["control_compat"]:
                    return self._fallback(
                        action, path, primary, cands, "ambiguous_candidates", ambiguous=[top.uri, second.uri]
                    )
            return MappingDecision(
                "catalog", top.doc, float(min(1.0, top.final)), "exact_target", path, primary, cands
            )
        return self._fallback(action, path, primary, cands, "no_exact_candidate")

    def _fallback(
        self,
        action: ExtractedAction,
        path: UiPath,
        primary: Optional[UiElement],
        cands: list[Candidate],
        reason: str,
        ambiguous: Optional[list[str]] = None,
    ) -> MappingDecision:
        if action.category == CATEGORY_AUTO and path.is_settings_navigation:
            desc, msg = dummy_texts(path, primary)
            return MappingDecision(
                "dummy", None, 0.0, f"{reason}->dummy_positive", path, primary, cands, ambiguous or [], desc, msg
            )
        why = "critical_catalog_only" if action.category == CATEGORY_CRITICAL else "not_a_settings_screen"
        return MappingDecision("none", None, 0.0, f"{reason}->{why}", path, primary, cands, ambiguous or [])
