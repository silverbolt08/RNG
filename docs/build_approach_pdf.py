"""Build docs/approach.pdf - the approach document presented to mentors.

Run:  python docs/build_approach_pdf.py
"""

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.graphics.shapes import Drawing, Rect, String, Line, Polygon
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether,
)

OUT = "docs/approach.pdf"

NAVY = colors.HexColor("#17365D")
BLUE = colors.HexColor("#2E74B5")
GREY = colors.HexColor("#595959")
LIGHT = colors.HexColor("#EDF2F8")
RULE = colors.HexColor("#BFD0E3")
GREEN = colors.HexColor("#2E6B4F")
MINT = colors.HexColor("#E8F3EC")
AMBER = colors.HexColor("#FBF3E4")

styles = getSampleStyleSheet()


def S(name, **kw):
    base = kw.pop("parent", styles["Normal"])
    return ParagraphStyle(name, parent=base, **kw)


BODY = S("body", fontName="Helvetica", fontSize=9.3, leading=13.4,
         textColor=colors.HexColor("#1A1A1A"), spaceAfter=6, alignment=TA_LEFT)
H1 = S("h1", fontName="Helvetica-Bold", fontSize=14.5, leading=18,
       textColor=NAVY, spaceBefore=14, spaceAfter=7)
H2 = S("h2", fontName="Helvetica-Bold", fontSize=10.6, leading=14,
       textColor=BLUE, spaceBefore=9, spaceAfter=4)
TITLE = S("title", fontName="Helvetica-Bold", fontSize=21, leading=25,
          textColor=NAVY, spaceAfter=3)
SUB = S("sub", fontName="Helvetica", fontSize=11.5, leading=15,
        textColor=GREY, spaceAfter=2)
META = S("meta", fontName="Helvetica", fontSize=8.6, leading=12, textColor=GREY)
CELL = S("cell", fontName="Helvetica", fontSize=8.3, leading=11.2)
CELLB = S("cellb", parent=CELL, fontName="Helvetica-Bold")
MONO = S("mono", fontName="Courier", fontSize=8.2, leading=11.4,
         textColor=colors.HexColor("#1A1A1A"))
CAP = S("cap", fontName="Helvetica-Oblique", fontSize=8.2, leading=11,
        textColor=GREY, spaceBefore=3, spaceAfter=8)
NOTE = S("note", fontName="Helvetica", fontSize=9.1, leading=13,
         textColor=colors.HexColor("#1A1A1A"))


def p(t, s=BODY):
    return Paragraph(t, s)


def bullets(items, style=BODY):
    out = []
    for it in items:
        out.append(Paragraph(
            '<bullet>&bull;</bullet>&nbsp;' + it,
            ParagraphStyle("b", parent=style, leftIndent=11, bulletIndent=1,
                           spaceAfter=3.5)))
    return out


def callout(title, text, bg=MINT, border=GREEN):
    inner = [p(f'<font color="#2E6B4F"><b>{title}</b></font>', NOTE),
             Spacer(1, 2.5), p(text, NOTE)]
    t = Table([[inner]], colWidths=[165 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("LINEBEFORE", (0, 0), (0, -1), 2.2, border),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    return t


def grid(header, rows, widths, align_left=True):
    data = [[Paragraph(h, CELLB) for h in header]]
    for r in rows:
        data.append([Paragraph(c, CELL) for c in r])
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, RULE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


# ---------------------------------------------------------------- diagram
def box(d, x, y, w, h, lines, fill=colors.white, stroke=BLUE, bold_first=True,
        size=7.6, lw=1.0):
    d.add(Rect(x, y, w, h, fillColor=fill, strokeColor=stroke, strokeWidth=lw))
    n = len(lines)
    total = n * (size + 2.6) - 2.6
    top = y + h / 2 + total / 2 - size
    for i, ln in enumerate(lines):
        f = "Helvetica-Bold" if (i == 0 and bold_first) else "Helvetica"
        d.add(String(x + w / 2, top - i * (size + 2.6), ln, fontName=f,
                     fontSize=size, fillColor=colors.HexColor("#1A1A1A"),
                     textAnchor="middle"))


def box_left(d, x, y, w, h, title, lines, fill=colors.white, stroke=BLUE):
    d.add(Rect(x, y, w, h, fillColor=fill, strokeColor=stroke, strokeWidth=1.0))
    d.add(String(x + 7, y + h - 12, title, fontName="Helvetica-Bold",
                 fontSize=7.8, fillColor=NAVY))
    for i, ln in enumerate(lines):
        d.add(String(x + 7, y + h - 24 - i * 10.2, ln, fontName="Helvetica",
                     fontSize=7.3, fillColor=colors.HexColor("#1A1A1A")))


def arrow(d, x1, y1, x2, y2, label=None, color=GREY):
    d.add(Line(x1, y1, x2, y2, strokeColor=color, strokeWidth=0.9))
    if y2 < y1:
        d.add(Polygon([x2 - 3.2, y2 + 5.5, x2 + 3.2, y2 + 5.5, x2, y2],
                      fillColor=color, strokeColor=color))
    else:
        d.add(Polygon([x2 - 3.2, y2 - 5.5, x2 + 3.2, y2 - 5.5, x2, y2],
                      fillColor=color, strokeColor=color))
    if label:
        d.add(String(x1 + 5, (y1 + y2) / 2 - 2.5, label, fontName="Helvetica",
                     fontSize=6.8, fillColor=GREY))


def architecture():
    d = Drawing(470, 322)

    box(d, 168, 290, 134, 24, ["Streamlit UI"], fill=LIGHT)
    arrow(d, 235, 290, 235, 268, "HTTP")

    d.add(Rect(22, 238, 426, 32, fillColor=NAVY, strokeColor=NAVY))
    d.add(String(235, 257, "FastAPI  +  Pydantic v2", fontName="Helvetica-Bold",
                 fontSize=8.4, fillColor=colors.white, textAnchor="middle"))
    d.add(String(235, 244, "/analyze    /ingest    /health    /trace/{run_id}",
                 fontName="Helvetica", fontSize=7.4,
                 fillColor=colors.HexColor("#C7D8EC"), textAnchor="middle"))

    # branches
    arrow(d, 110, 240, 110, 214)
    d.add(String(114, 224, "/ingest", fontName="Helvetica-Oblique",
                 fontSize=6.8, fillColor=GREY))
    arrow(d, 330, 240, 330, 214)
    d.add(String(334, 224, "/analyze", fontName="Helvetica-Oblique",
                 fontSize=6.8, fillColor=GREY))

    box_left(d, 22, 96, 176, 118, "INGEST PATH", [
        "1  loader  (Annex B schema)",
        "2  validation script",
        "3  clean: resample, gaps,",
        "      duplicates, resets",
        "4  bump dataset_id",
        "5  PRECOMPUTE bridge",
        "      -> analysis_cache",
    ], fill=colors.white)

    box_left(d, 236, 80, 212, 134, "LANGGRAPH  (/analyze)", [
        "1  classify            LLM call 1",
        "2  guard  (period? scope?)",
        "3  load_analysis  (cache hit)",
        "4  pipeline   deterministic",
        "5  write_brief       LLM call 2",
        "6  verify  numbers vs registry",
        "7  respond + persist trace",
    ], fill=colors.white)

    d.add(Rect(240, 101, 204, 13, fillColor=MINT, strokeColor=MINT))
    d.add(String(243, 105, "every number produced here, by code",
                 fontName="Helvetica-Oblique", fontSize=6.6, fillColor=GREEN))

    arrow(d, 110, 96, 110, 68)
    arrow(d, 342, 80, 342, 68)

    d.add(Rect(22, 36, 426, 32, fillColor=LIGHT, strokeColor=BLUE,
               strokeWidth=1.0))
    d.add(String(235, 55, "DuckDB  (single file)", fontName="Helvetica-Bold",
                 fontSize=8.2, fillColor=colors.HexColor("#1A1A1A"),
                 textAnchor="middle"))
    d.add(String(235, 42,
                 "raw tables  |  meter_clean  |  analysis_cache  |  runs  |  "
                 "run_steps  |  registry_values",
                 fontName="Helvetica", fontSize=6.9, fillColor=GREY,
                 textAnchor="middle"))

    d.add(Rect(22, 8, 426, 20, fillColor=AMBER, strokeColor=AMBER))
    d.add(String(235, 14,
                 "Two LLM calls per request, both at the edges. Everything "
                 "between them is deterministic.",
                 fontName="Helvetica-Bold", fontSize=7.6,
                 fillColor=colors.HexColor("#7A5B14"), textAnchor="middle"))
    return d


# ---------------------------------------------------------------- content
story = []

story.append(p("Team RNG", META))
story.append(Spacer(1, 1))
story.append(p("Factory Energy Cost Decomposition and Savings Agent", TITLE))
story.append(p("Our approach, for mentor review", SUB))
story.append(Spacer(1, 5))
tbl = Table([[Paragraph(
    "HCLTech Future Ready AI Engineer Hackathon &nbsp;&bull;&nbsp; "
    "IIIT Delhi &nbsp;&bull;&nbsp; 11 October 2026 &nbsp;&bull;&nbsp; "
    "github.com/silverbolt08/RNG", META)]], colWidths=[165 * mm])
tbl.setStyle(TableStyle([
    ("LINEABOVE", (0, 0), (-1, 0), 1.6, BLUE),
    ("TOPPADDING", (0, 0), (-1, -1), 5),
    ("LEFTPADDING", (0, 0), (-1, -1), 0),
]))
story.append(tbl)
story.append(Spacer(1, 11))

# 1 ------------------------------------------------------------------
story.append(p("1. What we are building", H1))
story.append(p(
    "A factory's electricity bill is 15 percent higher than last quarter. Our "
    "system attributes that rise to tariff, volume, product mix, weather and "
    "controllable waste, in that order, and only then recommends actions with "
    "costed savings, CO2e and payback. The five causes add up to the total by "
    "construction, not by adjustment."))
story.append(p(
    "The governing principle for every design choice below: <b>the language "
    "model classifies the request and writes the prose. It never computes a "
    "number and never decides what is wasteful.</b> Numbers come from code, "
    "and every number we print can be traced to the tool that produced it."))

# 2 ------------------------------------------------------------------
story.append(p("2. System architecture", H1))
story.append(architecture())
story.append(p(
    "Figure 1 &mdash; Request flow. The UI calls the API over HTTP rather than "
    "importing the modules, so our demo exercises the same contract the judges "
    "test.", CAP))
story.append(p(
    "Analysis is computed once when data lands and cached against the dataset "
    "version, so questions are answered from a warm cache. That keeps p50 and "
    "p95 latency low, and it makes the late data drop cheap to handle: a new "
    "ingest produces a new version, and \"what changed\" is the difference "
    "between two cached bridges, cause by cause."))

# 3 ------------------------------------------------------------------
story.append(p("3. Design decisions, and why", H1))
story.append(p(
    "The brief rewards the simplest architecture that meets the requirements. "
    "These are the choices we made and the reasoning we will defend."))
story.append(grid(
    ["Decision", "What we chose", "Why"],
    [["Request routing",
      "LLM classifies intent into structured JSON, then deterministic code runs "
      "a fixed pipeline per answer type. Two LLM calls per request.",
      "There are six known question types. An agentic tool-picking loop would "
      "add variable latency and an unbounded call count without improving "
      "accuracy on a closed set, and it makes traces harder to read."],
     ["Attribution method",
      "Shapley values over the four explainable causes; waste is the residual.",
      "Sequential attribution gives a different answer depending on the order "
      "you process causes in. Shapley averages over all orderings, so no cause "
      "is advantaged by its position."],
     ["Number safety",
      "Every tool result enters a number registry. The brief is validated "
      "against it after generation; on a mismatch we regenerate once, then "
      "fall back to a slot-filled template.",
      "Makes a hallucinated figure structurally unable to reach the response, "
      "while keeping the brief readable. The worst case is stilted prose, never "
      "a wrong number."],
     ["Attribution order",
      "Enforced by function signature, not by prompt wording.",
      "detect_leaks() cannot be called without the completed bridge, so nothing "
      "can be named as waste before tariff, volume, mix and weather have been "
      "removed."],
     ["Data store",
      "DuckDB, single file.",
      "Analytical queries over roughly 158,000 meter rows, reads Parquet "
      "natively, and the brief warns against loading time series into SQLite "
      "to satisfy the rule."],
     ["Compute timing",
      "Precompute on ingest, serve from cache.",
      "Latency is a reported metric, and judges ingest data and then "
      "immediately ask questions. We pay the cost once, where it is expected."],
     ["Agent count",
      "One classifier, one writer. No further agents.",
      "The decomposition, idle-load detection, savings and ranking are all "
      "deterministic. Additional agents would add failure modes without adding "
      "capability."],
     ["LLM",
      "Gemini 3.5 Flash Lite by default, selected by LLM_PROVIDER and "
      "LLM_MODEL; MOCK_LLM runs the full pipeline with no model at all.",
      "Only two calls per request, so a fast low-cost model is sufficient. The "
      "switch lets us move to Gemini 3.8 Flash if brief quality needs it, and "
      "MOCK_LLM keeps us working if the network does not."]],
    [26 * mm, 52 * mm, 87 * mm]))

# 4 ------------------------------------------------------------------
story.append(p("4. How we split the 15 percent", H1))
story.append(p(
    "The bill is roughly the sum of energy multiplied by price. Several drivers "
    "moved at once, and they interact, so the cross terms have to be assigned "
    "to someone. The assignment is not unique. Taking a simplified two-factor "
    "case where energy rose from 1,000 to 1,100 and price from 10.00 to 10.45, "
    "a total rise of 1,495:"))
story.append(grid(
    ["Order of attribution", "Price effect", "Volume effect", "Total"],
    [["Price measured first", "450", "1,045", "1,495"],
     ["Volume measured first", "495", "1,000", "1,495"]],
    [62 * mm, 33 * mm, 35 * mm, 35 * mm]))
story.append(Spacer(1, 6))
story.append(p(
    "Both add up. Both are defensible. The price effect differs by 45 purely "
    "because of processing order, and with four causes there are 24 orderings. "
    "We therefore use Shapley values, which average a cause's marginal "
    "contribution over every ordering and provably still sum to the total."))
story.append(p("Construction", H2))
story.append(p(
    "We define a counterfactual cost function C(S): the bill if the causes in "
    "set S sit at current-quarter levels and the rest stay at last quarter's. "
    "The baseline model from R2 evaluates it. With four causes that is sixteen "
    "evaluations, memoised.", BODY))
t = Table([[Paragraph(
    "tariff, volume, mix, weather&nbsp;&nbsp;=&nbsp;&nbsp;Shapley values over C"
    "&nbsp;&nbsp;&nbsp;(sums to C(all) - C(none))<br/>"
    "waste&nbsp;&nbsp;=&nbsp;&nbsp;actual_cost - C(all)"
    "&nbsp;&nbsp;&nbsp;(what the model cannot explain)<br/><br/>"
    "sum of all five&nbsp;&nbsp;=&nbsp;&nbsp;actual_Q3 - actual_Q2"
    "&nbsp;&nbsp;&nbsp;exactly, by construction", MONO)]],
    colWidths=[165 * mm])
t.setStyle(TableStyle([
    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F4F6F8")),
    ("BOX", (0, 0), (-1, -1), 0.5, RULE),
    ("LEFTPADDING", (0, 0), (-1, -1), 9),
    ("TOPPADDING", (0, 0), (-1, -1), 8),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
]))
story.append(t)
story.append(Spacer(1, 7))
story.append(p(
    "Additivity is therefore structural. The assertion in our code is a "
    "regression guard against floating-point drift and bugs, not a mechanism "
    "for forcing the numbers to reconcile."))
story.append(p("Where the ranges come from", H2))
story.append(p(
    "Annex A requires low and high values. We bootstrap: refit the baseline on "
    "50 resamples, recompute the Shapley split on each, and report the 10th and "
    "90th percentiles. Savings ranges propagate that interval together with our "
    "stated assumption ranges for the emission factor and leak persistence. No "
    "range is invented."))

# 5 ------------------------------------------------------------------
story.append(p("5. Two guardrails we build first", H1))
story.append(p("Nothing is blamed before the causes are removed", H2))
story.append(p(
    "Annex A requires tariff, volume, mix and weather effects to be removed "
    "before anything is called waste. We enforce this in the type system rather "
    "than in a prompt: the leak detector takes the completed bridge as a "
    "required argument, so it is impossible to call it earlier. The graph "
    "topology is the policy."))
story.append(p("Every number is traceable", H2))
story.append(p(
    "Each tool result is written to a registry row recording the value, its "
    "unit, the tool that produced it and its evidence reference. After the "
    "brief is generated we extract every numeral and check it against the "
    "registry with a rounding tolerance. Unmatched values trigger one "
    "regeneration, then the template fallback. The same registry rows populate "
    "evidence_refs in the response and appear in the trace, so one table serves "
    "the faithfulness check, the API contract and R12."))

# 6 ------------------------------------------------------------------
story.append(p("6. Data generation, and the circularity risk", H1))
story.append(p(
    "We generate our own data to the Annex B schema. That removes the problem "
    "of an unverifiable answer key and lets us measure decomposition error "
    "exactly, but it introduces a different risk, and we have designed against "
    "it deliberately."))
story.append(callout(
    "The risk we are designing against",
    "If the generator and the analyser are mirror images, a perfect result "
    "proves nothing. We therefore generate from explicit physical drivers "
    "&mdash; furnace from units multiplied by per-product energy intensity, "
    "HVAC from cooling degree-days, compressors from shift hours plus a leak "
    "beginning on a known date &mdash; while the analyser fits its baseline "
    "from the CSV files alone and never imports a generator constant. We also "
    "give the generator a piecewise temperature response that our linear "
    "baseline cannot represent, so a small residual error is expected and "
    "honest rather than suppressed."))
story.append(Spacer(1, 8))
story.append(p(
    "The generator is seeded and emits a ground_truth.json recording the true "
    "split and the location of every injected anomaly. We generate a second "
    "holdout set with a different seed and different patterns, and do not run "
    "against it until the evaluation pass."))
story.append(Spacer(1, 3))
story.append(grid(
    ["Edge case (Section 4.4)", "How we inject it", "How we detect it"],
    [["Sub-meters do not sum to the main meter",
      "Unmetered base load plus per-meter measurement noise",
      "Reconciliation check against a stated tolerance"],
     ["Missing intervals and meter resets",
      "Drop intervals in blocks; add a cumulative counter rollover",
      "Gap report; reset detected as a negative delta and corrected"],
     ["Tariff change mid-quarter",
      "Two tariff rows with a boundary inside the period",
      "Price joined per interval, never as a period average"],
     ["Mix shift raising energy per unit with no waste",
      "Shift product weights toward a more energy-intense family",
      "Mix term in the bridge absorbs it; waste stays flat"],
     ["Hot period raising cooling load",
      "Temperature excursion driving cooling degree-days",
      "Weather term in the bridge; HVAC excluded from leak search"],
     ["Public holiday logged as a normal shift",
      "is_holiday false on a day with near-zero production",
      "Shift calendar versus production cross-check in validation"]],
    [48 * mm, 59 * mm, 58 * mm]))
story.append(Spacer(1, 5))
story.append(p(
    "Each injected case doubles as an evaluation case, and the mix-shift trap "
    "is the one we will demo: it is the case where the correct answer blames "
    "product mix rather than a machine."))

# 7 ------------------------------------------------------------------
story.append(p("7. Requirements coverage", H1))
story.append(grid(
    ["ID", "How we meet it"],
    [["R1", "Loader normalises to the Annex B schema; cleaning resamples to 15 "
            "minutes, aligns the four tables on timestamp, repairs gaps, "
            "duplicates and meter resets. Validation script runs before any "
            "analysis."],
     ["R2", "Linear baseline predicting energy from volume, product mix and "
            "degree-days. R-squared reported in the trace."],
     ["R3", "Shapley decomposition over the four causes with waste as the "
            "residual. Additivity asserted in code."],
     ["R4", "Tariff joined per 15-minute interval against valid_from and "
            "valid_to, so a mid-period price change is handled exactly rather "
            "than averaged."],
     ["R5", "Per sub-meter consumption and share of total, ranked."],
     ["R6", "Idle load measured as consumption during zero-production "
            "intervals, reported by asset with the hours it occurs."],
     ["R7", "Leak detection from night and weekend baseline persistence, run "
            "only after the bridge is complete."],
     ["R8", "Savings in kWh, money and CO2e, each as a bootstrapped low-high "
            "range with the emission factor stated."],
     ["R9", "Actions ranked by payback period, then by ease."],
     ["R10", "Brief written by the LLM from the number registry only, with "
             "post-generation validation."],
     ["R11", "Waterfall bill-bridge chart showing the five causes summing to "
             "the total."],
     ["R12", "Every run persists to runs, run_steps and registry_values; "
             "/trace/{run_id} returns the Annex C shape."]],
    [11 * mm, 154 * mm]))

# 8 ------------------------------------------------------------------
story.append(p("8. How we will evaluate", H1))
story.append(p(
    "Because we generate the data, we know the true split, so decomposition "
    "error is measured rather than estimated. Our evaluation set is built from "
    "generator ground truth and exceeds the Section 7 minimums: cases where the "
    "correct answer blames price or mix rather than machines, numeric checks, "
    "out-of-scope questions, what-ifs, and deliberate attempts to make the model "
    "invent a figure."))
story.extend(bullets([
    "<b>Decomposition error</b> &mdash; mean absolute error per cause, in "
    "percentage points, against the injected truth.",
    "<b>Total reconciliation</b> &mdash; asserted on every run, reported as a "
    "pass rate.",
    "<b>Waste detection</b> &mdash; injected leak found; no waste claimed on "
    "the mix-shift and weather traps.",
    "<b>Savings accuracy</b> &mdash; share of cases where the true saving falls "
    "inside our reported range.",
    "<b>Number faithfulness</b> &mdash; share of briefs passing the registry "
    "check unaided, and the count of regenerations and template fallbacks.",
    "<b>Latency and cost</b> &mdash; p50 and p95, LLM calls and tokens per "
    "request.",
]))
story.append(Spacer(1, 3))
story.append(callout(
    "Comparing two configurations",
    "For the higher marks offered in Section 7 we will run both Shapley and "
    "sequential Laspeyres attribution across 50 generated scenarios and report "
    "mean absolute error per cause for each. The choice of method is then "
    "justified by numbers rather than by assertion. MOCK_LLM lets us run the "
    "whole set repeatedly at no cost.", bg=MINT, border=GREEN))

# 9 ------------------------------------------------------------------
story.append(p("9. What we have deliberately not built", H1))
story.append(grid(
    ["Not built", "Reason"],
    [["Vector store and retrieval",
      "There is no document corpus in this problem. The brief awards no marks "
      "for using one, and it would add a dependency and a failure mode for no "
      "capability."],
     ["Multi-agent orchestration",
      "Every analytical step is deterministic. A second reasoning agent would "
      "introduce non-determinism into a pipeline whose correctness we need to "
      "be able to assert."],
     ["Machine learning beyond a linear baseline",
      "A gradient-boosted model might fit better, but the baseline has to be "
      "explainable in the Q&amp;A and inverted for counterfactuals. We will "
      "report the R-squared we achieve and say where it limits us."],
     ["Cloud tracing tools",
      "Not permitted; tracing is local, in DuckDB."]],
    [42 * mm, 123 * mm]))

# 10 -----------------------------------------------------------------
story.append(p("10. Questions for our mentor", H1))
story.extend(bullets([
    "Since we generate our own data, will a late data drop still be supplied at "
    "13:45, or should we simulate our own corrected tariff table and "
    "sub-meter calibration fix?",
    "Is an emission factor supplied for CO2e, or do we choose and state our "
    "own?",
    "What reconciliation tolerance is expected between the sub-meter total and "
    "the main meter? We plan to state and justify our own.",
    "For live testing, will judges ingest CSVs in the Annex B schema only, or "
    "should the loader tolerate additional or reordered columns? We are "
    "building for the latter.",
]))

story.append(Spacer(1, 10))
f = Table([[Paragraph(
    "Prepared for the mentor check-in. Architecture, decomposition method and "
    "guardrails are settled; implementation is next. Repository: "
    "github.com/silverbolt08/RNG", META)]], colWidths=[165 * mm])
f.setStyle(TableStyle([
    ("LINEABOVE", (0, 0), (-1, 0), 0.8, RULE),
    ("TOPPADDING", (0, 0), (-1, -1), 6),
    ("LEFTPADDING", (0, 0), (-1, -1), 0),
]))
story.append(f)


def footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.6)
    canvas.setFillColor(GREY)
    canvas.drawString(22 * mm, 12 * mm,
                      "Team RNG  |  Factory Energy Cost Decomposition and "
                      "Savings Agent  |  Approach")
    canvas.drawRightString(A4[0] - 22 * mm, 12 * mm, "Page %d" % doc.page)
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.5)
    canvas.line(22 * mm, 15 * mm, A4[0] - 22 * mm, 15 * mm)
    canvas.restoreState()


doc = SimpleDocTemplate(
    OUT, pagesize=A4,
    leftMargin=22 * mm, rightMargin=22 * mm,
    topMargin=18 * mm, bottomMargin=20 * mm,
    title="Factory Energy Cost Decomposition and Savings Agent - Approach",
    author="Team RNG",
)
doc.build(story, onFirstPage=footer, onLaterPages=footer)
print("wrote", OUT)
