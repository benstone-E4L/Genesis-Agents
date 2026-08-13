#!/usr/bin/env python
"""Regenerate expected_policy_matrix.json.

Running this is a deliberate act of changing who may call what. The fixture is
the one screen on which a reviewer sees a commit's full permission delta
(docs/FINANCE-TOOL-CONTRACTS.md Section 7 Phase 3), so it must be regenerated
from live check_tool_policy() output and never hand-edited — a hand-edit can
assert a permission the runtime does not actually enforce, which is precisely
the drift the fixture exists to catch.

If you are running this to make test_tool_policy_matrix.py go green, stop and
confirm the permission change you are recording is one you intend.

The matrix is keyed "<slug>::<tool_name>" over every SLUG_ALLOWED_RISKS key
plus one sentinel slug standing in for DEFAULT_ALLOWED_RISKS, crossed with
every live-registered tool name. Both the slug list and the name list come from
the running system, so a tool that was deleted disappears from the fixture and
a tool that was added must be reviewed in.

Usage:
    python scripts/regen_policy_matrix.py            # write the fixture
    python scripts/regen_policy_matrix.py --check    # exit 1 if it would change
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURE = ROOT / "expected_policy_matrix.json"

# Must stay identical to test_tool_policy_matrix._UNKNOWN_SLUG.
UNKNOWN_SLUG = "unknown-slug-xyz"


def build_matrix() -> dict[str, dict[str, object]]:
    import tools  # noqa: E402
    from runtime.tool_policy import SLUG_ALLOWED_RISKS, check_tool_policy  # noqa: E402

    tools.register_default_tools()
    names = sorted(tools._TOOLS)
    slugs = sorted(SLUG_ALLOWED_RISKS) + [UNKNOWN_SLUG]

    matrix: dict[str, dict[str, object]] = {}
    for slug in slugs:
        for name in names:
            decision = check_tool_policy(slug, name)
            matrix[f"{slug}::{name}"] = {
                "ok": decision["ok"],
                "risk_class": decision["risk_class"],
            }
    return matrix


def main() -> int:
    check_only = "--check" in sys.argv
    matrix = build_matrix()
    body = json.dumps(matrix, indent=2, ensure_ascii=False, sort_keys=True) + "\n"

    if check_only:
        current = FIXTURE.read_text(encoding="utf-8") if FIXTURE.exists() else ""
        if current != body:
            print("expected_policy_matrix.json is STALE — rerun without --check")
            return 1
        print(f"expected_policy_matrix.json is current ({len(matrix)} pairs)")
        return 0

    FIXTURE.write_text(body, encoding="utf-8")
    allowed = sum(1 for v in matrix.values() if v["ok"])
    by_risk: dict[str, int] = {}
    for v in matrix.values():
        if v["ok"]:
            by_risk[str(v["risk_class"])] = by_risk.get(str(v["risk_class"]), 0) + 1
    print(f"wrote {FIXTURE}")
    print(f"pairs={len(matrix)} allowed={allowed} denied={len(matrix) - allowed}")
    print("allowed by risk class: " + ", ".join(f"{k}={v}" for k, v in sorted(by_risk.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
