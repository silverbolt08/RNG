"""The two LLM calls, and their offline stand-ins.

The LLM does exactly two things: it classifies the request, and it writes the
prose. It never computes a number and never decides what is wasteful.

MOCK_LLM=true swaps the classifier for the keyword router below and the writer
for the slot-filled template in brief.py, so the whole system runs offline.
The same two stand-ins are the fallback when the provider fails or times out.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass

from rng.api.schemas import ComponentHealth
from rng.config import Settings

log = logging.getLogger(__name__)

INTENTS = ("bridge", "bill_total", "top_users", "idle_load", "savings",
           "ranking", "what_if", "out_of_scope", "refuse", "unclear")

_PERIOD = re.compile(r"\b(20\d{2})[-\s]?Q([1-4])\b", re.IGNORECASE)
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent|per cent)", re.IGNORECASE)

# Requests to break the one rule. Checked deterministically, before and
# regardless of the model: a prompt cannot talk its way past a regex.
_REFUSE = ("ignore your", "ignore the", "ignore previous", "ignore all",
           "make up", "invent", "pretend", "just guess", "fabricate",
           "without using", "don't use the data", "do not use the data",
           "skip the calculation", "your best guess", "any number will do",
           "system prompt", "say that the", "tell me that the", "assume the waste is",
           "round it up to", "real company", "competitor",
           # tampering with the data or the result is the same request
           "delete", "falsify", "tamper", "manipulate", "doctor the", "cover up",
           "hide the", "remove the reading", "drop the reading", "alter the",
           "change the data", "edit the data", "look lower", "looks lower",
           "look better", "looks better")
_OUT_OF_SCOPE = ("next year", "next quarter", "next month", "forecast", "predict",
                 "will the tariff", "will be", "in the future", "going to be",
                 "solar", "headcount", "salary", "revenue", "profit",
                 "stock price", "weather tomorrow",
                 # other utilities and fuels: this is the electricity bill only
                 "natural gas", "gas bill", " gas", "diesel", "fuel", "water",
                 "steam")
# What the four tables are about. A full sentence that mentions none of it is
# a question about something else; a fragment is merely unclear.
_ON_TOPIC = ("bill", "energy", "electric", "kwh", "power", "tariff", "price",
             "meter", "cost", "consum", "usage", "load", "saving", "waste",
             "units", "output", "temperature", "weather", "emission", "co2",
             "carbon", "peak", "asset", "machine")


@dataclass
class Intent:
    intent: str = "unclear"
    period: str | None = None
    compare_to: str | None = None
    asset: str | None = None
    tariff_scale: float | None = None   # what-if: 0.9 means 10% lower
    scenario: str | None = None         # what-if: "tariff" | "fix_waste"
    order: str | None = None            # ranking: "asc" = the least, not the most
    reason: str | None = None
    source: str = "keyword_router"

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


def _has(text: str, *needles: str) -> bool:
    return any(n in text for n in needles)


def find_periods(question: str) -> list[str]:
    return [f"{y}-Q{q}" for y, q in _PERIOD.findall(question)]


def find_asset(question: str, assets: list[str]) -> str | None:
    q = question.lower()
    for asset in sorted(assets, key=len, reverse=True):
        if asset.lower() == "main":
            continue
        forms = {asset.lower(), asset.lower().replace("_", " ")}
        forms |= {f.rstrip("s") for f in forms if len(f) > 4}
        if any(f in q for f in forms):
            return asset
    return None


def must_refuse(question: str) -> bool:
    return _has(question.lower(), *_REFUSE)


def route_keywords(question: str, assets: list[str]) -> Intent:
    """The offline classifier: ordered keyword rules, most specific first."""
    q = question.lower().strip()
    periods = find_periods(question)
    out = Intent(asset=find_asset(question, assets))
    if _has(q, "least", "lowest", "smallest", "fewest", "minimum", "bottom"):
        out.order = "asc"
    if periods:
        out.period = periods[0]
    if len(periods) > 1:
        out.period, out.compare_to = sorted(periods)[-1], sorted(periods)[0]

    if must_refuse(question):
        out.intent, out.reason = "refuse", "it asks for a figure not computed from the data"
    elif _has(q, "what if", "what-if", "had been", "suppose", "if we fix",
              "if the tariff", "if we repair", "would the bill"):
        out.intent = "what_if"
        pct = _PERCENT.search(q)
        if _has(q, "tariff", "price", "rate") and pct:
            sign = 1.0 if _has(q, "higher", "more expensive", "increase", "rise",
                               "rose", "up by") else -1.0
            out.scenario = "tariff"
            out.tariff_scale = 1.0 + sign * float(pct.group(1)) / 100.0
        elif _has(q, "leak", "fix", "repair", "waste"):
            out.scenario = "fix_waste"
        else:
            out.intent = "out_of_scope"
            out.reason = ("only two what-ifs are supported: a tariff change by "
                          "a stated percentage, and fixing the identified waste")
    elif _has(q, *_OUT_OF_SCOPE):
        out.intent, out.reason = "out_of_scope", "it asks about something outside the loaded tables"
    elif _has(q, "idle", "nothing is produced", "nothing is being", "nothing being",
              "nothing was", "no production", "not producing",
              "not running", "standby", "at night", "weekend", "base load",
              "baseload", "when the plant is"):
        out.intent = "idle_load"
    elif _has(q, "pays back", "payback", "pay back", "fastest", "quickest",
              "rank the action", "easiest"):
        out.intent = "ranking"
    elif _has(q, "what should we do", "recommend", "saving", "save ", "action",
              "do first", "reduce the bill", "cut the bill", "fix"):
        out.intent = "savings"
    # A ranking word alone is not a question about assets: "the biggest driver
    # of the increase" asks about causes. It needs something to rank.
    elif (_has(q, "most energy", "top user", "which asset", "which machine",
               "which meter", "by asset", "by meter", "breakdown by",
               "use the most", "uses the most", "consume", "share of")
          or (_has(q, "biggest", "largest", "highest", "top ", "most", "least",
                   "lowest", "smallest")
              and _has(q, "asset", "machine", "meter", "equipment", "user",
                       "consumer"))):
        out.intent = "top_users"
    elif _has(q, "why", "go up", "went up", "gone up", "increase", "rise", "rose",
              "higher", "decompos", "split", "bridge", "cause", "driver",
              "tariff", "mix", "volume", "weather", "waste", "blame", "explain",
              "leak", "fault", "machine", "wasted", "change", "percentage",
              "efficien", "per unit", "product", "produc", "make more",
              "made more", "price", "hotter", "temperature", "due to"):
        out.intent = "bridge"
    elif _has(q, "how much", "total bill", "total cost", "spend", "spent",
              "what was the bill", "bill for"):
        out.intent = "bill_total"
    elif out.asset:
        out.intent = "top_users"
    elif len(q.split()) >= 4 and not _has(q, *_ON_TOPIC):
        out.intent = "out_of_scope"
        out.reason = ("it is not about the electricity bill, and the data holds "
                      "only meter readings, tariff, production and weather")
    else:
        out.reason = "the request does not say what to analyse"
    return out


_CLASSIFY_PROMPT = """You route questions about a factory's electricity bill.
Return ONLY a JSON object with these keys:
  intent: one of {intents}
  period, compare_to: period labels like "2026-Q3" if the question names them, else null
  asset: one of the listed meters if the question names one, else null
  scenario: for what_if only, "tariff" or "fix_waste", else null
  tariff_change_pct: for a tariff what_if, the signed percentage change (-10 for 10% lower), else null
  order: "asc" if the question asks for the least, lowest or smallest, else "desc"

Intents:
  bridge        why the bill changed; split into tariff, volume, mix, weather, waste
  bill_total    how much the bill was
  top_users     which assets use the most energy
  idle_load     energy used when nothing is produced
  savings       what to do, which actions save money
  ranking       which action pays back fastest
  what_if       a tariff change by a percentage, or fixing the identified waste
  out_of_scope  a clear request whose answer is NOT in the meter, tariff, production
                or weather tables: forecasts, other utilities or fuels (gas, water),
                staffing, finance, general knowledge, or any task unrelated to the
                electricity bill
  refuse        asks to invent, guess or assume a number, or to alter, delete,
                hide or falsify data or results
  unclear       is about the electricity bill but too vague to tell which analysis
                is wanted, or is not a request at all (a greeting)

Periods in the data: {periods}
Meters in the data: {assets}
Question: {question}"""

_WRITE_PROMPT = """You write a short brief for a finance manager.

Question: {question}

FACTS (the only numbers you may use; copy them exactly as written):
{facts}

Notes from the analysis:
{notes}

Rules:
- 3 to 5 plain sentences. No headings, no bullet points, no markdown.
- Use ONLY numbers that appear in FACTS. Never calculate, add, subtract,
  average, convert or re-round a number. Never write a number that is not there.
- Do not blame a machine unless the notes name one.
{feedback}"""


class LLM:
    """Provider switch: LLM_PROVIDER / LLM_MODEL. Gemini is implemented."""

    def __init__(self, settings: Settings):
        self.provider = settings.llm_provider.lower()
        self.model = settings.llm_model
        self._key = settings.gemini_api_key
        self.mock = settings.mock_llm
        self._client = None

    # -- provider ----------------------------------------------------------

    def _generate(self, prompt: str, as_json: bool) -> tuple[str, int]:
        if self.provider != "gemini":
            raise RuntimeError(f"LLM_PROVIDER={self.provider!r} is not implemented")
        if not self._key:
            raise RuntimeError("GEMINI_API_KEY is not set")
        from google import genai

        if self._client is None:
            try:
                self._client = genai.Client(
                    api_key=self._key, http_options={"timeout": 20000})
            except Exception:
                self._client = genai.Client(api_key=self._key)
        config = {"temperature": 0}
        if as_json:
            config["response_mime_type"] = "application/json"
        response = self._client.models.generate_content(
            model=self.model, contents=prompt, config=config)
        usage = getattr(response, "usage_metadata", None)
        tokens = int(getattr(usage, "total_token_count", 0) or 0)
        return (response.text or "").strip(), tokens

    def health(self) -> ComponentHealth:
        if self.mock:
            return ComponentHealth(status="ok", detail="MOCK_LLM: offline router and template")
        if self.provider != "gemini":
            return ComponentHealth(status="down", detail=f"provider {self.provider!r} not implemented")
        if not self._key:
            return ComponentHealth(status="degraded", detail="no API key; using offline fallback")
        return ComponentHealth(status="ok", detail=f"{self.provider}:{self.model}")

    # -- call 1: classify --------------------------------------------------

    def classify(self, question: str, periods: list[str],
                 assets: list[str]) -> tuple[Intent, dict]:
        """Returns (intent, usage). usage = {"calls": n, "tokens": n}."""
        fallback = route_keywords(question, assets)
        if self.mock or fallback.intent == "refuse":
            return fallback, {"calls": 0, "tokens": 0}
        try:
            text, tokens = self._generate(_CLASSIFY_PROMPT.format(
                intents=", ".join(INTENTS), periods=", ".join(periods) or "none",
                assets=", ".join(assets) or "none", question=question), as_json=True)
            data = json.loads(text)
            intent = str(data.get("intent", "")).strip()
            if intent not in INTENTS:
                raise ValueError(f"unknown intent {intent!r}")
            out = Intent(intent=intent, source=f"{self.provider}:{self.model}")
            out.period = data.get("period") or fallback.period
            out.compare_to = data.get("compare_to") or fallback.compare_to
            out.asset = data.get("asset") if data.get("asset") in assets else fallback.asset
            if intent == "what_if":
                out.scenario = data.get("scenario") or fallback.scenario
                pct = data.get("tariff_change_pct")
                out.tariff_scale = (1.0 + float(pct) / 100.0 if pct is not None
                                    else fallback.tariff_scale)
            # The direction of a ranking is too easy to drop: either source saying
            # "least" is enough.
            if "asc" in (fallback.order, data.get("order")):
                out.order = "asc"
            out.reason = fallback.reason
            return out, {"calls": 1, "tokens": tokens}
        except Exception as exc:  # network, quota, bad JSON: route offline
            log.warning("classifier fell back to keyword router: %s", exc)
            fallback.source = "keyword_router (fallback)"
            return fallback, {"calls": 1, "tokens": 0}

    # -- call 2: write -----------------------------------------------------

    def write(self, question: str, facts: str, notes: str,
              rejected: list[str] | None = None) -> tuple[str | None, dict]:
        """Returns (prose or None, usage). None means: use the template."""
        if self.mock:
            return None, {"calls": 0, "tokens": 0}
        feedback = ""
        if rejected:
            feedback = ("- Your previous draft used these numbers, which are NOT "
                        f"in FACTS: {', '.join(rejected)}. Remove them.")
        try:
            text, tokens = self._generate(_WRITE_PROMPT.format(
                question=question, facts=facts, notes=notes or "none",
                feedback=feedback), as_json=False)
            return (text or None), {"calls": 1, "tokens": tokens}
        except Exception as exc:
            log.warning("writer fell back to template: %s", exc)
            return None, {"calls": 1, "tokens": 0}
