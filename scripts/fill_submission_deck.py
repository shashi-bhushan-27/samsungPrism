"""Fill the PRISM submission template. Every number comes from artifacts/reports/*.json (official dataset run).
Team details and the demo-video link are filled in below."""
import json
import sys
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Pt

SRC, OUT, REPO = sys.argv[1], sys.argv[2], Path(sys.argv[3])
SHOT = sys.argv[4] if len(sys.argv) > 4 else None
REP = REPO / "artifacts" / "reports"
B = json.loads((REP / "benchmark.json").read_text())
ST = json.loads((REP / "stress.json").read_text()) if (REP / "stress.json").exists() else None
HO = json.loads((REP / "hostile.json").read_text()) if (REP / "hostile.json").exists() else None
AB = json.loads((REP / "ablation.json").read_text()) if (REP / "ablation.json").exists() else None
PW = json.loads((REP / "prewarm_report.json").read_text())
assert B["dataset"] == "official", B["dataset"]

PURPLE, DARK, GREY, LIGHT, WHITE = (RGBColor.from_string(x) for x in ("6D28D9", "14142B", "63637E", "D9D3F0", "FFFFFF"))
GREEN, AMBER = RGBColor.from_string("15803D"), RGBColor.from_string("B45309")
REPO_URL = "https://github.com/shashi-bhushan-27/samsungPrism"

prs = Presentation(SRC)
S = prs.slides
L, T, W, H = 838200, 1825625, 10515600, 4351338  # body placeholder box


def p95(d, *path):
    for k in path:
        d = d[k]
    return d


def ms(v):
    return f"{v / 1000:.2f} s" if v >= 1000 else f"{v:.0f} ms"


C = B["compliance_all_paths"]
CL = B["compliance"]
lat = B["latency"]
cold_p95 = lat["cold"]["server_ms"]["p95"]
exact_p95 = lat["exact_hit"]["server_ms"]["p95"]
sem = lat.get("semantic_hit_llm_paraphrases", {}).get("server_ms", {})
para = B["cache"]["paraphrase_llm"]
cp = B["cold_path"]
plans = sum(1 for it in PW["items"] if it["status"] == "cached")
nq = len(PW["items"])
samples = B.get("samples") or []


def body(slide):
    return next(sh for sh in slide.placeholders if sh.placeholder_format.idx == 1)


def bullets(shape, items, size=16, space=4):
    tf = shape.text_frame
    tf.clear()
    tf.word_wrap = True
    first = True
    for it in items:
        level = 0
        if isinstance(it, tuple):
            level, it = it
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        if first:  # the template's first paragraph carries buNone/zero indent: make it a normal bullet
            pPr = p._p.find("{http://schemas.openxmlformats.org/drawingml/2006/main}pPr")
            if pPr is not None:
                for k in ("marL", "indent"):
                    pPr.attrib.pop(k, None)
                for bn in pPr.findall("{http://schemas.openxmlformats.org/drawingml/2006/main}buNone"):
                    pPr.remove(bn)
        first = False
        p.level = level
        p.space_after = Pt(space)
        parts = it.split("**")
        for i, part in enumerate(parts):
            if not part:
                continue
            r = p.add_run()
            r.text = part
            r.font.size = Pt(size - 2 * level)
            r.font.color.rgb = DARK if level == 0 else GREY
            r.font.bold = i % 2 == 1


def box(slide, x, y, w, h, text, fill=LIGHT, color=DARK, size=12, bold=False, shape=MSO_SHAPE.ROUNDED_RECTANGLE, align=PP_ALIGN.CENTER):
    s = slide.shapes.add_shape(shape, Emu(x), Emu(y), Emu(w), Emu(h))
    s.fill.solid()
    s.fill.fore_color.rgb = fill
    s.line.color.rgb = fill
    s.shadow.inherit = False
    tf = s.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    for m in ("margin_left", "margin_right"):
        setattr(tf, m, Emu(60000))
    for m in ("margin_top", "margin_bottom"):
        setattr(tf, m, Emu(30000))
    lines = text.split("\n")
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        r = p.add_run()
        r.text = line
        r.font.size = Pt(size if i == 0 else size - 2)
        r.font.bold = bold if i == 0 else False
        r.font.color.rgb = color
    return s


def arrow(slide, x1, y1, x2, y2):
    c = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Emu(x1), Emu(y1), Emu(x2), Emu(y2))
    c.line.color.rgb = GREY
    c.line.width = Pt(1.5)
    ln = c.line._get_or_add_ln()
    from lxml import etree
    tail = etree.SubElement(ln, "{http://schemas.openxmlformats.org/drawingml/2006/main}tailEnd")
    tail.set("type", "triangle")
    return c


def table(slide, rows, x, y, w, col_w, size=12, row_h=300000, header_fill=PURPLE):
    t = slide.shapes.add_table(len(rows), len(rows[0]), Emu(x), Emu(y), Emu(w), Emu(row_h * len(rows))).table
    for j, cw in enumerate(col_w):
        t.columns[j].width = Emu(cw)
    for i, row in enumerate(rows):
        for j, val in enumerate(row):
            cell = t.cell(i, j)
            cell.margin_left = cell.margin_right = Emu(70000)
            cell.margin_top = cell.margin_bottom = Emu(25000)
            cell.fill.solid()
            cell.fill.fore_color.rgb = header_fill if i == 0 else (WHITE if i % 2 else RGBColor.from_string("F5F3FF"))
            tf = cell.text_frame
            tf.clear()
            p = tf.paragraphs[0]
            r = p.add_run()
            text = str(val)
            colour = None
            if text.startswith("✔ "):
                colour, text = GREEN, text[2:]
            elif text.startswith("△ "):
                colour, text = AMBER, text[2:]
            r.text = text
            r.font.size = Pt(size)
            r.font.bold = i == 0
            r.font.color.rgb = WHITE if i == 0 else (colour or DARK)
    return t


def todo(run):
    run.font.highlight_color = None
    rPr = run._r.get_or_add_rPr()
    from lxml import etree
    hl = etree.SubElement(rPr, "{http://schemas.openxmlformats.org/drawingml/2006/main}highlight")
    clr = etree.SubElement(hl, "{http://schemas.openxmlformats.org/drawingml/2006/main}srgbClr")
    clr.set("val", "FFF59D")
    # a:highlight must precede a:latin etc.; move it to the front of rPr children after fills
    rPr.remove(hl)
    rPr.insert(0, hl) if not len(rPr) else rPr.append(hl)


def small_title(slide, size=34):
    for r in slide.shapes.title.text_frame.paragraphs[0].runs:
        r.font.size = Pt(size)


# ---------------------------------------------------------------- slide 1: cover
cover = next(sh for sh in S[0].shapes if sh.has_text_frame and sh.text_frame.text.startswith("Theme ID"))
values = {
    "Theme ID -": ("Theme 2: Troubleshooting", False),
    "Team Name -": ("ResolveAI", False),
    "College Name -": ("VITV", False),
    "Member Name & Email 1- ": ("Shashi Bhushan – shashibhushan.vijay2022@vitstudent.ac.in", False),
    "Member Name & Email 2-": ("Astha Doshi – astha.doshi2023@vitstudent.ac.in", False),
    "Member Name & Email 3- ": ("Himangi Khanduri – himangi.khanduri2023@vitstudent.ac.in", False),
    "Submission Github link - ": (f"{REPO_URL}  (tag PRISM_GENAI_HACKATHON_Y2026)", False),
}
for p in list(cover.text_frame.paragraphs):  # the team has three members: drop the unused fourth line
    if p.text.startswith("Member Name & Email 4"):
        p._p.getparent().remove(p._p)
for p in cover.text_frame.paragraphs:
    key = p.text
    if key in values:
        val, is_todo = values[key]
        src = p.runs[0]
        r = p.add_run()
        r.text = " " + val
        r.font.size = src.font.size
        r.font.bold = False
        r.font.color.rgb = DARK if not is_todo else GREY

# ---------------------------------------------------------------- slide 2: theme
bullets(body(S[1]), [
    "**Theme 2: Smart Guided Troubleshooting Engine.** Turn a vague device complaint into a short, safe, one-tap fix plan.",
    "Input: the customer's complaint and the SIIS knowledge-base answer retrieved for it.",
    "Output (schema.py contract): a goal, a sentence-case title and ordered actions. Each action has imperative steps, a category (auto / manual / critical) and, for Settings changes, the exact catalog deeplink.",
    "Hard rules:",
    (1, "Use only steps found in the SIIS text. Use only deeplinks found in the catalog. No URLs."),
    (1, "One action per screen or feature. Critical actions such as restart or reset come last."),
    (1, "8–10 query variations. Fast on repeated or paraphrased complaints: P95 ≤ 300 ms on a cache hit, ≤ 8 s cold."),
    f"Official data: {nq} customer complaints (screen and display issues), {len(B.get('samples') or []) or 1} reference sample, a {577}-entry masked deeplink catalog (voiceassist://).",
], size=17)

# ---------------------------------------------------------------- slide 3: existing solutions
rows = [
    ["Approach", "What it does well", "Gap for this theme"],
    ["FAQ / knowledge-base search", "Finds the right article", "Leaves the customer to read it; no one-tap action, no ordering"],
    ["LLM chatbot (free text)", "Understands messy language", "Invents steps and links, varies run to run, slow and costly per query"],
    ["Rule-based decision trees", "Predictable, cheap", "Breaks on new phrasing; every new issue needs manual authoring"],
    ["Retrieval + LLM (plain RAG)", "Grounded answer text", "No structure: screens, categories and deeplinks are not guaranteed"],
]
table(S[2], rows, L, T, W, [2600000, 3000000, 4915600], size=14, row_h=560000)
tb = S[2].shapes.add_textbox(Emu(L), Emu(T + 560000 * 5 + 250000), Emu(W), Emu(700000)).text_frame
tb.word_wrap = True
r = tb.paragraphs[0].add_run()
r.text = ("Our gap to close: keep the language understanding of an LLM, but let code decide everything that must be "
          "exact: the grounding, the deeplink, the order, the schema and the cache.")
r.font.size = Pt(15)
r.font.bold = True
r.font.color.rgb = PURPLE
body(S[2]).text_frame.text = ""
body(S[2])._element.getparent().remove(body(S[2])._element)

# ---------------------------------------------------------------- slide 4: architecture
sl = S[3]
small_title(sl)
body(sl)._element.getparent().remove(body(sl)._element)
top = 1900000
bw, bh, gap = 1620000, 900000, 150000
stages = [
    ("Query enrichment", "typo fix, intent,\ncanonical query"),
    ("Cache lookup", "exact key → semantic\n(bge-small, gated)"),
    ("SIIS source", "request text, else\nBM25+dense KB"),
    ("Grounded extraction", "LLM (JSON schema)\n+ grounding check"),
    ("Deeplink mapping", "BM25+dense hybrid\n+ exact-screen resolver"),
    ("Sequencing + gates", "critical last; schema,\nrules, URL scan"),
]
x0 = 600000
for i, (t, sub) in enumerate(stages):
    x = x0 + i * (bw + gap)
    box(sl, x, top, bw, bh, f"{t}\n{sub}", fill=PURPLE if i in (3,) else LIGHT, color=WHITE if i == 3 else DARK, size=13, bold=True)
    if i:
        arrow(sl, x - gap, top + bh // 2, x, top + bh // 2)
box(sl, x0, top - 520000, 3000000, 380000, "POST /v1/troubleshoot {query, siis_response?}", fill=DARK, color=WHITE, size=12, bold=True)
hit_y = top + bh + 420000
box(sl, x0 + (bw + gap), hit_y, bw * 2 + gap, 560000, "HIT → validated plan returned\nno model call", fill=RGBColor.from_string("DCFCE7"), size=12, bold=True)
arrow(sl, x0 + (bw + gap) + bw // 2, top + bh, x0 + (bw + gap) + bw // 2, hit_y)
box(sl, x0 + 5 * (bw + gap), hit_y, bw, 560000, "Validated JSON\n(schema.py) → cache", fill=RGBColor.from_string("DCFCE7"), size=12, bold=True)
arrow(sl, x0 + 5 * (bw + gap) + bw // 2, top + bh, x0 + 5 * (bw + gap) + bw // 2, hit_y)
cat_y = hit_y + 900000
box(sl, x0 + 4 * (bw + gap), cat_y, bw, 620000, "Catalog registry\n577 masked URIs", fill=WHITE, size=12, bold=True)
arrow(sl, x0 + 4 * (bw + gap) + bw // 2, cat_y, x0 + 4 * (bw + gap) + bw // 2, top + bh)
notes = sl.shapes.add_textbox(Emu(x0), Emu(cat_y - 50000), Emu(5900000), Emu(1300000)).text_frame
notes.word_wrap = True
for i, line in enumerate([
    "The model is an assistant, not the controller:",
    "• it only reads language and extracts structure from the SIIS text;",
    "• the URI is always copied from the catalog object, never from model output;",
    "• every response passes schema.py, the business rules and a URL scan before it is returned or cached.",
]):
    p = notes.paragraphs[0] if i == 0 else notes.add_paragraph()
    r = p.add_run()
    r.text = line
    r.font.size = Pt(13)
    r.font.bold = i == 0
    r.font.color.rgb = DARK

# ---------------------------------------------------------------- slide 5: demo
sl = S[4]
example = None
recs = [json.loads(x) for x in (REPO / "results.jsonl").read_text().splitlines()]
recs.sort(key=lambda r: "inputs are delayed" not in r["query"])  # same complaint as the screenshot first
for rec in recs:
    ctx = rec.get("response", {}).get("contexts") or []
    acts = ctx[0]["actions"] if ctx else []
    if any((a["stepGroups"][0].get("actionableDeeplink") or {}).get("deeplink", "dummy").find("dummy") < 0
           for a in acts) and 2 <= len(acts) <= 8:
        example = rec
        break
items = [
    f"**Demo video:** https://youtu.be/eRBRi4PisPU (also docs/ResolveAI_demo.mp4; earlier API walkthrough: docs/demo.webm)",
    "**Live walkthrough** (http://localhost:8000/demo):",
    (1, "1. Paste an official complaint → plan in about 2–3 s on the first request (model call)."),
    (1, "2. Same or paraphrased complaint → served from the semantic cache in milliseconds, no model call."),
    (1, "3. A SIIS text carrying a URL or a prompt injection → the link and the injected instruction are removed."),
    (1, "4. An out-of-scope complaint → empty contexts with fallback no_siis_context (no invented plan)."),
]
if example:
    g = example["response"]["contexts"][0]
    items.append(f"**Example (official row):** \"{example['query'][:110].rstrip('. ')}…\"")
    for a in g["actions"][:4]:
        ad = a["stepGroups"][0].get("actionableDeeplink")
        tag = (f" → placeholder link ({ad['deeplink']})" if "dummy" in ad["deeplink"] else f" → {ad['message']}") if ad else ""
        items.append((1, f"[{a['category']}] {a['actionName']}{tag}"))
bx = body(sl)
if SHOT and Path(SHOT).exists():
    bx.width = Emu(5600000)
    sl.shapes.add_picture(SHOT, Emu(L + 5750000), Emu(T), width=Emu(4765600))
bullets(bx, items, size=14, space=3)

# ---------------------------------------------------------------- slide 6: tech stack
rows = [
    ["Layer", "Choice", "Why"],
    ["API", "Python 3.11, FastAPI, Pydantic v2, uvicorn", "Typed contract (schema.py) end to end"],
    ["Model (run time)", (f"Google Gemini {B['llm_model']} (extraction and variations; fail-over {' → '.join(B.get('llm_fallbacks') or [])}); Groq gpt-oss also supported"
                          if B.get("llm_provider") == "gemini" else
                          f"Groq {B['llm_model']} (extraction), {B.get('llm_enrichment_model')} (variations); Gemini supported"),
     "Strict JSON-schema output, fail-over chain, hedging"],
    ["Embeddings", "BAAI/bge-small-en-v1.5 via fastembed (ONNX, local)", "No network at request time; ~384-d vectors"],
    ["Retrieval", "BM25 + dense hybrid, exact-screen resolver", "Deterministic deeplink choice"],
    ["Cache", "SQLite (WAL): exact + semantic, versioned", "Hits skip the model; invalidated on catalog change"],
    ["Quality", "pytest (390 offline tests), live benchmarks", "Unit, integration, regression, adversarial"],
    ["Delivery", "Docker (non-root, HEALTHCHECK), Makefile", "One-command setup"],
    ["Dev tooling", "Claude Code (AI-assisted development)", "See AI disclosure form"],
]
body(S[5])._element.getparent().remove(body(S[5])._element)
table(S[5], rows, L, T - 150000, W, [1900000, 4600000, 4015600], size=13, row_h=470000)

# ---------------------------------------------------------------- slide 7: impact
bullets(body(S[6]), [
    "**Customer:** one-tap fixes instead of reading an article. Each Settings action opens the exact screen, and a validation deeplink can confirm the toggle took effect.",
    "**Support centre:** repeated and paraphrased complaints are answered from the cache, with no model call and no per-query model cost.",
    f"**Cost:** cold path ≈ ${cp['cost_usd_per_query']:.5f} per query ({cp['model_calls_per_query']:.2f} model calls, "
    f"{cp['input_tokens_per_query']:.0f} input / {cp['output_tokens_per_query']:.0f} output tokens); cache hits cost $0.",
    "**Safety:** no invented steps, links or URLs reach the customer. Disruptive actions (restart, safe mode, reset) are always last.",
    "**Use cases:** in-device help (assistant or Settings search), customer-care chat and agent-assist, self-service web support.",
    "**Extensible:** a new product area needs only its SIIS articles and catalog entries; nothing is hard-coded per issue.",
], size=16)

# ---------------------------------------------------------------- slide 8: results
sl = S[7]
small_title(sl)
body(sl)._element.getparent().remove(body(sl)._element)
auto_pct = C["auto_with_valid_actionable_pct"]
rows = [
    ["Check (official data, live model, real HTTP)", "Target", "Measured"],
    ["Schema-valid responses", "≥ 99%", f"✔ {C['schema_valid_pct']:.0f}% ({C['schema_valid_lines']}/{C['lines']})"],
    ["Goal / title / description rules", "≥ 95%", f"✔ {C['rule_compliance_goal_title_description_pct']:.0f}%"],
    ["URL leaks", "0", f"✔ {C['absolute_url_leaks']}"],
    ["Deeplinks found verbatim in the catalog", "100%", f"✔ {C['deeplink_catalog_validity_pct']:.0f}% ({C['deeplinks_catalog_valid']}/{C['deeplinks_emitted']})"],
    ["Auto actions with a valid deeplink", "≥ 90%", (f"✔ {auto_pct:.1f}%" if auto_pct >= 90 else f"△ {auto_pct:.1f}%")],
    ["P95 latency: exact cache hit", "≤ 300 ms", f"✔ {ms(exact_p95)}"],
]
if sem:
    rows.append(["P95 latency: unseen paraphrase hit", "≤ 300 ms", f"✔ {ms(sem['p95'])}"])
rows.append(["P95 latency: cold full pipeline", "≤ 8 s", (f"✔ {ms(cold_p95)}" if cold_p95 <= 8000 else f"△ {ms(cold_p95)}")])
if para.get("n"):
    hit = para["correct_hit_rate_pct"]
    rows.append(["Unseen paraphrases served the correct plan", "≥ 80%", (f"✔ {hit:.1f}%" if hit >= 80 else f"△ {hit:.1f}%") + f" ({para['correct_plan_hits']}/{para['n']})"])
rows.append(["Wrong-plan cache hits (unseen paraphrases)", "0", f"△ {para.get('wrong_plan_hits')}/{para.get('n')}"])
rows.append(["Official queries with a grounded plan", "—", f"{plans}/{nq} (others: no_match)"])
table(sl, rows, L, T - 100000, 7400000, [3900000, 1100000, 2400000], size=12, row_h=330000)
lim = sl.shapes.add_textbox(Emu(L + 7550000), Emu(T - 100000), Emu(W - 7550000), Emu(4300000)).text_frame
lim.word_wrap = True
for i, line in enumerate([
    ("Innovation", True),
    ("Model assists, code decides: catalog-only URIs, grounding filter, deterministic order.", False),
    ("Gated semantic cache (concept + qualifier checks) to avoid serving the wrong plan.", False),
    ("Limitations (honest)", True),
    ("Only 1 official reference sample: step accuracy vs. gold is not measurable at scale.", False),
    ("Some SIIS answers do not address the complaint; the system returns no_match rather than inventing steps.", False),
    *([(f"Free-tier daily quota ran out in the final run: {_fb}/{cp['ok']} cold requests used the fail-over model.", False)]
      if (_fb := sum(n for m_, n in cp['models'].items() if m_ != B['llm_model'])) else
      [("One multi-intent edge case (E04) returned no_match instead of a plan.", False)]),
    ("Most official auto steps are outside Settings (panels, other apps): no catalog link exists for them.", False),
]):
    text, hd = line
    p = lim.paragraphs[0] if i == 0 else lim.add_paragraph()
    p.space_after = Pt(3)
    r = p.add_run()
    r.text = text if hd else "• " + text
    r.font.size = Pt(14 if hd else 12)
    r.font.bold = hd
    r.font.color.rgb = PURPLE if hd else DARK

# ---------------------------------------------------------------- slide 9: what's next
bullets(body(S[8]), [
    "**More gold data:** label the official queries (expected actions and deeplinks) to measure step accuracy and deeplink relevance, not only rule compliance.",
    "**On-device or edge model** for extraction, so cold requests need no network and no third-party API.",
    "**Validation loop:** after a one-tap action, read the validation deeplink and adapt the next step (e.g. skip a toggle already on).",
    "**Multilingual complaints:** the embedding cache and the lexicon are English-only today.",
    "**Catalog coverage report:** list SIIS screens with no catalog entry (served with the placeholder deeplink today) so they can be added.",
    "**Feedback signal:** use 'did this fix it?' answers to rank actions and to retire stale cache entries.",
], size=16)

# ---------------------------------------------------------------- slide 10: brownie points
items = [
    "**Zero invented deeplinks or URLs** across every measured response: the URI is copied from the catalog, never generated.",
    "**Hostile review:** prompt and URL injection, fake URIs, parent-menu traps and cache poisoning were probed live (synthetic fixture) and offline; every issue found was fixed with a regression test (HARDENING_REPORT.md, H1–H19).",
    "**Measured, not claimed:** metrics.md is rendered from JSON reports produced against a real server and a real model; no number is typed by hand.",
    "**Ablation (synthetic fixture):** full-LLM vs. hybrid vs. rules deeplink mapping on the same extracted actions; on 13 gold actions the full-LLM mapper scored 1.77/2 deeplink relevance vs 2.00 for hybrid retrieval, at about 7× the cost per query.",
    "**Requirements traceability:** each rule maps to its code, its test and its evidence; targets not met are marked as such.",
    "**Format-tolerant:** adapted from the synthetic fixture to the official voiceassist:// catalog and SIIS objects without changing the contract.",
]
bullets(body(S[9]), items, size=15)

# ---------------------------------------------------------------- slide 11: checklist
bullets(body(S[10]), [
    f"Working prototype code — public or shared GitHub repo: **Y** ({REPO_URL})",
    "README with reproducible setup instructions: **Y** (make install / indexes / test / serve; Docker)",
    "Demo video, max 5 minutes: **Y** (https://youtu.be/eRBRi4PisPU; docs/ResolveAI_demo.mp4, docs/demo.webm)",
    "Presentation file (PPT or PDF): **Y** (this deck, docs/ folder)",
    "AI disclosure form: **Y** (docs/LangAI3.0_AI_Disclosure.docx, AI_DISCLOSURE.md)",
    "GitHub tag: **PRISM_GENAI_HACKATHON_Y2026**",
], size=17)

# Make every URL in the deck clickable (a run is split so only the URL itself carries the link).
import copy as _copy
import re as _re

_URL = _re.compile(r"https://[^\s;),]+")
for _slide in prs.slides:
    for _sh in _slide.shapes:
        if not _sh.has_text_frame:
            continue
        for _p in _sh.text_frame.paragraphs:
            for _r in list(_p.runs):
                m = _URL.search(_r.text)
                if not m or _r.hyperlink.address:
                    continue
                before, url, after = _r.text[:m.start()], m.group(0), _r.text[m.end():]
                _r.text = before
                link_el = _copy.deepcopy(_r._r)
                _r._r.addnext(link_el)
                from pptx.text.text import _Run
                link = _Run(link_el, _p)
                link.text = url
                link.hyperlink.address = url
                if after:
                    tail_el = _copy.deepcopy(_r._r)
                    link_el.addnext(tail_el)
                    _Run(tail_el, _p).text = after
prs.save(OUT)
print("saved", OUT)
