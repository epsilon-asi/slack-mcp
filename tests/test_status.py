"""Tests for the cross-workspace status, presence, and workspace-listing tools."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

import slack_mcp_server as sms


def _profiles(mock):
    """Map alias -> profile payload for every users.profile.set call made."""
    out = {}
    for call in mock.call_args_list:
        url = call.args[0] if call.args else call.kwargs["url"]
        if "users.profile.set" in url:
            out[call.kwargs["account"].alias] = call.kwargs["payload"]["profile"]
    return out


class TestParseStatusExpiration:
    def test_empty_means_never(self):
        assert sms._parse_status_expiration("") == 0

    @pytest.mark.parametrize(
        "value,seconds", [("30m", 1800), ("2h", 7200), ("1d", 86400), ("90 M", 5400)]
    )
    def test_relative_offsets(self, value, seconds):
        now = int(datetime.now(timezone.utc).timestamp())
        assert abs(sms._parse_status_expiration(value) - (now + seconds)) <= 2

    def test_unix_timestamp_passes_through(self):
        assert sms._parse_status_expiration("1761000000") == 1761000000

    def test_iso_date(self):
        expected = int(
            datetime(2026, 10, 21, tzinfo=timezone.utc).timestamp()
        )
        assert sms._parse_status_expiration("2026-10-21") == expected

    def test_nonsense_raises_with_guidance(self):
        with pytest.raises(ValueError) as excinfo:
            sms._parse_status_expiration("next tuesday")
        assert "30m" in str(excinfo.value)


class TestSetStatus:
    @pytest.mark.asyncio
    async def test_fans_out_to_every_workspace_by_default(self, accounts, quiet_log, responder):
        with patch.object(sms, "make_request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = responder({"users.profile.set": {"ok": True}})
            result = await sms.set_status(text="Heads down", emoji=":no_bell:")

        assert set(result) == {"taskrabbit", "epsilon"}
        assert all(r["ok"] for r in result.values())
        assert _profiles(mock_req) == {
            "taskrabbit": {
                "status_text": "Heads down",
                "status_emoji": ":no_bell:",
                "status_expiration": 0,
            },
            "epsilon": {
                "status_text": "Heads down",
                "status_emoji": ":no_bell:",
                "status_expiration": 0,
            },
        }

    @pytest.mark.asyncio
    async def test_emoji_is_normalized_to_colon_form(self, accounts, quiet_log, responder):
        with patch.object(sms, "make_request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = responder()
            await sms.set_status(emoji="coffee", workspace="taskrabbit")
        assert _profiles(mock_req)["taskrabbit"]["status_emoji"] == ":coffee:"

    @pytest.mark.asyncio
    async def test_blank_emoji_stays_blank(self, accounts, quiet_log, responder):
        with patch.object(sms, "make_request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = responder()
            await sms.set_status(text="hi", workspace="taskrabbit")
        assert _profiles(mock_req)["taskrabbit"]["status_emoji"] == ""

    @pytest.mark.asyncio
    async def test_expiration_is_sent_as_a_timestamp(self, accounts, quiet_log, responder):
        with patch.object(sms, "make_request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = responder()
            result = await sms.set_status(text="Lunch", expiration="1h", workspace="taskrabbit")

        now = int(datetime.now(timezone.utc).timestamp())
        sent = _profiles(mock_req)["taskrabbit"]["status_expiration"]
        assert abs(sent - (now + 3600)) <= 2
        assert result["taskrabbit"]["status_expiration"] == sent

    @pytest.mark.asyncio
    async def test_a_single_workspace_can_be_targeted(self, accounts, quiet_log, responder):
        with patch.object(sms, "make_request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = responder()
            result = await sms.set_status(text="hi", workspace="epsilon")
        assert list(result) == ["epsilon"]
        assert list(_profiles(mock_req)) == ["epsilon"]

    @pytest.mark.asyncio
    async def test_one_workspace_failing_leaves_the_other_set(self, accounts, quiet_log, responder):
        def _per_account(account, payload):
            if account.alias == "epsilon":
                return {"ok": False, "error": "token_expired"}
            return {"ok": True}

        with patch.object(sms, "make_request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = responder({"users.profile.set": _per_account})
            result = await sms.set_status(text="Heads down")

        assert result["taskrabbit"]["ok"] is True
        assert result["epsilon"] == {"ok": False, "error": "token_expired"}

    @pytest.mark.asyncio
    async def test_no_response_is_reported_per_workspace(self, accounts, quiet_log, responder):
        with patch.object(sms, "make_request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = responder({"users.profile.set": None})
            result = await sms.set_status(text="hi", workspace="taskrabbit")
        assert result["taskrabbit"]["ok"] is False
        assert "no response" in result["taskrabbit"]["error"]

    @pytest.mark.asyncio
    async def test_bot_token_workspace_is_refused_without_calling_slack(
        self, accounts, quiet_log, responder, monkeypatch
    ):
        accounts["bot"] = sms.SlackAccount(alias="bot", team_id="T333", bot_token="xoxb-1")

        with patch.object(sms, "make_request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = responder()
            result = await sms.set_status(text="hi", workspace="bot")

        assert result["bot"]["ok"] is False
        assert "bot token" in result["bot"]["error"]
        assert _profiles(mock_req) == {}

    @pytest.mark.asyncio
    async def test_unknown_workspace_raises_before_any_call(self, accounts, quiet_log):
        with patch.object(sms, "make_request", new_callable=AsyncMock) as mock_req:
            with pytest.raises(ValueError):
                await sms.set_status(text="hi", workspace="nope")
            mock_req.assert_not_called()

    @pytest.mark.asyncio
    async def test_read_only_mode_refuses(self, accounts, quiet_log, monkeypatch):
        monkeypatch.setenv(sms.READ_ONLY_ENV_VAR, "true")
        with pytest.raises(RuntimeError):
            await sms.set_status(text="hi")

    @pytest.mark.asyncio
    async def test_warns_when_a_custom_emoji_is_missing_in_one_workspace(
        self, accounts, quiet_log, responder
    ):
        def _emoji(account, payload):
            if account.alias == "taskrabbit":
                return {"ok": True, "emoji": {"tr-standup": "https://example.test/x.png"}}
            return {"ok": True, "emoji": {}}

        with patch.object(sms, "make_request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = responder({"emoji.list": _emoji})
            result = await sms.set_status(emoji=":tr-standup:")

        assert "warning" not in result["taskrabbit"]
        assert "render blank" in result["epsilon"]["warning"]

    @pytest.mark.asyncio
    async def test_standard_emoji_produce_no_warning(self, accounts, quiet_log, responder):
        """emoji.list only returns custom emoji, so absence everywhere means standard."""
        with patch.object(sms, "make_request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = responder({"emoji.list": {"ok": True, "emoji": {}}})
            result = await sms.set_status(emoji=":coffee:")

        assert all("warning" not in r for r in result.values())


class TestClearStatus:
    @pytest.mark.asyncio
    async def test_clears_text_emoji_and_expiry_everywhere(self, accounts, quiet_log, responder):
        with patch.object(sms, "make_request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = responder()
            result = await sms.clear_status()

        assert set(result) == {"taskrabbit", "epsilon"}
        for profile in _profiles(mock_req).values():
            assert profile == {"status_text": "", "status_emoji": "", "status_expiration": 0}


class TestGetStatus:
    @pytest.mark.asyncio
    async def test_reads_each_workspace(self, accounts, responder):
        def _get(account, payload):
            return {
                "ok": True,
                "profile": {
                    "status_text": f"busy in {account.alias}",
                    "status_emoji": ":no_bell:",
                    "status_expiration": 0,
                },
            }

        with patch.object(sms, "make_request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = responder({"users.profile.get": _get})
            result = await sms.get_status()

        assert result["taskrabbit"]["status_text"] == "busy in taskrabbit"
        assert result["epsilon"]["status_text"] == "busy in epsilon"

    @pytest.mark.asyncio
    async def test_reports_errors_per_workspace(self, accounts, responder):
        with patch.object(sms, "make_request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = responder(
                {"users.profile.get": {"ok": False, "error": "invalid_auth"}}
            )
            result = await sms.get_status()
        assert result["taskrabbit"] == {"ok": False, "error": "invalid_auth"}


class TestSetPresence:
    @pytest.mark.asyncio
    async def test_sets_presence_everywhere(self, accounts, quiet_log, responder):
        with patch.object(sms, "make_request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = responder()
            result = await sms.set_presence("away")

        assert result == {
            "taskrabbit": {"ok": True, "presence": "away"},
            "epsilon": {"ok": True, "presence": "away"},
        }
        payloads = [
            c.kwargs["payload"]
            for c in mock_req.call_args_list
            if "users.setPresence" in (c.args[0] if c.args else "")
        ]
        assert payloads == [{"presence": "away"}, {"presence": "away"}]

    @pytest.mark.asyncio
    async def test_bot_token_workspace_is_refused(self, accounts, quiet_log, responder):
        accounts.clear()
        accounts["bot"] = sms.SlackAccount(alias="bot", bot_token="xoxb-1")
        with patch.object(sms, "make_request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = responder()
            result = await sms.set_presence("away")
        assert result["bot"]["ok"] is False


class TestListWorkspaces:
    @pytest.mark.asyncio
    async def test_lists_without_calling_slack(self, accounts):
        with patch.object(sms, "make_request", new_callable=AsyncMock) as mock_req:
            rows = await sms.list_workspaces()
            mock_req.assert_not_called()

        by_alias = {r["alias"]: r for r in rows}
        assert by_alias["taskrabbit"]["is_default"] is True
        assert by_alias["epsilon"]["is_default"] is False
        assert by_alias["epsilon"]["can_set_status"] is True

    @pytest.mark.asyncio
    async def test_bot_token_accounts_are_flagged_as_unable_to_set_status(self, accounts):
        accounts["bot"] = sms.SlackAccount(alias="bot", bot_token="xoxb-1")
        rows = await sms.list_workspaces()
        by_alias = {r["alias"]: r for r in rows}
        assert by_alias["bot"]["can_set_status"] is False
        assert by_alias["bot"]["auth"] == "bot_token"

    @pytest.mark.asyncio
    async def test_verify_fills_in_team_and_user(self, accounts, responder):
        def _auth(account, payload):
            return {
                "ok": True,
                "team": f"{account.alias}-team",
                "team_id": account.team_id,
                "user": "chris",
            }

        with patch.object(sms, "make_request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = responder({"auth.test": _auth})
            rows = await sms.list_workspaces(verify=True)

        by_alias = {r["alias"]: r for r in rows}
        assert by_alias["taskrabbit"]["verified"] is True
        assert by_alias["taskrabbit"]["team_name"] == "taskrabbit-team"
        assert by_alias["taskrabbit"]["user"] == "chris"

    @pytest.mark.asyncio
    async def test_verify_reports_a_dead_token(self, accounts, responder):
        with patch.object(sms, "make_request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = responder({"auth.test": {"ok": False, "error": "invalid_auth"}})
            rows = await sms.list_workspaces(verify=True)

        by_alias = {r["alias"]: r for r in rows}
        assert by_alias["epsilon"]["verified"] is False
        assert by_alias["epsilon"]["error"] == "invalid_auth"

    @pytest.mark.asyncio
    async def test_verify_does_not_repoint_an_existing_cache_dir(self, accounts, responder):
        """team_id keys the on-disk cache, so a verified value must not overwrite it."""
        before = accounts["epsilon"].cache_dir

        with patch.object(sms, "make_request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = responder(
                {"auth.test": {"ok": True, "team": "E", "team_id": "T_DIFFERENT", "user": "u"}}
            )
            await sms.list_workspaces(verify=True)

        assert accounts["epsilon"].cache_dir == before
