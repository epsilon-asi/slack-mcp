"""Tests for the account registry, workspace selection, and per-account caches."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

import slack_mcp_server as sms


class TestResolveAccounts:
    def test_empty_selector_is_the_default_account(self, accounts):
        assert [a.alias for a in sms.resolve_accounts("")] == ["taskrabbit"]

    @pytest.mark.parametrize("selector", ["all", "ALL", "*"])
    def test_all_selects_every_account(self, accounts, selector):
        assert {a.alias for a in sms.resolve_accounts(selector)} == {"taskrabbit", "epsilon"}

    def test_selects_by_alias_case_insensitively(self, accounts):
        assert [a.alias for a in sms.resolve_accounts("EPSILON")] == ["epsilon"]

    def test_selects_by_team_id(self, accounts):
        assert [a.alias for a in sms.resolve_accounts("T222")] == ["epsilon"]

    def test_selects_by_team_name(self, accounts):
        assert [a.alias for a in sms.resolve_accounts("epsilon asi")] == ["epsilon"]

    def test_comma_separated_list_preserves_order(self, accounts):
        assert [a.alias for a in sms.resolve_accounts("epsilon, taskrabbit")] == [
            "epsilon",
            "taskrabbit",
        ]

    def test_duplicates_are_collapsed(self, accounts):
        assert [a.alias for a in sms.resolve_accounts("epsilon,T222,epsilon")] == ["epsilon"]

    def test_unknown_workspace_names_what_is_configured(self, accounts):
        with pytest.raises(ValueError) as excinfo:
            sms.resolve_accounts("nope")
        message = str(excinfo.value)
        assert "nope" in message
        assert "taskrabbit" in message

    def test_one_unknown_rejects_the_whole_selector(self, accounts):
        """A partial fan-out would silently skip a workspace the caller asked for."""
        with pytest.raises(ValueError):
            sms.resolve_accounts("taskrabbit,nope")


class TestAccountCaches:
    def test_cache_dir_is_keyed_on_team_id(self, accounts, tmp_path):
        assert accounts["epsilon"].cache_dir == tmp_path / "accounts" / "T222"

    def test_cache_dir_falls_back_to_alias_without_a_team_id(self, accounts, tmp_path):
        account = sms.SlackAccount(alias="solo", xoxc_token="x", xoxd_token="d")
        assert account.cache_dir == tmp_path / "accounts" / "solo"

    def test_channel_caches_are_independent(self, accounts):
        accounts["taskrabbit"].channel_cache["general"] = "C_TR"
        accounts["epsilon"].channel_cache["general"] = "C_EP"
        assert accounts["taskrabbit"].channel_cache["general"] == "C_TR"
        assert accounts["epsilon"].channel_cache["general"] == "C_EP"

    @pytest.mark.asyncio
    async def test_channel_lookup_uses_the_active_account(self, accounts, quiet_log):
        """The same channel name resolves differently per workspace."""
        accounts["taskrabbit"].channel_cache["general"] = "C_TR"
        accounts["epsilon"].channel_cache["general"] = "C_EP"

        with patch.object(sms, "make_request", new_callable=AsyncMock) as mock_req:
            assert await sms.get_channel_id_by_name("#general") == "C_TR"
            with sms._using_account(accounts["epsilon"]):
                assert await sms.get_channel_id_by_name("#general") == "C_EP"
            mock_req.assert_not_called()

    @pytest.mark.asyncio
    async def test_user_cache_is_saved_under_the_active_account(self, accounts):
        account = accounts["epsilon"]
        account.user_cache_loaded = True
        with patch.object(sms, "make_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"ok": True, "user": {"name": "ada", "profile": {}}}
            with sms._using_account(account):
                assert await sms.get_user_handle("U1") == "ada"

        assert account.user_cache == {"U1": "ada"}
        assert accounts["taskrabbit"].user_cache == {}
        assert json.loads(account.user_cache_file.read_text()) == {"U1": "ada"}

    @pytest.mark.asyncio
    async def test_refresh_user_cache_clears_only_the_named_workspace(self, accounts, quiet_log):
        accounts["taskrabbit"].user_cache["U1"] = "ada"
        accounts["epsilon"].user_cache["U2"] = "grace"

        assert await sms.refresh_user_cache("epsilon") == 1
        assert accounts["taskrabbit"].user_cache == {"U1": "ada"}
        assert accounts["epsilon"].user_cache == {}

    @pytest.mark.asyncio
    async def test_refresh_user_cache_all_clears_everything(self, accounts, quiet_log):
        accounts["taskrabbit"].user_cache["U1"] = "ada"
        accounts["epsilon"].user_cache["U2"] = "grace"

        assert await sms.refresh_user_cache("all") == 2
        assert accounts["taskrabbit"].user_cache == {}
        assert accounts["epsilon"].user_cache == {}


class TestFanOut:
    @pytest.mark.asyncio
    async def test_results_are_keyed_by_alias(self, accounts):
        async def _fn(account):
            return {"ok": True, "alias": account.alias}

        result = await sms._fan_out(list(accounts.values()), _fn)
        assert result == {
            "taskrabbit": {"ok": True, "alias": "taskrabbit"},
            "epsilon": {"ok": True, "alias": "epsilon"},
        }

    @pytest.mark.asyncio
    async def test_one_failure_does_not_sink_the_others(self, accounts):
        async def _fn(account):
            if account.alias == "epsilon":
                raise RuntimeError("token expired")
            return {"ok": True}

        result = await sms._fan_out(list(accounts.values()), _fn)
        assert result["taskrabbit"] == {"ok": True}
        assert result["epsilon"]["ok"] is False
        assert "token expired" in result["epsilon"]["error"]

    @pytest.mark.asyncio
    async def test_active_account_does_not_leak_between_tasks(self, accounts):
        """Each gathered task gets its own context copy, so interleaving is safe."""
        seen = {}

        async def _fn(account):
            # Yield control so the tasks interleave mid-flight.
            await asyncio.sleep(0)
            seen[account.alias] = sms._current_account().alias
            await asyncio.sleep(0)
            seen[account.alias] = sms._current_account().alias
            return {"ok": True}

        await sms._fan_out(list(accounts.values()), _fn)
        assert seen == {"taskrabbit": "taskrabbit", "epsilon": "epsilon"}

    @pytest.mark.asyncio
    async def test_context_is_restored_afterwards(self, accounts):
        async def _fn(account):
            return {"ok": True}

        await sms._fan_out(list(accounts.values()), _fn)
        assert sms._active_account.get() is None
        assert sms._current_account().alias == "taskrabbit"


class TestLoadAccounts:
    def _write(self, monkeypatch, tmp_path, config):
        path = tmp_path / "workspaces.json"
        path.write_text(json.dumps(config))
        monkeypatch.setattr(sms, "WORKSPACES_FILE", path)
        monkeypatch.setattr(sms, "DATA_DIR", tmp_path)
        return path

    def test_shared_xoxd_applies_to_every_account(self, monkeypatch, tmp_path):
        self._write(
            monkeypatch,
            tmp_path,
            {
                "xoxd": "xoxd-shared",
                "default": "two",
                "accounts": {
                    "one": {"team_id": "T1", "xoxc": "xoxc-1"},
                    "two": {"team_id": "T2", "xoxc": "xoxc-2"},
                },
            },
        )
        sms._load_accounts()
        assert sms._default_alias == "two"
        assert sms._accounts["one"].xoxd_token == "xoxd-shared"
        assert sms._accounts["two"].xoxd_token == "xoxd-shared"

    def test_per_account_xoxd_overrides_the_shared_one(self, monkeypatch, tmp_path):
        self._write(
            monkeypatch,
            tmp_path,
            {
                "xoxd": "xoxd-shared",
                "accounts": {"one": {"xoxc": "xoxc-1", "xoxd": "xoxd-own"}},
            },
        )
        sms._load_accounts()
        assert sms._accounts["one"].xoxd_token == "xoxd-own"

    def test_accounts_without_tokens_are_skipped(self, monkeypatch, tmp_path):
        self._write(
            monkeypatch,
            tmp_path,
            {
                "accounts": {
                    "good": {"xoxc": "xoxc-1", "xoxd": "xoxd-1"},
                    "no_cookie": {"xoxc": "xoxc-2"},
                    "malformed": "not-a-dict",
                }
            },
        )
        sms._load_accounts()
        assert list(sms._accounts) == ["good"]

    def test_unknown_default_falls_back_to_the_first_account(self, monkeypatch, tmp_path):
        self._write(
            monkeypatch,
            tmp_path,
            {
                "default": "missing",
                "accounts": {"one": {"xoxc": "x", "xoxd": "d"}},
            },
        )
        sms._load_accounts()
        assert sms._default_alias == "one"

    def test_bot_token_accounts_are_accepted(self, monkeypatch, tmp_path):
        self._write(monkeypatch, tmp_path, {"accounts": {"bot": {"bot_token": "xoxb-1"}}})
        sms._load_accounts()
        assert sms._accounts["bot"].uses_bot_token is True

    def test_falls_back_to_env_when_the_file_is_absent(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sms, "WORKSPACES_FILE", tmp_path / "does-not-exist.json")
        monkeypatch.setattr(sms, "DATA_DIR", tmp_path)
        monkeypatch.setattr(sms, "SLACK_BOT_TOKEN", "xoxb-env")
        monkeypatch.setattr(sms, "LOGS_CHANNEL_ID", "C_LOGS")
        monkeypatch.setattr(sms, "SLACK_TEAM_ID", "T_ENV")

        sms._load_accounts()
        assert list(sms._accounts) == [sms.DEFAULT_ACCOUNT_ALIAS]
        account = sms._accounts[sms.DEFAULT_ACCOUNT_ALIAS]
        assert account.bot_token == "xoxb-env"
        assert account.logs_channel_id == "C_LOGS"
        assert account.team_id == "T_ENV"

    def test_malformed_json_falls_back_instead_of_crashing(self, monkeypatch, tmp_path):
        path = tmp_path / "workspaces.json"
        path.write_text("{not json")
        monkeypatch.setattr(sms, "WORKSPACES_FILE", path)
        monkeypatch.setattr(sms, "DATA_DIR", tmp_path)
        monkeypatch.setattr(sms, "SLACK_BOT_TOKEN", "xoxb-env")

        sms._load_accounts()
        assert list(sms._accounts) == [sms.DEFAULT_ACCOUNT_ALIAS]


class TestAccountAuth:
    def test_user_token_sends_the_d_cookie(self, accounts):
        headers, cookies = accounts["taskrabbit"].auth()
        assert headers["Authorization"] == "Bearer xoxc-tr"
        assert cookies == {"d": "xoxd-shared"}

    def test_bot_token_sends_no_cookie(self):
        account = sms.SlackAccount(alias="bot", bot_token="xoxb-1")
        headers, cookies = account.auth()
        assert headers["Authorization"] == "Bearer xoxb-1"
        assert cookies == {}

    def test_account_without_tokens_has_no_auth(self):
        assert sms.SlackAccount(alias="empty").auth() is None

    @pytest.mark.asyncio
    async def test_make_request_uses_the_named_account(self, accounts):
        """The explicit account argument wins over the active one."""
        captured = {}

        class _Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"ok": True}

        class _Client:
            def __init__(self, cookies=None):
                captured["cookies"] = cookies

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def request(self, method, url, headers=None, **kwargs):
                captured["headers"] = headers
                return _Response()

        with patch.object(sms.httpx, "AsyncClient", _Client):
            await sms.make_request(
                "https://slack.com/api/auth.test", account=accounts["epsilon"]
            )

        assert captured["headers"]["Authorization"] == "Bearer xoxc-ep"
        assert captured["cookies"] == {"d": "xoxd-shared"}
