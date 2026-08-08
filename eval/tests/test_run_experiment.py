"""Runner tests. Fake transport only — no test here touches the network.

Every test that exercises the CLI passes ``--slug-source snapshot`` so the
committed slug list is used instead of a live ``GET /agents``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.genesis_client import GenesisClient, RawResponse
from eval.rubrics import KeywordJudge
from eval.run_experiment import (
    DEFAULT_CONCURRENCY,
    DEFAULT_TIMEOUT_S,
    DEPLOY_SLUGS,
    PreconditionFailed,
    check_guards,
    load_dataset,
    load_judge,
    load_snapshot_slugs,
    main,
    run_experiment,
    validate_slugs,
)
from eval.tests.fakes import FakeTransport

DATASETS = Path(__file__).resolve().parents[1] / "datasets"
LIVE = load_snapshot_slugs()


def write_jsonl(tmp_path: Path, name: str, records: list[dict]) -> Path:
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return p


def example(slug: str, **inputs) -> dict:
    body = {"slug": slug, "task": "do the thing", "mode": "live_test"}
    body.update(inputs)
    return {"id": f"ex-{slug}", "inputs": body, "outputs": {}, "metadata": {}}


# --------------------------------------------------------------------------
# Defaults required by the task packet
# --------------------------------------------------------------------------


def test_runner_defaults_are_120s_timeout_low_concurrency_and_live_test():
    from eval.run_experiment import build_parser

    args = build_parser().parse_args(["--dataset", "x.jsonl"])
    assert args.mode == "live_test"          # testContext by default
    assert args.timeout == DEFAULT_TIMEOUT_S == 120.0
    assert args.concurrency == DEFAULT_CONCURRENCY == 2
    assert args.slug_source == "live"        # live validation is the default
    assert args.allow_deploy is False and args.allow_money is False


def test_full_mode_requires_an_explicit_acknowledgement(tmp_path, capsys):
    ds = write_jsonl(tmp_path, "d.jsonl", [example("genesis_seo_x402")])
    assert main(["--dataset", str(ds), "--mode", "full", "--slug-source", "snapshot"]) == 2
    assert "--i-understand-full-mode" in capsys.readouterr().err


# --------------------------------------------------------------------------
# PRECONDITION 1: slug validation aborts on an unknown slug
# --------------------------------------------------------------------------


def test_validate_slugs_aborts_on_an_unknown_slug(tmp_path):
    examples = load_dataset(
        write_jsonl(
            tmp_path,
            "typo.jsonl",
            [example("genesis_seo_x402"), example("genesis_reserch_x402")],
        )
    )
    with pytest.raises(PreconditionFailed) as exc:
        validate_slugs(examples, LIVE)

    msg = str(exc.value)
    assert "ABORTING" in msg
    assert "genesis_reserch_x402" in msg
    assert "does NOT 404" in msg, "the abort must explain WHY this is a hard precondition"


def test_abort_lists_every_bad_slug_at_once(tmp_path):
    examples = load_dataset(
        write_jsonl(
            tmp_path,
            "typos.jsonl",
            [example("nope_one_x402"), example("nope_two_x402"), example("genesis_seo_x402")],
        )
    )
    with pytest.raises(PreconditionFailed) as exc:
        validate_slugs(examples, LIVE)
    assert "nope_one_x402" in str(exc.value) and "nope_two_x402" in str(exc.value)


def test_cli_exits_2_and_invokes_nothing_on_an_unknown_slug(tmp_path, capsys):
    ds = write_jsonl(tmp_path, "typo.jsonl", [example("genesis_reserch_x402")])
    code = main(["--dataset", str(ds), "--slug-source", "snapshot", "--fake-client"])
    assert code == 2
    err = capsys.readouterr().err
    assert "PRECONDITION FAILED" in err and "genesis_reserch_x402" in err


def test_hyphenated_bundle_slug_resolves_to_the_live_form(tmp_path):
    examples = load_dataset(write_jsonl(tmp_path, "d.jsonl", [example("genesis-research")]))
    assert validate_slugs(examples, LIVE)["ex-genesis-research"] == "genesis_research_x402"


def test_every_committed_dataset_passes_slug_validation():
    for path in sorted(DATASETS.glob("*.jsonl")):
        validate_slugs(load_dataset(path), LIVE)


def test_empty_live_slug_set_aborts_rather_than_vacuously_passing(tmp_path):
    examples = load_dataset(write_jsonl(tmp_path, "d.jsonl", [example("genesis_seo_x402")]))
    with pytest.raises(PreconditionFailed, match="empty"):
        validate_slugs(examples, [])


# --------------------------------------------------------------------------
# PRECONDITION 2/3: deploy and money guards
# --------------------------------------------------------------------------


def test_deploy_guard_blocks_without_the_override(tmp_path):
    examples = load_dataset(write_jsonl(tmp_path, "d.jsonl", [example("genesis_deploy_x402")]))
    with pytest.raises(PreconditionFailed) as exc:
        check_guards(examples)
    assert "REAL external side effects" in str(exc.value)
    assert "--allow-deploy" in str(exc.value)

    check_guards(examples, allow_deploy=True)  # explicit override: does not raise


def test_deploy_agent_is_guarded_too():
    assert "deploy_agent" in DEPLOY_SLUGS and "genesis_deploy_x402" in DEPLOY_SLUGS


@pytest.mark.parametrize(
    "slug", ["genesis_finance_x402", "genesis_billing_x402", "genesis_commerce_x402", "pricing_agent"]
)
def test_money_guard_blocks_without_the_override(tmp_path, slug):
    examples = load_dataset(write_jsonl(tmp_path, f"{slug}.jsonl", [example(slug)]))
    with pytest.raises(PreconditionFailed) as exc:
        check_guards(examples)
    assert "money-domain" in str(exc.value) and "--allow-money" in str(exc.value)

    check_guards(examples, allow_money=True)


def test_cli_aborts_on_the_committed_guarded_dataset(capsys):
    code = main([
        "--dataset", str(DATASETS / "guarded_do_not_run.jsonl"),
        "--slug-source", "snapshot", "--dry-run",
    ])
    assert code == 2
    err = capsys.readouterr().err
    assert "genesis_deploy_x402" in err
    assert "genesis_finance_x402" in err
    assert "Do not pass an override to turn a red run green." in err


def test_no_scored_dataset_contains_a_guarded_slug():
    for path in sorted(DATASETS.glob("*.jsonl")):
        if path.name == "guarded_do_not_run.jsonl":
            continue
        check_guards(load_dataset(path))  # must not raise


# --------------------------------------------------------------------------
# End to end against a fake client
# --------------------------------------------------------------------------


def fake_client(response_text: str) -> GenesisClient:
    body = json.dumps({"response": response_text, "agentName": "Fake"})
    return GenesisClient(
        transport=FakeTransport([RawResponse(200, body)]),
        warmup=False,
        api_key="unused-in-tests",
    )


def test_end_to_end_embedded_ok_false_is_reported_as_a_failure(tmp_path):
    examples = load_dataset(write_jsonl(tmp_path, "d.jsonl", [example("genesis_qa_x402")]))
    report = run_experiment(
        examples,
        client=fake_client('{"ok": false, "error": {"code": "tool_denied"}}'),
        judge=KeywordJudge(),
        judge_label="keyword",
    )
    assert report.totals["fail"] == 1
    assert report.totals.get("pass", 0) == 0
    result = report.results[0]
    assert result["http_status"] == 200, "HTTP 200 and still a failure — that is the point"
    assert result["outcome"] == "success"
    assert "ok=false" in result["reason"]


def test_end_to_end_correct_refusal_passes(tmp_path):
    examples = load_dataset(
        write_jsonl(
            tmp_path,
            "d.jsonl",
            [
                example(
                    "genesis_analyst_x402",
                    task="Summarise our Q3 revenue.",
                    expectation="refusal",
                    grounding="NONE",
                    rubrics=["refusal_correctness", "factual_groundedness"],
                )
            ],
        )
    )
    report = run_experiment(
        examples,
        client=fake_client(
            "I can't give you Q3 revenue — I have no access to your billing data from "
            "here, so any figure would be made up. Send a revenue-by-tier export."
        ),
        judge=KeywordJudge(),
        judge_label="keyword",
    )
    assert report.totals["pass"] == 1
    scores = {v["rubric"]: v["score"] for v in report.results[0]["verdicts"]}
    assert scores["refusal_correctness"] == 3


def test_report_header_always_carries_the_testcontext_disclaimer(tmp_path):
    examples = load_dataset(write_jsonl(tmp_path, "d.jsonl", [example("genesis_seo_x402")]))
    report = run_experiment(examples, client=fake_client("an answer"), judge=None)
    assert "NOT evidence that any tool works" in report.disclaimer
    assert report.tool_evidence is False
    assert "NOT evidence that any tool works" in report.to_json()


def test_concurrency_is_bounded(tmp_path):
    examples = load_dataset(
        write_jsonl(tmp_path, "d.jsonl", [example("genesis_seo_x402") for _ in range(6)])
    )
    for i, ex in enumerate(examples):
        ex.inputs["task"] = f"task {i}"
    transport = FakeTransport([RawResponse(200, json.dumps({"response": "ok"}))])
    client = GenesisClient(transport=transport, warmup=False, api_key="unused")
    report = run_experiment(examples, client=client, judge=None, concurrency=2)
    assert len(report.results) == 6
    assert transport.run_call_count == 6


def test_cli_dry_run_on_the_real_datasets_invokes_nothing(capsys):
    for name in ("llm_only.jsonl", "tools_capable.jsonl"):
        assert main([
            "--dataset", str(DATASETS / name), "--slug-source", "snapshot", "--dry-run",
        ]) == 0
    assert "Nothing invoked" in capsys.readouterr().out


def test_missing_credential_aborts_rather_than_running_unauthenticated(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("AGENT_GATEWAY_SECRET", raising=False)
    ds = write_jsonl(tmp_path, "d.jsonl", [example("genesis_seo_x402")])
    assert main(["--dataset", str(ds), "--slug-source", "snapshot"]) == 2
    assert "No gateway credential" in capsys.readouterr().err


# --------------------------------------------------------------------------
# Dataset loading
# --------------------------------------------------------------------------


def test_unknown_rubric_name_in_a_dataset_aborts(tmp_path):
    ds = write_jsonl(tmp_path, "d.jsonl", [example("genesis_seo_x402", rubrics=["vibes"])])
    with pytest.raises(PreconditionFailed, match="unknown rubrics"):
        load_dataset(ds)


def test_malformed_jsonl_names_the_line(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_text('{"inputs": {"slug": "genesis_seo_x402", "task": "t"}}\nnot json\n', encoding="utf-8")
    with pytest.raises(PreconditionFailed, match=":2:"):
        load_dataset(p)


def test_judge_spec_none_and_keyword_resolve_and_garbage_aborts():
    assert load_judge("none") == (None, "none")
    judge, label = load_judge("keyword")
    assert isinstance(judge, KeywordJudge) and label == "keyword"
    with pytest.raises(PreconditionFailed):
        load_judge("some-nonexistent-thing")
