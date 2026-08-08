"""CI gate for escrow containment.

docs/FINANCE-TOOL-CONTRACTS.md Sections 3.8 and 6.3.

escrow_client.py is not dormant: eleven live call sites, one of which settles
escrow on job success with no human in the loop. These tests prove the
containment is fail-closed by default and that the Cato-facing build refuses to
start if the module is still shipped.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import escrow_guard  # noqa: E402
from escrow_guard import (  # noqa: E402
    ESCROW_FUNCTIONS,
    PROFILE_CATO,
    PROFILE_ENV_VAR,
    PROFILE_MARKETPLACE,
    assert_escrow_containment,
    escrow_blocked,
    escrow_permitted,
)

MAIN_PY = PROJECT_ROOT / "main.py"
WORKER_PY = PROJECT_ROOT / "worker.py"


class TestFailClosedDefault:
    def test_escrow_is_blocked_when_the_profile_is_unset(self, monkeypatch):
        monkeypatch.delenv(PROFILE_ENV_VAR, raising=False)
        assert escrow_permitted() is False, (
            "Escrow must be blocked unless a deployment explicitly claims it. "
            "An over-restrictive result is correctable; an over-permissive one is not."
        )

    @pytest.mark.parametrize(
        "value",
        ["", "cato", "CATO", "e4l", "production", "true", "1", "swarmsync", "marketplace"],
    )
    def test_only_the_exact_marketplace_profile_permits_escrow(self, monkeypatch, value):
        monkeypatch.setenv(PROFILE_ENV_VAR, value)
        assert escrow_permitted() is False, f"profile {value!r} must not permit escrow"

    def test_marketplace_profile_permits_escrow(self, monkeypatch):
        monkeypatch.setenv(PROFILE_ENV_VAR, PROFILE_MARKETPLACE)
        assert escrow_permitted() is True


class TestCatoFacingBuildRefusesToStart:
    def test_negative_control_cato_build_with_escrow_client_present_raises(self, monkeypatch):
        """NEGATIVE CONTROL.

        escrow_client.py IS present in this repo, so declaring the Cato-facing
        profile must make the boot assertion raise. If this test ever passes
        silently, the assertion is decorative.
        """
        monkeypatch.setenv(PROFILE_ENV_VAR, PROFILE_CATO)
        assert importlib.util.find_spec("escrow_client") is not None, (
            "precondition: escrow_client.py is present in this repo"
        )
        with pytest.raises(RuntimeError, match="escrow_client is importable in a Cato-facing build"):
            assert_escrow_containment()

    def test_cato_build_passes_once_escrow_client_is_absent(self, monkeypatch):
        """The Cato-facing artefact must not ship escrow_client.py at all.

        Simulated by making the module unfindable, which is what removing it
        from the deployment artefact achieves.
        """
        monkeypatch.setenv(PROFILE_ENV_VAR, PROFILE_CATO)
        monkeypatch.setattr(escrow_guard, "escrow_client_importable", lambda: False)
        assert_escrow_containment()  # must not raise

    def test_default_profile_boots_but_blocks(self, monkeypatch):
        monkeypatch.delenv(PROFILE_ENV_VAR, raising=False)
        assert_escrow_containment()  # must not raise
        assert escrow_permitted() is False


class TestBlockedResponse:
    def test_blocked_response_is_a_failure_envelope(self, monkeypatch):
        monkeypatch.delenv(PROFILE_ENV_VAR, raising=False)
        resp = escrow_blocked("complete_escrow")
        assert resp["ok"] is False
        assert resp["error"]["code"] == "policy_denied"
        assert resp["error"]["retryable"] is False
        assert resp["escrow_blocked"] is True
        assert resp["tool"] == "escrow_client.complete_escrow"

    def test_all_four_escrow_functions_are_named_in_the_prohibition_list(self):
        from runtime.tool_policy import PROHIBITED_TOOLS

        for name in ESCROW_FUNCTIONS:
            assert name in PROHIBITED_TOOLS


class TestCallSitesAreGuarded:
    def test_main_binds_escrow_only_behind_the_guard(self):
        text = MAIN_PY.read_text(encoding="utf-8")
        assert "from escrow_guard import escrow_permitted as _escrow_permitted" in text
        # The import of escrow_client must sit inside the permitted branch.
        guard_at = text.index("if not _escrow_permitted():")
        import_at = text.index("from escrow_client import (")
        assert guard_at < import_at, (
            "escrow_client must only be imported inside the escrow-permitted branch"
        )

    def test_worker_guards_every_local_escrow_import(self):
        """worker.py imports escrow_client inside functions, bypassing main.py's
        binding, so each site needs its own guard."""
        lines = WORKER_PY.read_text(encoding="utf-8").splitlines()
        local_imports = [i for i, l in enumerate(lines) if "from escrow_client import" in l]
        assert local_imports, "precondition: worker.py has local escrow_client imports"
        for i in local_imports:
            window = "\n".join(lines[max(0, i - 8):i])
            assert "escrow_permitted()" in window, (
                f"worker.py line {i + 1} imports escrow_client with no escrow_permitted() "
                "guard within the preceding 8 lines"
            )

    def test_every_call_site_carries_a_numbered_blocker_comment(self):
        """All eleven verified call sites must be accounted for in the source."""
        blockers = []
        for path in (MAIN_PY, WORKER_PY):
            blockers += re.findall(r"ESCROW BLOCKERS? ([\d/,\sand]+) —", path.read_text(encoding="utf-8"))
        assert blockers, "no ESCROW BLOCKER markers found"
        numbers: set[str] = set()
        for chunk in blockers:
            numbers.update(re.findall(r"(\d+)/\d+", chunk))
        assert len(numbers) == 11, (
            f"expected 11 numbered escrow blockers, found {sorted(numbers, key=int)}"
        )
