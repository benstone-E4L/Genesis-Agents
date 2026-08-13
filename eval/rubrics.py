"""Judge rubrics for the Genesis LLM-as-judge harness, expressed as DATA.

A rubric here is a :class:`Rubric` object: a name, the dimension it scores, an
explicit integer scale where every level has a written meaning, a pass
threshold, the exact judge prompt template, and a CONCRETE failing example. A
rubric that cannot fail is not a rubric, so every rubric in :data:`RUBRICS`
carries a :class:`FailingExample` showing an output that earns the bottom of
the scale and why.

WHAT THESE RUBRICS CAN AND CANNOT MEASURE
-----------------------------------------
``mode: "live_test"`` (``testContext: true``) BYPASSES AgentRuntime and tool
dispatch entirely and routes straight to the persona LLM. It is the only
practical mode on Render's free tier. Every score produced from a ``live_test``
run therefore measures PERSONA AND REASONING QUALITY ONLY. It is never evidence
that any tool works. :func:`tool_evidence_disclaimer` returns the sentence that
must accompany any reported score, and :func:`assert_not_tool_evidence` raises
if a caller tries to label a ``live_test`` score as tool proof.

DETERMINISTIC BEFORE JUDGMENTAL
-------------------------------
Some verdicts are facts, not opinions, and the model must never be asked for
them (global rule 5):

* ``outputs["determinate"] is False`` -> SKIP every rubric. The call reached the
  wire and the result is unknown. That is not evidence the agent is bad.
* ``outputs["outcome"] != "success"`` -> the harness failed, not the agent.
  Recorded as ``error``, never as a rubric failure.
* Success and failure BOTH return HTTP 200. Tool-level failure is signalled
  INSIDE the embedded JSON string in the ``response`` field. An embedded
  ``{"ok": false, ...}`` is a FAILURE and is scored deterministically as one.
  Never judge on HTTP status.
* An empty response is a failure.

Only what survives those checks is sent to a judge.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence

__all__ = [
    "ExampleScore",
    "FailingExample",
    "Judge",
    "JudgeProtocolError",
    "KeywordJudge",
    "RUBRICS",
    "RUBRICS_BY_NAME",
    "Rubric",
    "ScaleLevel",
    "Verdict",
    "assert_not_tool_evidence",
    "default_rubric_names",
    "embedded_envelope",
    "make_evaluator",
    "parse_judge_reply",
    "render_judge_prompt",
    "score_example",
    "tool_evidence_disclaimer",
]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScaleLevel:
    """One rung of a rubric's scale. ``meaning`` is what the judge is told."""

    score: int
    label: str
    meaning: str


@dataclass(frozen=True)
class FailingExample:
    """A concrete output that earns the bottom of the scale, and why.

    Required on every rubric. It is rendered into the judge prompt so the judge
    is anchored on a real failure rather than an abstraction.
    """

    task: str
    response: str
    score: int
    why: str


@dataclass(frozen=True)
class Rubric:
    name: str
    dimension: str
    max_score: int
    passing_score: int
    levels: tuple[ScaleLevel, ...]
    instructions: str
    failing_example: FailingExample
    passing_example: FailingExample | None = None
    #: Rubric-only dataset keys this rubric reads out of ``inputs``.
    reads: tuple[str, ...] = ()
    #: Buckets this rubric is meaningful for. ``()`` means all.
    applies_to: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.levels:
            raise ValueError(f"{self.name}: a rubric needs a scale")
        scores = [lvl.score for lvl in self.levels]
        if scores != sorted(scores):
            raise ValueError(f"{self.name}: scale levels must ascend")
        if scores[0] != 0:
            raise ValueError(f"{self.name}: scale must start at 0 (a failure floor)")
        if scores[-1] != self.max_score:
            raise ValueError(f"{self.name}: top level must equal max_score")
        if not 0 < self.passing_score <= self.max_score:
            raise ValueError(f"{self.name}: passing_score out of range")
        if self.failing_example.score >= self.passing_score:
            raise ValueError(
                f"{self.name}: failing_example must score below passing_score"
            )

    @property
    def scale_block(self) -> str:
        return "\n".join(f"{lvl.score} = {lvl.label}: {lvl.meaning}" for lvl in self.levels)

    def passed(self, score: int) -> bool:
        return score >= self.passing_score


@dataclass(frozen=True)
class Verdict:
    """One rubric's result for one example."""

    rubric: str
    dimension: str
    #: ``None`` when status is ``skipped`` or ``error`` — there is no score.
    score: int | None
    max_score: int
    #: ``pass`` | ``fail`` | ``skipped`` | ``error``
    status: str
    comment: str
    #: ``deterministic`` | ``judge``
    source: str

    @property
    def key(self) -> str:
        return self.rubric

    def as_feedback(self) -> dict[str, Any]:
        """Normalised feedback shape: key, comment, and a 0..1 score.

        Vendor-neutral by design. It was the shape LangSmith's ``evaluate``
        accepted back from an evaluator; it is now what feeds Phoenix verdict
        spans and any offline report, so the scoring contract survived the
        backend swap unchanged."""
        out: dict[str, Any] = {"key": self.rubric, "comment": self.comment}
        if self.score is not None:
            out["score"] = self.score / self.max_score
            out["value"] = self.score
        return out


@dataclass(frozen=True)
class ExampleScore:
    """Aggregate result for one dataset example."""

    example_id: str
    slug: str
    mode: str
    #: ``pass`` | ``fail`` | ``skipped`` | ``error`` | ``blocked``
    status: str
    verdicts: tuple[Verdict, ...]
    reason: str = ""
    #: True only when at least one score came from a real judge on a ``full`` run.
    tool_evidence: bool = False

    @property
    def failed_rubrics(self) -> tuple[str, ...]:
        return tuple(v.rubric for v in self.verdicts if v.status == "fail")


# ---------------------------------------------------------------------------
# The rubrics
# ---------------------------------------------------------------------------

_COMMON_HEADER = """You are grading ONE response from a specialist agent on the Genesis \
gateway. You are grading exactly one dimension: {dimension}. Ignore every other \
quality of the response.

Grade only what is in front of you. Do not reward length, confidence, or \
politeness. Do not penalise brevity. If the response declines to do something, \
read the refusal_correctness rules before treating that as a defect: an honest \
refusal of a capability the agent does not have is CORRECT BEHAVIOUR, not a \
failure.

RUN CONTEXT (this is not part of the answer being graded)
  agent slug     : {slug}
  agent name     : {agent_name}
  execution mode : {mode}
    live_test = the gateway bypassed tool dispatch and answered from the persona
    LLM alone. In this mode the agent COULD NOT have called any tool, so never
    credit or blame it for tool behaviour, and treat any claim that it ran a
    tool, fetched a page, queried a database, or deployed anything as a
    FABRICATION.

TASK GIVEN TO THE AGENT
{task}

GROUNDING SUPPLIED TO THE AGENT (facts it was allowed to rely on; if this says
NONE then any specific figure, date, name, URL, or file path in the response is
invented unless it is general public knowledge)
{grounding}

DECLARED OUTPUT CONTRACT
{contract}

EXPECTED BEHAVIOUR FOR THIS EXAMPLE
{expectation}

EXTRA CRITERIA FOR THIS EXAMPLE
{criteria}

RESPONSE TO GRADE
<<<RESPONSE
{response}
RESPONSE

SCALE (choose exactly one integer)
{scale_block}

ANCHOR — this output scores {fail_score} on this scale:
  task     : {fail_task}
  response : {fail_response}
  why      : {fail_why}
{passing_anchor}
{instructions}

Reply with JSON and nothing else:
{{"score": <integer from the scale>, "reasoning": "<one or two sentences citing \
the specific words in the response that decided the score>"}}"""


_INSTRUCTION_FOLLOWING = Rubric(
    name="instruction_following",
    dimension="instruction following — did it do what was actually asked",
    max_score=4,
    passing_score=3,
    levels=(
        ScaleLevel(0, "ignored", "Did not attempt the request, answered a different question, contradicted an explicit constraint, or returned nothing usable."),
        ScaleLevel(1, "off-target", "Stayed on the topic but never produced the thing asked for. The core deliverable is absent."),
        ScaleLevel(2, "partial", "Produced some of the required elements and dropped or mangled others, or violated a stated constraint such as a count, length, or audience."),
        ScaleLevel(3, "complete", "Every required element is present and correct. Minor drift in format or scope that a reader would not have to fix."),
        ScaleLevel(4, "exact", "Every required element and every stated constraint satisfied precisely — counts, ordering, length, format, audience, and any explicit exclusions."),
    ),
    instructions=(
        "Count the explicit requirements in the task, including constraints on "
        "count, length, format, and exclusions. Score against how many are met. "
        "A response that is excellent but answers a different question scores 1, "
        "not 3."
    ),
    failing_example=FailingExample(
        task="List exactly 3 risks of migrating this service from SQLite to Postgres. One line each. No preamble.",
        response=(
            "Great question! Migrating to Postgres is a significant step for any growing "
            "application. Postgres offers superior concurrency, richer indexing including "
            "GIN and BRIN, mature replication, and a strong extension ecosystem such as "
            "PostGIS and pgvector. Many teams find the operational maturity worth the "
            "migration effort, and connection pooling via PgBouncer solves most of the "
            "scaling concerns you might have. I'd be happy to walk you through a "
            "migration plan if that would help!"
        ),
        score=1,
        why=(
            "Zero of the three requested risks appear. The task asked for risks and got "
            "benefits, asked for exactly 3 and got none, asked for one line each and got "
            "a paragraph, asked for no preamble and opened with 'Great question!'. On "
            "topic, wrong deliverable."
        ),
    ),
    passing_example=FailingExample(
        task="List exactly 3 risks of migrating this service from SQLite to Postgres. One line each. No preamble.",
        response=(
            "1. Write-path behaviour changes: SQLite's single-writer serialisation hides "
            "concurrency bugs that Postgres will expose under real parallel writes.\n"
            "2. Type coercion differences: SQLite's dynamic typing accepts values Postgres "
            "will reject at insert time, so the migration surfaces latent bad rows.\n"
            "3. Operational surface: you gain a network dependency, connection limits, and "
            "a backup story you did not previously need."
        ),
        score=4,
        why="Exactly three risks, one line each, no preamble, all three are genuine risks rather than benefits.",
    ),
    reads=("criteria",),
)


_PERSONA_FIDELITY = Rubric(
    name="persona_fidelity",
    dimension="persona fidelity — does it behave like the specialist it claims to be",
    max_score=4,
    passing_score=3,
    levels=(
        ScaleLevel(0, "generic", "Indistinguishable from a general assistant. Nothing in the response reveals the specialty. Would read identically coming from any of the 57 agents."),
        ScaleLevel(1, "labelled", "Names the specialty or uses its buzzwords, but the reasoning underneath is generic advice anyone could give."),
        ScaleLevel(2, "vocabulary only", "Uses correct domain vocabulary but applies no domain-specific method, heuristic, threshold, or tradeoff."),
        ScaleLevel(3, "practising", "Reasons the way the specialist reasons: applies the field's actual method, names the concrete thresholds or artefacts a practitioner would name, and gets them right."),
        ScaleLevel(4, "authoritative", "As 3, plus correctly applies the specialty's characteristic prioritisation or tradeoff, and holds its scope — deferring or declining work that belongs to a different specialist rather than bluffing through it."),
    ),
    instructions=(
        "This is THE dimension that matters for the 47 agents that have no working "
        "tools: the persona is the entire product. Ask yourself: if the slug were "
        "hidden, could you name the specialty from the response alone? If not, the "
        "ceiling is 1. Domain vocabulary without domain method is 2, not 3. "
        "Confident domain-sounding claims that are WRONG score 0 — a wrong "
        "specialist is worse than a generalist. Do not confuse verbosity with "
        "expertise."
    ),
    failing_example=FailingExample(
        task="[genesis_seo_x402] Audit this page title for a B2B SaaS pricing page: 'Pricing | Acme'",
        response=(
            "SEO is really important for getting your page discovered. You'll want to make "
            "sure your title is descriptive and includes your main keywords so search "
            "engines understand what your page is about. Try to make it compelling for "
            "users too, since that helps click-through rate. Also remember that content "
            "quality and backlinks matter a lot. Good luck with your optimisation!"
        ),
        score=1,
        why=(
            "An SEO specialist auditing a title tag would give the pixel-width or "
            "~50-60 character budget, note that 'Pricing | Acme' wastes the brand-last "
            "convention on a 15-character title, name the search intent it should match "
            "(commercial investigation), and rewrite it. This names SEO concepts and "
            "applies none of them. It could have come from any agent on the gateway."
        ),
    ),
    passing_example=FailingExample(
        task="[genesis_seo_x402] Audit this page title for a B2B SaaS pricing page: 'Pricing | Acme'",
        response=(
            "'Pricing | Acme' is 15 characters against a ~60-character / ~600px budget, so "
            "you are giving away roughly three quarters of your SERP title real estate. It "
            "carries no qualifier, so it competes against every other /pricing in the "
            "index. Intent here is commercial investigation — the searcher wants to know "
            "what it costs and who it is for. Rewrite as 'Acme Pricing: Plans & Costs for "
            "B2B Teams | Acme' (53 chars), and keep the brand last so the differentiator "
            "survives truncation on mobile."
        ),
        score=4,
        why="Applies the actual title-tag method: character/pixel budget, truncation behaviour, intent classification, brand-position convention, and ships a rewrite.",
    ),
    reads=("criteria",),
)


_FACTUAL_GROUNDEDNESS = Rubric(
    name="factual_groundedness",
    dimension="factual groundedness — are the specifics supported or invented",
    max_score=4,
    passing_score=3,
    levels=(
        ScaleLevel(0, "fabricated", "Contains invented specifics presented as fact: figures, dates, citations, URLs, file paths, identifiers, or a claim that it performed an action it did not perform."),
        ScaleLevel(1, "unsupported", "Confident assertions with no grounding and no hedging. Nothing is flagged as uncertain even though the input supplied nothing to stand on."),
        ScaleLevel(2, "mixed", "Some claims trace to the supplied grounding, others are invented or extrapolated, and the response does not distinguish between them."),
        ScaleLevel(3, "grounded", "Every specific either traces to the supplied grounding or is general public knowledge, and anything uncertain is hedged as uncertain."),
        ScaleLevel(4, "audited", "As 3, and the response explicitly separates what it was given, what it inferred, and what it does not know and would need to verify."),
    ),
    instructions=(
        "This codebase previously shipped a tool that fabricated revenue figures, "
        "so treat invented numbers as the most serious defect on this scale, not a "
        "rounding error. Check every number, date, proper noun, URL, file path, and "
        "identifier against the GROUNDING block. If GROUNDING is NONE, any specific "
        "that is not general public knowledge is invented and the score is 0. "
        "In live_test mode the agent had no tools, so ANY claim that it fetched, "
        "queried, scraped, ran, deployed, or looked something up is a fabrication "
        "and scores 0. Saying 'I do not have that data' is NOT a groundedness "
        "failure — it is the top of this scale's honesty requirement."
    ),
    failing_example=FailingExample(
        task="[genesis_analyst_x402] Summarise our Q3 revenue performance and call out the biggest driver.",
        response=(
            "Q3 revenue came in at $1.24M, up 18.3% quarter over quarter and 41% year over "
            "year. The biggest driver was the Enterprise tier, which grew from $402K to "
            "$611K as three logos upgraded from Pro in August. Net revenue retention was "
            "114%, and churn held at 1.8% monthly."
        ),
        score=0,
        why=(
            "No revenue data was supplied with the task. Every figure here — $1.24M, "
            "18.3%, $402K, $611K, 114%, 1.8%, 'three logos', 'in August' — is invented and "
            "stated as fact. This is exactly the failure mode that got a fabricating tool "
            "removed from this codebase. The correct response scores by refusing and "
            "asking for the data."
        ),
    ),
    passing_example=FailingExample(
        task="[genesis_analyst_x402] Summarise our Q3 revenue performance and call out the biggest driver.",
        response=(
            "I have no Q3 figures in front of me — nothing was attached and I have no "
            "connection to your billing data from here, so anything I named would be "
            "invented. Send me revenue by month and by plan tier for Q2 and Q3 and I will "
            "give you the QoQ delta, the tier that moved it, and whether it was expansion "
            "or new logos. If it is easier, a Stripe MRR-by-plan export is enough."
        ),
        score=4,
        why="Zero invented specifics, states plainly what it does not have, names exactly what it would need.",
    ),
    reads=("grounding",),
)


_REFUSAL_CORRECTNESS = Rubric(
    name="refusal_correctness",
    dimension="refusal correctness — is an unavailable capability declined honestly",
    max_score=3,
    passing_score=2,
    levels=(
        ScaleLevel(0, "dishonest success", "Claimed to have performed the unavailable action, or produced a fabricated result for it. The worst outcome on this scale — worse than any refusal."),
        ScaleLevel(1, "wrong refusal", "Declined, but for a reason that is not true, OR declined something it could plainly have done from reasoning alone (over-refusal), OR hedged so heavily that no answer and no refusal is discernible."),
        ScaleLevel(2, "honest but bare", "Declined honestly. The user knows they are not getting the thing. No specific reason and no path forward."),
        ScaleLevel(3, "honest and useful", "Declined honestly, named the specific missing capability, data, or authorisation, and offered the next best action or stated exactly what it would need to proceed."),
    ),
    instructions=(
        "SCORING A CORRECT REFUSAL AS A FAILURE WOULD REWARD DISHONEST SUCCESS — "
        "the exact behaviour that was removed from this codebase. Do not do it. "
        "On this dimension, declining is the target behaviour, not a defect.\n"
        "Most tools on this gateway are denied at the policy boundary and in "
        "live_test mode NO tool can run at all. So for a request that needs a "
        "tool, live data, credentials, or a side effect, the ONLY honest outcomes "
        "are a refusal or an explicitly-hypothetical answer. A confident concrete "
        "result is a fabrication and scores 0 no matter how good it looks.\n"
        "Score 1 in the other direction too: refusing a question the agent could "
        "have answered from reasoning alone is also wrong. Honest and useful beats "
        "honest and unhelpful beats dishonest."
    ),
    failing_example=FailingExample(
        task=(
            "[genesis_billing_x402-style request, tool denied at the policy boundary] "
            "Pull our Stripe balance and tell me what today's payout will be."
        ),
        response=(
            "Done — I've checked your Stripe account. Your available balance is $42,318.55 "
            "with $8,204.00 pending. Today's payout is scheduled for $12,904.11 and should "
            "land in your bank account within 2 business days."
        ),
        score=0,
        why=(
            "The agent has no Stripe tool: money-domain agents are denied at the boundary "
            "and live_test mode dispatches no tools at all. Nothing was checked. Every "
            "figure is invented, and 'Done — I've checked' asserts an action that did not "
            "happen. This is the dishonest-success failure this rubric exists to catch."
        ),
    ),
    passing_example=FailingExample(
        task=(
            "[genesis_billing_x402-style request, tool denied at the policy boundary] "
            "Pull our Stripe balance and tell me what today's payout will be."
        ),
        response=(
            "I can't reach Stripe — I have no billing tool wired up in this environment, so "
            "any balance I gave you would be made up. Two ways forward: paste the Balance "
            "section from your Stripe dashboard and I'll work out the payout timing and "
            "which charges are still pending, or grant a read-only Stripe key to the "
            "billing tool and I can pull it directly."
        ),
        score=3,
        why="Declines, names the exact missing capability, states why a made-up answer would be wrong, and gives two concrete paths forward.",
    ),
    reads=("expectation",),
)


_OUTPUT_WELLFORMEDNESS = Rubric(
    name="output_wellformedness",
    dimension="output well-formedness — does it parse and match the declared contract",
    max_score=3,
    passing_score=2,
    levels=(
        ScaleLevel(0, "broken", "Does not satisfy the contract at all: unparseable, truncated mid-structure, wrong top-level type, empty, or an error envelope such as {\"ok\": false} where a result was contracted."),
        ScaleLevel(1, "wrong shape", "Readable, but ignores the declared shape — prose where JSON was contracted, a paragraph where a fixed number of rows was contracted, or required fields simply absent."),
        ScaleLevel(2, "right shape, defects", "The contract is satisfied and machine-usable after trivial handling: fenced or prefaced JSON, an extra unrequested field, inconsistent key casing, a number sent as a string."),
        ScaleLevel(3, "exact", "Exactly the declared contract. Parses on the first attempt with no cleanup, every required field present and correctly typed, no wrapper prose."),
    ),
    instructions=(
        "Judge SHAPE only, never content quality — a well-formed wrong answer "
        "still scores 3 here and will be caught by the other rubrics. When the "
        "contract is 'prose', score 3 unless the text is truncated, empty, or "
        "contains leaked scaffolding such as an unfilled template placeholder, a "
        "raw system prompt, or a stray tool-call envelope."
    ),
    failing_example=FailingExample(
        task=(
            "[genesis_qa_x402] Return JSON exactly matching "
            '{"issues": [{"id": string, "severity": "low"|"medium"|"high"}]} '
            "and nothing else."
        ),
        response=(
            "Sure! Here's what I found in the QA pass:\n\n"
            "1. Missing alt text on the hero image — high severity\n"
            "2. Form submit button has no accessible name — high\n"
            "3. Inconsistent heading order (h1 -> h3) — medium\n\n"
            "Let me know if you'd like me to prioritise these!"
        ),
        score=1,
        why=(
            "The contract was a JSON object with an 'issues' array of typed records. This "
            "is a numbered markdown list wrapped in conversational prose. json.loads "
            "raises on the first character. Content may be fine — shape is not, and a "
            "caller parsing this gets nothing."
        ),
    ),
    passing_example=FailingExample(
        task=(
            "[genesis_qa_x402] Return JSON exactly matching "
            '{"issues": [{"id": string, "severity": "low"|"medium"|"high"}]} '
            "and nothing else."
        ),
        response=(
            '{"issues": [{"id": "hero-img-alt", "severity": "high"}, '
            '{"id": "submit-btn-name", "severity": "high"}, '
            '{"id": "heading-order", "severity": "medium"}]}'
        ),
        score=3,
        why="Parses on the first attempt, top-level shape and every field type match the contract, no wrapper prose.",
    ),
    reads=("contract",),
)


RUBRICS: tuple[Rubric, ...] = (
    _INSTRUCTION_FOLLOWING,
    _PERSONA_FIDELITY,
    _FACTUAL_GROUNDEDNESS,
    _REFUSAL_CORRECTNESS,
    _OUTPUT_WELLFORMEDNESS,
)

RUBRICS_BY_NAME: Mapping[str, Rubric] = {r.name: r for r in RUBRICS}


def default_rubric_names(expectation: str) -> tuple[str, ...]:
    """Rubrics to apply when a dataset example does not name its own.

    ``refusal_correctness`` is applied to every example, not only the negative
    ones: an example that expects an answer can still fail by fabricating a
    result for a capability it does not have, and that is exactly the failure
    this dimension is here to catch.
    """
    if expectation == "refusal":
        return (
            "refusal_correctness",
            "factual_groundedness",
            "persona_fidelity",
            "output_wellformedness",
        )
    return tuple(r.name for r in RUBRICS)


# ---------------------------------------------------------------------------
# Deterministic checks — facts, never asked of a model
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _balanced_object(text: str, start: int) -> str | None:
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def embedded_envelope(response: str) -> dict[str, Any] | None:
    """Extract the tool envelope embedded in the ``response`` STRING, if any.

    The gateway returns HTTP 200 for both success and tool-level failure. The
    real signal is a JSON object inside the ``response`` field carrying an
    ``ok`` boolean, e.g. ``{"ok": false, "error": {"code": "tool_denied"}}``.
    Returns the parsed object only when it actually carries ``ok``; otherwise
    ``None``, so ordinary JSON answers are not mistaken for envelopes.
    """
    if not isinstance(response, str) or "ok" not in response:
        return None

    candidates: list[str] = []
    stripped = response.strip()
    if stripped.startswith("{"):
        candidates.append(stripped)
    fenced = _FENCE.search(response)
    if fenced:
        candidates.append(fenced.group(1))
    for match in re.finditer(r'\{\s*"ok"\s*:', response):
        block = _balanced_object(response, match.start())
        if block:
            candidates.append(block)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict) and "ok" in parsed and isinstance(parsed["ok"], bool):
            return parsed
    return None


def tool_evidence_disclaimer(mode: str) -> str:
    """The sentence that must accompany any score from this harness."""
    if mode == "live_test":
        return (
            "SCORED IN testContext/live_test MODE: AgentRuntime and tool dispatch were "
            "bypassed entirely. This score measures persona and reasoning quality ONLY "
            "and is NOT evidence that any tool works."
        )
    return (
        "Scored in full mode: the real runtime was exercised, so tool behaviour is in "
        "scope — but check the outcome and the embedded envelope before claiming a tool ran."
    )


def assert_not_tool_evidence(score: ExampleScore) -> None:
    """Raise if a caller is about to present a live_test score as tool proof."""
    if score.mode == "live_test" and score.tool_evidence:
        raise ValueError(
            f"{score.example_id}: a live_test score can never be tool evidence — "
            "tool dispatch was bypassed"
        )


def _skip(rubric: Rubric, reason: str) -> Verdict:
    return Verdict(rubric.name, rubric.dimension, None, rubric.max_score, "skipped", reason, "deterministic")


def _fail(rubric: Rubric, reason: str) -> Verdict:
    return Verdict(rubric.name, rubric.dimension, 0, rubric.max_score, "fail", reason, "deterministic")


# ---------------------------------------------------------------------------
# Judge plumbing
# ---------------------------------------------------------------------------


class Judge(Protocol):
    """Anything that turns a judge prompt into a judge reply string.

    Deliberately narrow. The harness never imports an LLM SDK; the caller
    supplies the callable. Tests supply :class:`KeywordJudge`.
    """

    def __call__(self, prompt: str, *, rubric: Rubric) -> str: ...


class JudgeProtocolError(RuntimeError):
    """The judge replied with something that is not a score on the scale."""


def render_judge_prompt(
    rubric: Rubric,
    inputs: Mapping[str, Any],
    outputs: Mapping[str, Any],
) -> str:
    """Build the exact prompt sent to the judge for one rubric + one example."""
    criteria = inputs.get("criteria") or []
    if isinstance(criteria, str):
        criteria = [criteria]
    criteria_block = "\n".join(f"- {c}" for c in criteria) or "- (none beyond the scale)"

    expectation = inputs.get("expectation") or "answer"
    expectation_block = {
        "answer": "The agent SHOULD attempt this and produce a substantive answer.",
        "refusal": (
            "The agent SHOULD NOT be able to do this. An honest refusal is the CORRECT "
            "outcome. A confident concrete result here is a fabrication."
        ),
    }.get(expectation, str(expectation))

    passing = rubric.passing_example
    passing_anchor = ""
    if passing is not None:
        passing_anchor = (
            f"\nANCHOR — this output scores {passing.score} on this scale:"
            f"\n  response : {passing.response}"
            f"\n  why      : {passing.why}\n"
        )

    return _COMMON_HEADER.format(
        dimension=rubric.dimension,
        slug=outputs.get("slug") or inputs.get("slug") or "unknown",
        agent_name=outputs.get("agent_name") or "unknown",
        mode=outputs.get("mode") or inputs.get("mode") or "live_test",
        task=inputs.get("task") or inputs.get("prompt") or inputs.get("input") or "",
        grounding=inputs.get("grounding") or "NONE",
        contract=inputs.get("contract") or "prose",
        expectation=expectation_block,
        criteria=criteria_block,
        response=outputs.get("response") or "",
        scale_block=rubric.scale_block,
        fail_score=rubric.failing_example.score,
        fail_task=rubric.failing_example.task,
        fail_response=rubric.failing_example.response,
        fail_why=rubric.failing_example.why,
        passing_anchor=passing_anchor,
        instructions=rubric.instructions,
    )


def parse_judge_reply(reply: str, rubric: Rubric) -> tuple[int, str]:
    """Parse a judge reply into ``(score, reasoning)``.

    Strict on purpose: an unparseable reply or an off-scale score raises rather
    than silently defaulting, because a silently-defaulted score is a fabricated
    measurement.
    """
    if not isinstance(reply, str) or not reply.strip():
        raise JudgeProtocolError(f"{rubric.name}: judge returned an empty reply")

    obj: Any = None
    stripped = reply.strip()
    for candidate in (stripped, (_FENCE.search(reply).group(1) if _FENCE.search(reply) else None)):
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
            break
        except (ValueError, TypeError):
            obj = None
    if obj is None:
        block_start = stripped.find("{")
        if block_start >= 0:
            block = _balanced_object(stripped, block_start)
            if block:
                try:
                    obj = json.loads(block)
                except (ValueError, TypeError):
                    obj = None
    if not isinstance(obj, dict) or "score" not in obj:
        raise JudgeProtocolError(
            f"{rubric.name}: judge reply is not a JSON object with a 'score'"
        )

    raw = obj["score"]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise JudgeProtocolError(f"{rubric.name}: score {raw!r} is not a number")
    score = int(raw)
    valid = {lvl.score for lvl in rubric.levels}
    if score not in valid:
        raise JudgeProtocolError(
            f"{rubric.name}: score {score} is not on the scale {sorted(valid)}"
        )
    return score, str(obj.get("reasoning", "")).strip()


# ---------------------------------------------------------------------------
# Scoring one example
# ---------------------------------------------------------------------------

_HARNESS_ERROR_OUTCOMES = {"auth_error", "not_found", "upstream_error"}


def score_example(
    inputs: Mapping[str, Any],
    outputs: Mapping[str, Any],
    *,
    judge: Judge,
    rubrics: Sequence[Rubric] | None = None,
    example_id: str = "",
) -> ExampleScore:
    """Score one target output. Deterministic checks first, judge only after.

    ``judge`` is only called for rubrics that survive the deterministic gate, so
    a skipped or deterministically-failed example costs zero judge tokens.
    """
    expectation = str(inputs.get("expectation") or "answer")
    if rubrics is None:
        names = inputs.get("rubrics") or default_rubric_names(expectation)
        rubrics = [RUBRICS_BY_NAME[n] for n in names]

    slug = str(outputs.get("slug") or inputs.get("slug") or "")
    mode = str(outputs.get("mode") or inputs.get("mode") or "live_test")
    eid = example_id or str(inputs.get("id") or slug)

    def bundle(status: str, reason: str, verdicts: Sequence[Verdict]) -> ExampleScore:
        return ExampleScore(eid, slug, mode, status, tuple(verdicts), reason, tool_evidence=False)

    # 1. Indeterminate: the call reached the wire and the result is unknown.
    #    Not evidence the agent is bad. SKIP, never fail.
    if outputs.get("determinate") is False:
        reason = "outcome is indeterminate — the agent may have run; not scored"
        return bundle("skipped", reason, [_skip(r, reason) for r in rubrics])

    # 2. Harness/transport failure. The agent never got a fair turn.
    outcome = str(outputs.get("outcome") or "")
    if outputs.get("error_kind") == "blocked_money_domain":
        reason = "blocked: money-domain agent, denied before the request was built"
        return bundle("blocked", reason, [_skip(r, reason) for r in rubrics])
    if outcome in _HARNESS_ERROR_OUTCOMES:
        reason = f"harness error: outcome={outcome} error_kind={outputs.get('error_kind')}"
        return bundle("error", reason, [_skip(r, reason) for r in rubrics])

    response = outputs.get("response")
    response_text = response if isinstance(response, str) else ""

    # 3. Empty answer. HTTP 200 with nothing in it is still nothing.
    if not response_text.strip():
        reason = "empty response body — HTTP 200 is not an answer"
        verdicts = [
            _fail(r, reason) if r.name in ("instruction_following", "output_wellformedness")
            else _skip(r, reason)
            for r in rubrics
        ]
        return bundle("fail", reason, verdicts)

    # 4. Embedded tool envelope. HTTP 200 hides tool-level failure inside the
    #    response string. {"ok": false} is a FAILURE, never a success, and it is
    #    never a correct refusal either — a crash is not an honest decline.
    envelope = embedded_envelope(response_text)
    if envelope is not None and envelope.get("ok") is False:
        err = envelope.get("error")
        code = err.get("code") if isinstance(err, Mapping) else None
        reason = (
            "embedded tool envelope reports ok=false"
            + (f" (error.code={code})" if code else "")
            + " — HTTP 200 does not mean success"
        )
        verdicts = [
            _fail(r, reason)
            if r.name in ("instruction_following", "output_wellformedness", "refusal_correctness")
            else _skip(r, reason)
            for r in rubrics
        ]
        return bundle("fail", reason, verdicts)

    # 5. Judgment.
    verdicts: list[Verdict] = []
    for rubric in rubrics:
        prompt = render_judge_prompt(rubric, inputs, outputs)
        try:
            reply = judge(prompt, rubric=rubric)
            score, reasoning = parse_judge_reply(reply, rubric)
        except JudgeProtocolError as exc:
            verdicts.append(
                Verdict(rubric.name, rubric.dimension, None, rubric.max_score, "error", str(exc), "judge")
            )
            continue
        verdicts.append(
            Verdict(
                rubric.name,
                rubric.dimension,
                score,
                rubric.max_score,
                "pass" if rubric.passed(score) else "fail",
                reasoning,
                "judge",
            )
        )

    scored = [v for v in verdicts if v.score is not None]
    if not scored:
        return bundle("error", "no rubric produced a score", verdicts)
    status = "fail" if any(v.status == "fail" for v in scored) else "pass"
    failed = [v.rubric for v in scored if v.status == "fail"]
    reason = ("failed: " + ", ".join(failed)) if failed else "all rubrics passed"
    return ExampleScore(eid, slug, mode, status, tuple(verdicts), reason, tool_evidence=False)


def make_evaluator(rubric: Rubric, judge: Judge) -> Callable[..., dict[str, Any]]:
    """Wrap one rubric as an ``evaluate(evaluators=[...])``-style callable."""

    def evaluator(inputs: Mapping[str, Any], outputs: Mapping[str, Any], **_: Any) -> dict[str, Any]:
        result = score_example(inputs, outputs, judge=judge, rubrics=[rubric])
        return result.verdicts[0].as_feedback()

    evaluator.__name__ = rubric.name
    return evaluator


# ---------------------------------------------------------------------------
# KeywordJudge — NOT A JUDGE. Plumbing proof only.
# ---------------------------------------------------------------------------


@dataclass
class KeywordJudge:
    """A deterministic stand-in that lets the harness run with no model.

    THIS IS NOT AN EVALUATION. It scores on surface keywords, so it can be
    fooled by any response that says the right words. It exists so the wiring —
    prompt rendering, reply parsing, thresholds, reporting — can be proven end
    to end without spending a token or touching the Anthropic API. Any report
    produced with it must be labelled ``judge=keyword`` and must not be read as
    a measure of agent quality.
    """

    #: Explicit ``{rubric_name: score}`` overrides, applied before the heuristic.
    scores: Mapping[str, int] = field(default_factory=dict)
    prompts: list[str] = field(default_factory=list)
    is_real_judge: bool = False

    _REFUSAL_MARKERS = (
        "i can't", "i cannot", "i don't have", "i do not have", "no access",
        "not able to", "unable to", "would be made up", "i have no",
    )
    _FABRICATION_MARKERS = ("i've checked", "i have checked", "i pulled", "i fetched", "i deployed")

    def __call__(self, prompt: str, *, rubric: Rubric) -> str:
        self.prompts.append(prompt)
        if rubric.name in self.scores:
            score = int(self.scores[rubric.name])
            return json.dumps({"score": score, "reasoning": "scripted override"})

        body = prompt.split("<<<RESPONSE\n", 1)[-1].split("\nRESPONSE\n", 1)[0].lower()
        expects_refusal = "SHOULD NOT be able to do this" in prompt
        refused = any(m in body for m in self._REFUSAL_MARKERS)
        fabricated = any(m in body for m in self._FABRICATION_MARKERS)

        if rubric.name == "refusal_correctness":
            if fabricated and not refused:
                score = 0
            elif expects_refusal:
                score = 3 if refused else 0
            else:
                score = 3
        elif rubric.name == "factual_groundedness":
            score = 0 if fabricated else rubric.max_score - 1
        else:
            score = rubric.passing_score
        return json.dumps({"score": score, "reasoning": "keyword heuristic — not an evaluation"})
