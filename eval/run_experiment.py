"""Runnable experiment driver for the Genesis agent gateway.

Traces and evaluation verdicts go to Arize Phoenix (see eval/traceable.py).

    python -m eval.run_experiment --dataset eval/datasets/llm_only.jsonl --dry-run
    python -m eval.run_experiment --dataset eval/datasets/llm_only.jsonl --fake-client
    python -m eval.run_experiment --dataset eval/datasets/llm_only.jsonl   # needs a credential

HARD PRECONDITIONS — these abort the run, they are not warnings
---------------------------------------------------------------
1. EVERY slug in the dataset is validated against a live ``GET /agents`` before
   anything is invoked. A slug that is not served aborts the whole run with exit
   code 2.

   This is a hard precondition because a typo'd slug DOES NOT 404. ``main.py``'s
   ``run_agent()`` falls back to a generic persona built from ``slug.title()``
   and returns HTTP 200 with a bland answer. Only ``GET /capabilities`` 404s.
   Without this check the harness would score an agent that does not exist and
   report the result as if it did.

2. ``genesis_deploy_x402`` / ``deploy_agent`` are refused without
   ``--allow-deploy``. They have REAL external side effects (GitHub commits,
   pull requests and workspace shell execution).

3. Money-domain slugs (finance / billing / commerce / pricing / payment /
   payout / invoice / escrow) are refused without ``--allow-money``. They are
   denied at the Cato boundary anyway, so a score from them measures nothing.

WHAT A GREEN RUN DOES AND DOES NOT PROVE
----------------------------------------
The default mode is ``live_test`` (``testContext: true``), which BYPASSES
AgentRuntime and tool dispatch entirely and answers from the persona LLM. It is
the only practical mode on Render's free tier. A high score from a ``live_test``
run says NOTHING about whether any tool works. Every report this script writes
carries that sentence in its header, and ``--mode full`` is gated behind
``--i-understand-full-mode``.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

if __package__ in (None, ""):  # allow `python eval/run_experiment.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.genesis_client import (
    DEFAULT_BASE_URL,
    GenesisClient,
    MoneyDomainBlocked,
    is_money_domain,
    resolve_live_slug,
)
from eval.rubrics import (
    RUBRICS_BY_NAME,
    ExampleScore,
    Judge,
    KeywordJudge,
    default_rubric_names,
    score_example,
    tool_evidence_disclaimer,
)
from eval.target import arun_example

__all__ = [
    "DEFAULT_CONCURRENCY",
    "DEFAULT_TIMEOUT_S",
    "DEPLOY_SLUGS",
    "Example",
    "PreconditionFailed",
    "Report",
    "build_parser",
    "check_guards",
    "fetch_live_slugs",
    "load_dataset",
    "load_judge",
    "main",
    "run_experiment",
    "validate_slugs",
]

#: Measured: 49.2s and 74.7s for a single testContext call on a WARM instance.
#: Cold start was never measured, so 120s is a floor, not a generous margin.
DEFAULT_TIMEOUT_S = 120.0
#: Low on purpose. The gateway is a Render free-tier instance.
DEFAULT_CONCURRENCY = 2

#: Slugs with real external side effects. Refused without ``--allow-deploy``.
DEPLOY_SLUGS = frozenset({"genesis_deploy_x402", "deploy_agent"})

DATASET_DIR = Path(__file__).resolve().parent / "datasets"
SNAPSHOT_PATH = DATASET_DIR / "live_slugs.json"


class PreconditionFailed(RuntimeError):
    """A hard precondition failed. The run is aborted before any invocation."""


# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Example:
    id: str
    inputs: dict[str, Any]
    reference: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def slug(self) -> str:
        return str(self.inputs.get("slug", ""))

    @property
    def rubric_names(self) -> tuple[str, ...]:
        named = self.inputs.get("rubrics")
        if named:
            return tuple(str(n) for n in named)
        return default_rubric_names(str(self.inputs.get("expectation") or "answer"))


def load_dataset(path: str | Path) -> list[Example]:
    """Read a JSONL (or JSON-array) dataset into :class:`Example` objects."""
    p = Path(path)
    if not p.exists():
        raise PreconditionFailed(f"dataset not found: {p}")
    raw = p.read_text(encoding="utf-8").strip()
    if not raw:
        raise PreconditionFailed(f"dataset is empty: {p}")

    records: list[Any]
    if raw.lstrip().startswith("["):
        records = json.loads(raw)
    else:
        records = []
        for lineno, line in enumerate(raw.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError as exc:
                raise PreconditionFailed(f"{p}:{lineno}: not valid JSON — {exc}") from exc

    examples: list[Example] = []
    for i, rec in enumerate(records):
        if not isinstance(rec, Mapping) or not isinstance(rec.get("inputs"), Mapping):
            raise PreconditionFailed(f"{p}: record {i} has no 'inputs' mapping")
        inputs = dict(rec["inputs"])
        eid = str(rec.get("id") or f"{p.stem}-{i}")
        unknown = [n for n in (inputs.get("rubrics") or []) if n not in RUBRICS_BY_NAME]
        if unknown:
            raise PreconditionFailed(f"{p}: example {eid} names unknown rubrics {unknown}")
        examples.append(
            Example(
                id=eid,
                inputs=inputs,
                reference=dict(rec.get("outputs") or {}),
                metadata=dict(rec.get("metadata") or {}),
            )
        )
    return examples


# ---------------------------------------------------------------------------
# Precondition 1: live slug validation
# ---------------------------------------------------------------------------


def fetch_live_slugs(
    base_url: str = DEFAULT_BASE_URL, *, timeout_s: float = 30.0
) -> frozenset[str]:
    """Unauthenticated ``GET /agents``. The authority on what exists.

    Never sends a credential — this endpoint does not need one, and a harness
    precondition should not depend on auth state.
    """
    import httpx

    url = f"{base_url.rstrip('/')}/agents"
    try:
        resp = httpx.get(url, timeout=timeout_s)
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:  # noqa: BLE001 - any failure is a precondition failure
        raise PreconditionFailed(
            f"could not fetch the live agent list from {url} ({type(exc).__name__}). "
            "Refusing to run: without it a typo'd slug would be silently answered by "
            "a fallback persona. Use --slug-source snapshot only if you accept a "
            "stale list."
        ) from exc

    agents = body.get("agents") if isinstance(body, Mapping) else body
    if not isinstance(agents, list) or not agents:
        raise PreconditionFailed(f"{url} returned no agents")
    slugs = {
        (a.get("slug") if isinstance(a, Mapping) else a)
        for a in agents
    }
    return frozenset(s for s in slugs if isinstance(s, str) and s)


def load_snapshot_slugs(path: str | Path = SNAPSHOT_PATH) -> frozenset[str]:
    """Offline fallback. Stale by construction — prefer :func:`fetch_live_slugs`."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return frozenset(a["slug"] for a in data["agents"])


def validate_slugs(
    examples: Sequence[Example], live_slugs: Iterable[str]
) -> dict[str, str]:
    """ABORT unless every example's slug is served by the live gateway.

    Returns ``{example_id: resolved_live_slug}``. Raises
    :class:`PreconditionFailed` listing every offender — one exception for the
    whole dataset, so a run is not fixed one typo at a time.
    """
    live = frozenset(live_slugs)
    if not live:
        raise PreconditionFailed("the live slug set is empty — cannot validate anything")

    resolved: dict[str, str] = {}
    problems: list[str] = []
    for ex in examples:
        raw = ex.slug
        if not raw:
            problems.append(f"  {ex.id}: no slug")
            continue
        try:
            live_slug, resolution = resolve_live_slug(raw)
        except ValueError as exc:
            problems.append(f"  {ex.id}: {exc}")
            continue
        if live_slug not in live:
            near = sorted(s for s in live if raw.split("_")[0][:8] in s)[:5]
            problems.append(
                f"  {ex.id}: {raw!r} -> {live_slug!r} is NOT served by the gateway"
                + (f" (did you mean: {', '.join(near)})" if near else "")
            )
            continue
        resolved[ex.id] = live_slug
        if resolution != "verified":
            resolved[ex.id] = live_slug

    if problems:
        raise PreconditionFailed(
            "ABORTING: unknown slug(s) in the dataset.\n"
            + "\n".join(problems)
            + "\n\nA typo'd slug does NOT 404 on this gateway — run_agent() falls back to "
            "a generic persona built from slug.title() and returns HTTP 200 with a bland "
            "answer. Running anyway would score an agent that does not exist."
        )
    return resolved


# ---------------------------------------------------------------------------
# Precondition 2/3: side-effect and money guards
# ---------------------------------------------------------------------------


def check_guards(
    examples: Sequence[Example],
    *,
    allow_deploy: bool = False,
    allow_money: bool = False,
) -> None:
    """ABORT on side-effecting or money-domain slugs unless explicitly allowed."""
    blocked: list[str] = []
    for ex in examples:
        try:
            live_slug, _ = resolve_live_slug(ex.slug)
        except ValueError:
            live_slug = ex.slug
        if live_slug in DEPLOY_SLUGS and not allow_deploy:
            blocked.append(
                f"  {ex.id}: {live_slug} has REAL external side effects "
                "(GitHub commits/PRs, workspace shell). Override: --allow-deploy"
            )
        if is_money_domain(live_slug) and not allow_money:
            blocked.append(
                f"  {ex.id}: {live_slug} is a money-domain agent, denied at the Cato "
                "boundary by a hardcoded denylist. A score from it measures nothing. "
                "Override: --allow-money"
            )
    if blocked:
        raise PreconditionFailed(
            "ABORTING: guarded slug(s) in the dataset.\n"
            + "\n".join(blocked)
            + "\n\nDo not pass an override to turn a red run green."
        )


# ---------------------------------------------------------------------------
# Judges
# ---------------------------------------------------------------------------


def load_judge(spec: str) -> tuple[Judge | None, str]:
    """Resolve ``--judge``. Returns ``(judge, label)``.

    ``none``     — run the target only, score nothing.
    ``keyword``  — :class:`KeywordJudge`. NOT an evaluation. Plumbing proof only.
    ``pkg.mod:fn`` — any callable ``(prompt, *, rubric) -> str``. This is the hook
    for a real model-backed judge; none is shipped here and this script never
    imports an LLM SDK.
    """
    if spec == "none":
        return None, "none"
    if spec == "keyword":
        return KeywordJudge(), "keyword"
    if ":" not in spec:
        raise PreconditionFailed(
            f"--judge {spec!r}: expected 'none', 'keyword', or 'package.module:callable'"
        )
    mod_name, _, attr = spec.partition(":")
    try:
        mod = importlib.import_module(mod_name)
        judge = getattr(mod, attr)
    except Exception as exc:  # noqa: BLE001
        raise PreconditionFailed(f"--judge {spec!r}: {type(exc).__name__}: {exc}") from exc
    if not callable(judge):
        raise PreconditionFailed(f"--judge {spec!r}: {attr} is not callable")
    return judge, spec


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


@dataclass
class Report:
    dataset: str
    mode: str
    judge: str
    base_url: str
    timeout_s: float
    concurrency: int
    slug_source: str
    live_slug_count: int
    disclaimer: str
    started_utc: str
    elapsed_s: float = 0.0
    tool_evidence: bool = False
    results: list[dict[str, Any]] = field(default_factory=list)
    totals: dict[str, int] = field(default_factory=dict)
    rubric_totals: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)


async def _arun_all(
    examples: Sequence[Example],
    client: GenesisClient,
    *,
    concurrency: int,
) -> list[dict[str, Any]]:
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(ex: Example) -> dict[str, Any]:
        async with sem:
            try:
                return await arun_example(ex.inputs, client=client)
            except MoneyDomainBlocked as exc:
                return {
                    "response": "",
                    "outcome": "upstream_error",
                    "ok": False,
                    "determinate": True,
                    "slug": ex.slug,
                    "requested_slug": ex.slug,
                    "mode": ex.inputs.get("mode") or "live_test",
                    "error_kind": "blocked_money_domain",
                    "error_message": str(exc),
                }

    return list(await asyncio.gather(*(one(ex) for ex in examples)))


def run_experiment(
    examples: Sequence[Example],
    *,
    client: GenesisClient,
    judge: Judge | None,
    judge_label: str = "none",
    mode: str = "live_test",
    concurrency: int = DEFAULT_CONCURRENCY,
    dataset_name: str = "",
    slug_source: str = "live",
    live_slug_count: int = 0,
) -> Report:
    """Invoke the target for every example, then score with the rubrics.

    ``client`` is injected, so the whole path is exercisable against a fake
    transport with no network. Preconditions are the caller's job — run
    :func:`validate_slugs` and :func:`check_guards` first; :func:`main` does.
    """
    report = Report(
        dataset=dataset_name,
        mode=mode,
        judge=judge_label,
        base_url=client.base_url,
        timeout_s=client.timeout_s,
        concurrency=concurrency,
        slug_source=slug_source,
        live_slug_count=live_slug_count,
        disclaimer=tool_evidence_disclaimer(mode),
        started_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    t0 = time.monotonic()
    outputs = asyncio.run(_arun_all(examples, client, concurrency=concurrency))
    report.elapsed_s = round(time.monotonic() - t0, 2)

    totals = {"pass": 0, "fail": 0, "skipped": 0, "error": 0, "blocked": 0}
    rubric_totals: dict[str, dict[str, Any]] = {}

    for ex, out in zip(examples, outputs):
        if judge is None:
            score = ExampleScore(
                ex.id,
                str(out.get("slug") or ex.slug),
                str(out.get("mode") or mode),
                "skipped",
                (),
                "judge=none — target invoked, nothing scored",
            )
        else:
            score = score_example(
                ex.inputs,
                out,
                judge=judge,
                rubrics=[RUBRICS_BY_NAME[n] for n in ex.rubric_names],
                example_id=ex.id,
            )
        totals[score.status] = totals.get(score.status, 0) + 1
        for v in score.verdicts:
            slot = rubric_totals.setdefault(
                v.rubric,
                {"pass": 0, "fail": 0, "skipped": 0, "error": 0, "scores": []},
            )
            slot[v.status] = slot.get(v.status, 0) + 1
            if v.score is not None:
                slot["scores"].append(v.score)
        report.results.append(
            {
                "id": ex.id,
                "slug": score.slug,
                "requested_slug": ex.slug,
                "expectation": ex.inputs.get("expectation", "answer"),
                "negative": bool(ex.metadata.get("negative")),
                "status": score.status,
                "reason": score.reason,
                "outcome": out.get("outcome"),
                "determinate": out.get("determinate"),
                "http_status": out.get("http_status"),
                "elapsed_ms": out.get("elapsed_ms"),
                "response_chars": len(str(out.get("response") or "")),
                "verdicts": [
                    {
                        "rubric": v.rubric,
                        "score": v.score,
                        "max": v.max_score,
                        "status": v.status,
                        "source": v.source,
                        "comment": v.comment,
                    }
                    for v in score.verdicts
                ],
            }
        )

    for slot in rubric_totals.values():
        scores = slot.pop("scores")
        slot["n_scored"] = len(scores)
        slot["mean"] = round(sum(scores) / len(scores), 2) if scores else None
    report.totals = totals
    report.rubric_totals = rubric_totals
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m eval.run_experiment",
        description="Run a Genesis agent evaluation. Validates every slug against the "
        "live gateway and aborts on a mismatch.",
    )
    p.add_argument("--dataset", required=True, help="path to a .jsonl dataset")
    p.add_argument(
        "--mode",
        choices=("live_test", "full"),
        default="live_test",
        help="live_test (default) sets testContext:true and BYPASSES tool dispatch",
    )
    p.add_argument(
        "--i-understand-full-mode",
        action="store_true",
        help="required with --mode full: real runtime, real side effects, async job "
        "envelopes this runner does not poll",
    )
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    p.add_argument("--base-url", default=os.getenv("GENESIS_BASE_URL", DEFAULT_BASE_URL))
    p.add_argument(
        "--slug-source",
        choices=("live", "snapshot"),
        default="live",
        help="'live' fetches GET /agents (default and correct). 'snapshot' reads the "
        "stale committed list — offline use only.",
    )
    p.add_argument(
        "--judge",
        default="keyword",
        help="none | keyword | package.module:callable. 'keyword' is NOT an evaluation.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="validate slugs and guards, print the plan, invoke nothing",
    )
    p.add_argument(
        "--fake-client",
        action="store_true",
        help="offline smoke test: scripted transport, no network, no credential",
    )
    p.add_argument("--allow-deploy", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--allow-money", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--out", help="write the JSON report to this path")
    return p


def _fake_client(timeout_s: float) -> GenesisClient:
    """A GenesisClient wired to a scripted transport. Never touches the network."""
    from eval.genesis_client import RawResponse

    class _Scripted:
        async def request(self, method: str, url: str, **kw: Any) -> RawResponse:
            if url.endswith("/health"):
                return RawResponse(200, '{"status":"ok"}')
            return RawResponse(
                200,
                json.dumps(
                    {
                        "response": (
                            "FAKE CLIENT RESPONSE — no agent was invoked. I cannot reach "
                            "any live system from here, so I have no data to report."
                        ),
                        "agentName": "Fake Agent",
                    }
                ),
            )

        async def aclose(self) -> None:
            return None

    return GenesisClient(transport=_Scripted(), timeout_s=timeout_s, warmup=False)


def _print_summary(report: Report) -> None:
    print()
    print("=" * 78)
    print(report.disclaimer)
    print("=" * 78)
    print(f"dataset     : {report.dataset}")
    print(f"mode        : {report.mode}    judge: {report.judge}    concurrency: {report.concurrency}")
    print(f"slug source : {report.slug_source} ({report.live_slug_count} slugs validated)")
    print(f"elapsed     : {report.elapsed_s}s")
    print()
    for r in report.results:
        flag = {"pass": "PASS", "fail": "FAIL", "skipped": "SKIP",
                "error": "ERR ", "blocked": "BLCK"}.get(r["status"], "????")
        neg = " [negative]" if r["negative"] else ""
        print(f"  {flag}  {r['id']}{neg}")
        if r["status"] != "pass":
            print(f"          {r['reason']}")
        for v in r["verdicts"]:
            if v["score"] is not None:
                print(f"          {v['rubric']:<24} {v['score']}/{v['max']}  ({v['status']}, {v['source']})")
    print()
    print("  totals:", ", ".join(f"{k}={v}" for k, v in report.totals.items() if v))
    for name, slot in sorted(report.rubric_totals.items()):
        if slot["n_scored"]:
            print(f"  {name:<24} mean {slot['mean']}  pass={slot['pass']} fail={slot['fail']}")
    print()
    if report.judge == "keyword":
        print("  judge=keyword: surface heuristics, NOT an evaluation. These numbers")
        print("  prove the plumbing works. They do not measure agent quality.")
    print()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.mode == "full" and not args.i_understand_full_mode:
            raise PreconditionFailed(
                "--mode full exercises the REAL runtime: real side effects, real cost, "
                "and 5 of the tool-capable slugs return an async job envelope "
                "(job_id + poll_url) that this runner does not poll. Pass "
                "--i-understand-full-mode if that is genuinely what you want."
            )

        examples = load_dataset(args.dataset)
        for ex in examples:
            ex.inputs.setdefault("mode", args.mode)
            if args.mode == "full":
                ex.inputs["mode"] = "full"

        if args.slug_source == "live":
            live = fetch_live_slugs(args.base_url)
        else:
            live = load_snapshot_slugs()
            print(
                "WARNING: --slug-source snapshot. The committed list is stale by "
                "construction; only the live gateway is authoritative.",
                file=sys.stderr,
            )
        validate_slugs(examples, live)
        check_guards(examples, allow_deploy=args.allow_deploy, allow_money=args.allow_money)

        judge, judge_label = load_judge(args.judge)
    except PreconditionFailed as exc:
        print(f"\nPRECONDITION FAILED\n{exc}\n", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"\nDRY RUN — {len(examples)} example(s), all slugs validated against "
              f"{len(live)} live slugs, guards clear. Nothing invoked.")
        print(tool_evidence_disclaimer(args.mode))
        for ex in examples:
            print(f"  {ex.id:<44} {ex.slug:<32} {','.join(ex.rubric_names)}")
        return 0

    if args.fake_client:
        client = _fake_client(args.timeout)
    else:
        client = GenesisClient(base_url=args.base_url, timeout_s=args.timeout)
        if not client.has_credential():
            print(
                "\nPRECONDITION FAILED\nNo gateway credential in the environment "
                "(GATEWAY_API_KEY or AGENT_GATEWAY_SECRET). Use --dry-run to validate "
                "the dataset, or --fake-client for an offline smoke test.\n",
                file=sys.stderr,
            )
            return 2

    report = run_experiment(
        examples,
        client=client,
        judge=judge,
        judge_label=judge_label,
        mode=args.mode,
        concurrency=args.concurrency,
        dataset_name=str(args.dataset),
        slug_source=args.slug_source,
        live_slug_count=len(live),
    )
    _print_summary(report)
    if args.out:
        Path(args.out).write_text(report.to_json(), encoding="utf-8")
        print(f"  report written to {args.out}\n")
    return 1 if report.totals.get("fail") else 0


if __name__ == "__main__":
    raise SystemExit(main())
