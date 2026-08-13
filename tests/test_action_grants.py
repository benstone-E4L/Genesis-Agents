import pytest


def test_action_grant_is_signed_expiring_exact_and_single_use(tmp_path):
    from runtime.action_grants import GrantError, consume_action_grant, issue_action_grant

    kwargs = dict(principal_id="entra:u", tenant_id="e4l", tool="github_tool", args={"project": "x"})
    token = issue_action_grant(**kwargs, authorization_id="auth-1", key="g" * 32, now=100, ttl_seconds=10)
    assert consume_action_grant(token, **kwargs, db_path=tmp_path / "auth.db", key="g" * 32, now=105) == "auth-1"
    with pytest.raises(GrantError, match="already_consumed"):
        consume_action_grant(token, **kwargs, db_path=tmp_path / "auth.db", key="g" * 32, now=105)


def test_action_grant_wrong_tool_args_tenant_and_expiry_fail(tmp_path):
    from runtime.action_grants import GrantError, consume_action_grant, issue_action_grant

    base = dict(principal_id="entra:u", tenant_id="e4l", tool="github_tool", args={"project": "x"})
    token = issue_action_grant(**base, authorization_id="auth-1", key="g" * 32, now=100, ttl_seconds=10)
    for changed, error in [
        ({"tool": "workspace_shell"}, "tool"), ({"args": {"project": "y"}}, "args"),
        ({"tenant_id": "other"}, "tenant"), ({"now": 111}, "expired"),
    ]:
        call = {**base, **{k: v for k, v in changed.items() if k != "now"}}
        with pytest.raises(GrantError, match=error):
            consume_action_grant(token, **call, db_path=tmp_path / f"{error}.db", key="g" * 32, now=changed.get("now", 105))
