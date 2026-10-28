"""Tests that every tool acts in the workspace its `workspace` argument names."""

from unittest.mock import AsyncMock, patch

import pytest

import slack_mcp_server as sms

# One minimal call per workspace-aware tool. Kept exhaustive on purpose: a tool
# added without the workspace parameter should fail test_every_tool_is_covered.
CALLS = {
    "get_thread": {"channel_id": "C1", "thread_ts": "1700000000.1"},
    "get_channel_history": {"channel_id": "C1"},
    "get_channel_id_by_name": {"channel_name": "general"},
    "refresh_channel_cache": {},
    "refresh_user_cache": {},
    "resolve_user_id": {"query": "ada"},
    "post_message": {"channel_id": "C1", "message": "hi"},
    "post_command": {"channel_id": "C1", "command": "/here", "text": ""},
    "add_reaction": {"channel_id": "C1", "message_ts": "1700000000.1", "reaction": "wave"},
    "get_reactions": {"channel_id": "C1", "message_ts": "1700000000.1"},
    "whoami": {},
    "join_channel": {"channel_id": "C1"},
    "create_channel": {"name": "new-channel"},
    "invite_users_to_channel": {"channel_id": "C1", "user_ids": ["U1"]},
    "rename_channel": {"channel_id": "C1", "new_name": "renamed"},
    "list_joined_channels": {},
    "clear_usergroup": {"usergroup_id": "S1"},
    "update_usergroup_members": {"usergroup_id": "S1", "user_ids": ["U1"]},
    "send_dm": {"user_id": "U1", "message": "hi"},
    "send_group_dm": {"user_ids": ["U1", "U2"], "message": "hi"},
    "search_messages": {"query": "deploy"},
    "search_channel_messages": {"channel_id": "C1", "query": "deploy"},
    "set_status": {"text": "hi"},
    "clear_status": {},
    "get_status": {},
    "set_presence": {"presence": "away"},
}

# Default to every workspace rather than one; covered in test_status.py.
FAN_OUT_BY_DEFAULT = {"set_status", "clear_status", "get_status", "set_presence"}
# Default to one workspace but also accept "all", since refreshing every cache
# at once is useful and harmless. Covered in test_multi_workspace.py.
FAN_OUT_CAPABLE = {"refresh_channel_cache", "refresh_user_cache"}
# Purely local: clears cached handles without calling Slack.
NO_API_CALL = {"refresh_user_cache"}

# Tools that act in exactly one workspace and reject a multi-workspace selector.
SINGLE = {
    name: kwargs
    for name, kwargs in CALLS.items()
    if name not in FAN_OUT_BY_DEFAULT and name not in FAN_OUT_CAPABLE
}
# Tools whose routing is observable through the Slack calls they make.
ROUTED = {
    name: kwargs
    for name, kwargs in CALLS.items()
    if name not in FAN_OUT_BY_DEFAULT and name not in NO_API_CALL
}


@pytest.fixture
def seen(monkeypatch):
    """Record which account each Slack call was made as."""
    aliases = []

    async def _record(url, method="POST", payload=None, account=None):
        aliases.append((account or sms._current_account()).alias)
        return {"ok": True}

    monkeypatch.setattr(sms, "make_request", AsyncMock(side_effect=_record))
    return aliases


@pytest.mark.asyncio
async def test_fan_out_capable_tools_accept_all(accounts, quiet_log, seen):
    """refresh_* are the exception: acting on every cache at once is harmless."""
    assert await sms.refresh_channel_cache(workspace="all") is True
    assert set(seen) == {"taskrabbit", "epsilon"}
    assert await sms.refresh_user_cache(workspace="all") == 0


@pytest.mark.asyncio
async def test_every_tool_is_covered(accounts):
    """Every registered tool that takes `workspace` appears in CALLS."""
    tools = await sms.mcp.list_tools()
    aware = {
        t.name for t in tools if "workspace" in (t.inputSchema.get("properties") or {})
    }
    assert aware - set(CALLS) == set(), "a workspace-aware tool is missing from CALLS"
    assert set(CALLS) - aware == set(), "CALLS names a tool that is not workspace-aware"


class TestRouting:
    @pytest.mark.parametrize("tool", sorted(ROUTED))
    @pytest.mark.asyncio
    async def test_named_workspace_is_used(self, tool, accounts, quiet_log, seen):
        await getattr(sms, tool)(**ROUTED[tool], workspace="epsilon")
        assert seen, f"{tool} made no Slack call"
        assert set(seen) == {"epsilon"}, f"{tool} acted as {set(seen)}"

    @pytest.mark.parametrize("tool", sorted(ROUTED))
    @pytest.mark.asyncio
    async def test_omitted_workspace_uses_the_default(self, tool, accounts, quiet_log, seen):
        await getattr(sms, tool)(**ROUTED[tool])
        assert set(seen) == {"taskrabbit"}, f"{tool} acted as {set(seen)}"

    @pytest.mark.parametrize("tool", sorted(ROUTED))
    @pytest.mark.asyncio
    async def test_team_id_selects_the_workspace(self, tool, accounts, quiet_log, seen):
        await getattr(sms, tool)(**ROUTED[tool], workspace="T222")
        assert set(seen) == {"epsilon"}

    @pytest.mark.parametrize("tool", sorted(ROUTED))
    @pytest.mark.asyncio
    async def test_unknown_workspace_is_rejected(self, tool, accounts, quiet_log, seen):
        with pytest.raises(ValueError, match="Unknown workspace"):
            await getattr(sms, tool)(**ROUTED[tool], workspace="nope")
        assert seen == [], f"{tool} called Slack before validating the workspace"

    @pytest.mark.parametrize("tool", sorted(SINGLE))
    @pytest.mark.asyncio
    async def test_multiple_workspaces_are_rejected(self, tool, accounts, quiet_log, seen):
        """These tools act in one workspace; "all" would be ambiguous, not helpful."""
        with pytest.raises(ValueError, match="one workspace at a time"):
            await getattr(sms, tool)(**SINGLE[tool], workspace="all")
        assert seen == []

    @pytest.mark.asyncio
    async def test_the_error_names_the_offending_workspaces(self, accounts, quiet_log):
        with pytest.raises(ValueError) as excinfo:
            await sms.post_message("C1", "hi", workspace="taskrabbit,epsilon")
        message = str(excinfo.value)
        assert "taskrabbit" in message and "epsilon" in message


class TestContext:
    @pytest.mark.asyncio
    async def test_context_is_restored_after_a_call(self, accounts, quiet_log, seen):
        await sms.whoami(workspace="epsilon")
        assert sms._active_account.get() is None
        assert sms._current_account().alias == "taskrabbit"

    @pytest.mark.asyncio
    async def test_context_is_restored_after_a_failure(self, accounts, quiet_log, seen):
        with pytest.raises(ValueError):
            await sms.whoami(workspace="nope")
        assert sms._active_account.get() is None

    @pytest.mark.asyncio
    async def test_a_nested_call_stays_in_the_active_workspace(self, accounts, seen):
        """log_to_slack posts via post_message; during fan-out that nested call
        must not fall back to the default workspace."""
        accounts["epsilon"].logs_channel_id = "C_EP_LOGS"

        await sms._fan_out(
            [accounts["epsilon"]], lambda account: sms.log_to_slack("working", account=account)
        )

        assert seen, "the activity log made no Slack call"
        assert set(seen) == {"epsilon"}, "the activity log was posted to the wrong workspace"

    @pytest.mark.asyncio
    async def test_positional_calls_still_work(self, accounts, quiet_log, seen):
        """The decorator must not break existing callers."""
        await sms.post_message("C1", "hi")
        assert set(seen) == {"taskrabbit"}

    @pytest.mark.asyncio
    async def test_resolve_one_prefers_the_active_account(self, accounts):
        with sms._using_account(accounts["epsilon"]):
            assert sms.resolve_one("").alias == "epsilon"
            assert sms.resolve_one("taskrabbit").alias == "taskrabbit"
        assert sms.resolve_one("").alias == "taskrabbit"


class TestCachesStaySeparate:
    @pytest.mark.asyncio
    async def test_channel_created_in_one_workspace_is_not_cached_in_another(
        self, accounts, quiet_log
    ):
        async def _create(url, method="POST", payload=None, account=None):
            return {"ok": True, "channel": {"id": "C_NEW", "name": "new-channel"}}

        with patch.object(sms, "make_request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = _create
            await sms.create_channel("new-channel", workspace="epsilon")

        assert accounts["epsilon"].channel_cache == {"new-channel": "C_NEW"}
        assert accounts["taskrabbit"].channel_cache == {}

    @pytest.mark.asyncio
    async def test_create_channel_sends_the_right_team_id(self, accounts, quiet_log):
        captured = {}

        async def _create(url, method="POST", payload=None, account=None):
            if "conversations.create" in url:
                captured["team_id"] = (payload or {}).get("team_id")
            return {"ok": True, "channel": {"id": "C_NEW", "name": "new-channel"}}

        with patch.object(sms, "make_request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = _create
            await sms.create_channel("new-channel", workspace="epsilon")

        assert captured["team_id"] == "T222"
