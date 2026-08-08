"""test_workspace_isolation.py — Phase 2 workspace path escape prevention tests."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_ws(job_id: str, tmp_path: Path) -> object:
    """Create a fresh workspace in the pytest tmp_path to avoid /tmp pollution."""
    import os
    import runtime.workspace_manager as wm

    # Override WORKSPACE_ROOT so tests don't write to /tmp on Windows
    original_root = wm.WORKSPACE_ROOT
    wm.WORKSPACE_ROOT = str(tmp_path)
    # Remove any stale registry entry
    wm._registry.pop(job_id, None)
    ws = wm.create_workspace(job_id)
    wm.WORKSPACE_ROOT = original_root
    return ws


# ---------------------------------------------------------------------------
# create_workspace
# ---------------------------------------------------------------------------

class TestCreateWorkspace:
    def test_creates_directory(self, tmp_path):
        import runtime.workspace_manager as wm
        wm._registry.pop("ws-create-01", None)
        original = wm.WORKSPACE_ROOT
        wm.WORKSPACE_ROOT = str(tmp_path)
        try:
            ws = wm.create_workspace("ws-create-01")
            assert ws.path.is_dir(), "create_workspace must create the directory"
        finally:
            wm.WORKSPACE_ROOT = original
            wm._registry.pop("ws-create-01", None)

    def test_session_id_auto_generated(self, tmp_path):
        import runtime.workspace_manager as wm
        wm._registry.pop("ws-session-01", None)
        original = wm.WORKSPACE_ROOT
        wm.WORKSPACE_ROOT = str(tmp_path)
        try:
            ws = wm.create_workspace("ws-session-01")
            assert ws.session_id and len(ws.session_id) > 8
        finally:
            wm.WORKSPACE_ROOT = original
            wm._registry.pop("ws-session-01", None)

    def test_explicit_session_id_preserved(self, tmp_path):
        import runtime.workspace_manager as wm
        wm._registry.pop("ws-session-02", None)
        original = wm.WORKSPACE_ROOT
        wm.WORKSPACE_ROOT = str(tmp_path)
        try:
            ws = wm.create_workspace("ws-session-02", session_id="my-session-xyz")
            assert ws.session_id == "my-session-xyz"
        finally:
            wm.WORKSPACE_ROOT = original
            wm._registry.pop("ws-session-02", None)

    def test_idempotent_on_duplicate_call(self, tmp_path):
        import runtime.workspace_manager as wm
        wm._registry.pop("ws-idem-01", None)
        original = wm.WORKSPACE_ROOT
        wm.WORKSPACE_ROOT = str(tmp_path)
        try:
            ws1 = wm.create_workspace("ws-idem-01", session_id="s1")
            ws2 = wm.create_workspace("ws-idem-01", session_id="s2-ignored")
            assert ws1 is ws2, "Second create_workspace call must return existing workspace"
            assert ws2.session_id == "s1"
        finally:
            wm.WORKSPACE_ROOT = original
            wm._registry.pop("ws-idem-01", None)


# ---------------------------------------------------------------------------
# assert_inside_workspace — escape prevention
# ---------------------------------------------------------------------------

class TestAssertInsideWorkspace:
    def test_valid_subpath_allowed(self, tmp_path):
        import runtime.workspace_manager as wm
        wm._registry.pop("ws-valid-01", None)
        original = wm.WORKSPACE_ROOT
        wm.WORKSPACE_ROOT = str(tmp_path)
        try:
            ws = wm.create_workspace("ws-valid-01")
            subfile = ws.path / "output.txt"
            subfile.touch()
            result = wm.assert_inside_workspace("ws-valid-01", subfile)
            assert result == subfile.resolve()
        finally:
            wm.WORKSPACE_ROOT = original
            wm._registry.pop("ws-valid-01", None)

    def test_workspace_root_itself_allowed(self, tmp_path):
        import runtime.workspace_manager as wm
        wm._registry.pop("ws-root-01", None)
        original = wm.WORKSPACE_ROOT
        wm.WORKSPACE_ROOT = str(tmp_path)
        try:
            ws = wm.create_workspace("ws-root-01")
            # The workspace root itself should be allowed
            result = wm.assert_inside_workspace("ws-root-01", ws.path)
            assert result == ws.path.resolve()
        finally:
            wm.WORKSPACE_ROOT = original
            wm._registry.pop("ws-root-01", None)

    def test_dotdot_escape_blocked(self, tmp_path):
        import runtime.workspace_manager as wm
        wm._registry.pop("ws-escape-01", None)
        original = wm.WORKSPACE_ROOT
        wm.WORKSPACE_ROOT = str(tmp_path)
        try:
            ws = wm.create_workspace("ws-escape-01")
            escape_path = ws.path / ".." / "etc" / "passwd"
            with pytest.raises(PermissionError, match="Path escape"):
                wm.assert_inside_workspace("ws-escape-01", escape_path)
        finally:
            wm.WORKSPACE_ROOT = original
            wm._registry.pop("ws-escape-01", None)

    def test_absolute_escape_blocked(self, tmp_path):
        import runtime.workspace_manager as wm
        wm._registry.pop("ws-abs-01", None)
        original = wm.WORKSPACE_ROOT
        wm.WORKSPACE_ROOT = str(tmp_path)
        try:
            wm.create_workspace("ws-abs-01")
            with pytest.raises(PermissionError, match="Path escape"):
                wm.assert_inside_workspace("ws-abs-01", "/etc/passwd")
        finally:
            wm.WORKSPACE_ROOT = original
            wm._registry.pop("ws-abs-01", None)

    def test_prefix_collision_blocked(self, tmp_path):
        """Ensure /tmp/jobs/myjobbad is not accepted for workspace /tmp/jobs/myjob."""
        import runtime.workspace_manager as wm
        wm._registry.pop("ws-prefix-01", None)
        original = wm.WORKSPACE_ROOT
        wm.WORKSPACE_ROOT = str(tmp_path)
        try:
            ws = wm.create_workspace("ws-prefix-01")
            # Path that starts with workspace path string but is a sibling
            sibling = ws.path.parent / (ws.path.name + "bad")
            sibling.mkdir(exist_ok=True)
            with pytest.raises(PermissionError, match="Path escape"):
                wm.assert_inside_workspace("ws-prefix-01", sibling)
        finally:
            wm.WORKSPACE_ROOT = original
            wm._registry.pop("ws-prefix-01", None)

    def test_no_workspace_raises_value_error(self):
        import runtime.workspace_manager as wm
        with pytest.raises(ValueError, match="No workspace registered"):
            wm.assert_inside_workspace("nonexistent-job-xyz", "/some/path")


# ---------------------------------------------------------------------------
# get_workspace / set_workspace_status
# ---------------------------------------------------------------------------

class TestWorkspaceStatus:
    def test_get_workspace_returns_none_for_unknown(self):
        import runtime.workspace_manager as wm
        assert wm.get_workspace("no-such-job-xyz") is None

    def test_set_status_transitions(self, tmp_path):
        import runtime.workspace_manager as wm
        wm._registry.pop("ws-status-01", None)
        original = wm.WORKSPACE_ROOT
        wm.WORKSPACE_ROOT = str(tmp_path)
        try:
            wm.create_workspace("ws-status-01")
            wm.set_workspace_status("ws-status-01", "ACTIVE")
            assert wm.get_workspace("ws-status-01").status == "ACTIVE"
            wm.set_workspace_status("ws-status-01", "FINALIZING")
            assert wm.get_workspace("ws-status-01").status == "FINALIZING"
        finally:
            wm.WORKSPACE_ROOT = original
            wm._registry.pop("ws-status-01", None)

    def test_invalid_status_raises(self, tmp_path):
        import runtime.workspace_manager as wm
        wm._registry.pop("ws-status-02", None)
        original = wm.WORKSPACE_ROOT
        wm.WORKSPACE_ROOT = str(tmp_path)
        try:
            wm.create_workspace("ws-status-02")
            with pytest.raises(ValueError, match="Invalid workspace status"):
                wm.set_workspace_status("ws-status-02", "UNKNOWN_STATE")
        finally:
            wm.WORKSPACE_ROOT = original
            wm._registry.pop("ws-status-02", None)
