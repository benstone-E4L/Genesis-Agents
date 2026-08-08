"""Dataset conformance. Proves the schema reconciles with t25's eval/target.py.

If these fail after t25 changes ``parse_example``, the datasets are the thing
that must move — ``target.py`` owns the input contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.rubrics import RUBRICS_BY_NAME
from eval.run_experiment import load_dataset
from eval.target import parse_example

DATASETS = Path(__file__).resolve().parents[1] / "datasets"
SCORED = ("llm_only.jsonl", "tools_capable.jsonl")
ALL = SCORED + ("guarded_do_not_run.jsonl",)


@pytest.mark.parametrize("name", ALL)
def test_every_example_satisfies_t25_target_input_schema(name):
    """The authority on the input contract is target.parse_example."""
    for ex in load_dataset(DATASETS / name):
        parsed = parse_example(ex.inputs)  # raises InvalidExample if non-conformant
        assert parsed["slug"] == ex.inputs["slug"]
        assert parsed["mode"] in ("live_test", "full")


@pytest.mark.parametrize("name", ALL)
def test_rubric_only_fields_are_ignored_by_the_target(name):
    """Rubric-only keys ride in the same inputs dict; the target must ignore them."""
    for ex in load_dataset(DATASETS / name):
        parsed = parse_example(ex.inputs)
        for key in ("expectation", "grounding", "contract", "criteria", "rubrics"):
            assert key not in parsed, f"{key} leaked into the gateway request body"


@pytest.mark.parametrize("name", SCORED)
def test_each_scored_dataset_has_at_least_two_negative_examples(name):
    examples = load_dataset(DATASETS / name)
    negatives = [e for e in examples if e.metadata.get("negative")]
    assert len(negatives) >= 2, f"{name}: rubrics are only exercised in one direction"
    for e in negatives:
        assert e.inputs["expectation"] == "refusal"
        assert "refusal_correctness" in e.rubric_names


@pytest.mark.parametrize("name", SCORED)
def test_negative_examples_declare_no_grounding_so_any_specific_is_invented(name):
    for e in load_dataset(DATASETS / name):
        if e.metadata.get("negative"):
            assert str(e.inputs.get("grounding", "")).startswith("NONE"), e.id


@pytest.mark.parametrize("name", SCORED)
def test_every_example_declares_grounding_a_contract_and_criteria(name):
    for e in load_dataset(DATASETS / name):
        assert e.inputs.get("grounding"), f"{e.id}: judge cannot check groundedness without it"
        assert e.inputs.get("contract"), f"{e.id}: output_wellformedness needs a contract"
        assert e.inputs.get("criteria"), f"{e.id}: no example-specific criteria"
        for name_ in e.rubric_names:
            assert name_ in RUBRICS_BY_NAME


@pytest.mark.parametrize("name", SCORED)
def test_scored_datasets_default_to_live_test_mode(name):
    for e in load_dataset(DATASETS / name):
        assert e.inputs.get("mode", "live_test") == "live_test", (
            f"{e.id}: a scored dataset must not default to full mode"
        )


def test_manifest_matches_the_files_on_disk():
    manifest = json.loads((DATASETS / "_manifest.json").read_text(encoding="utf-8"))
    declared = {d["file"]: d for d in manifest["datasets"]}
    assert set(declared) == set(ALL)
    for name, spec in declared.items():
        examples = load_dataset(DATASETS / name)
        assert spec["examples"] == len(examples), name
        if "negatives" in spec:
            actual = sum(1 for e in examples if e.metadata.get("negative"))
            assert spec["negatives"] == actual, name


def test_manifest_excludes_the_three_money_agents_and_deploy():
    manifest = json.loads((DATASETS / "_manifest.json").read_text(encoding="utf-8"))
    excluded = manifest["excluded_from_all_datasets"]
    assert set(excluded) == {
        "genesis_finance_x402",
        "genesis_billing_x402",
        "genesis_commerce_x402",
        "genesis_deploy_x402",
    }


def test_deploy_appears_only_in_the_quarantine_file():
    for name in SCORED:
        slugs = {e.inputs["slug"] for e in load_dataset(DATASETS / name)}
        assert "genesis_deploy_x402" not in slugs
        assert "deploy_agent" not in slugs
