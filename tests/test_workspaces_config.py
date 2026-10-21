"""Tests for workspaces.json generation, in both token extraction scripts.

setup-slack-mcp.py is published for `python3 <(curl ...)`, so it carries its own
copy of the merge logic rather than importing scripts/slack_workspaces.py. The
parity tests at the bottom are what keep the two from drifting apart.
"""

import importlib.util
import json
import stat
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import slack_workspaces as sw  # noqa: E402


def _load_setup_script():
    spec = importlib.util.spec_from_file_location("setup_slack_mcp", SCRIPTS / "setup-slack-mcp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup = _load_setup_script()


def _local_config(teams: dict) -> bytes:
    """A storage blob shaped like Chromium's localStorage entry."""
    return b"\x01_https://app.slack.com\x00\x01localConfig_v2" + json.dumps(
        {"teams": teams, "lastActiveTeam": next(iter(teams), "")}
    ).encode()


TEAMS = {
    "T111": {"id": "T111AAAAAA", "name": "Taskrabbit", "domain": "taskrabbit", "token": "xoxc-tr"},
    "T222": {"id": "T222BBBBBB", "name": "Epsilon ASI", "domain": "epsilon-asi", "token": "xoxc-ep"},
}


class TestExtractLocalConfig:
    def test_reads_every_team(self):
        config = sw.extract_local_config(_local_config(TEAMS))
        assert sorted(config["teams"]) == ["T111", "T222"]

    def test_reads_utf16_encoded_storage(self):
        """Both the key and value stored as UTF-16LE."""
        blob = b"\x00" + ("localConfig_v2" + json.dumps({"teams": TEAMS})).encode("utf-16-le")
        config = sw.extract_local_config(blob)
        assert config is not None
        assert sorted(config["teams"]) == ["T111", "T222"]

    def test_reads_a_utf16_value_under_a_single_byte_key(self):
        """Chromium's real layout: the key is single-byte, the value UTF-16LE.

        The key anchor and the JSON then decode cleanly in different candidates,
        so the parser has to fall back to anchoring on the payload itself.
        """
        blob = (
            b"\x01_https://app.slack.com\x00\x01localConfig_v2"
            + b"\x00"
            + json.dumps({"teams": TEAMS}).encode("utf-16-le")
        )
        config = sw.extract_local_config(blob)
        assert config is not None
        assert sorted(config["teams"]) == ["T111", "T222"]

    def test_returns_none_when_truncated(self):
        truncated = _local_config(TEAMS)[:-40]
        assert sw.extract_local_config(truncated) is None

    def test_recovers_a_bare_teams_object_without_the_key(self):
        """The key anchor is preferred, not required — a UTF-16 value can leave
        the key in a different decoding than its JSON."""
        config = sw.extract_local_config(b'{"teams": {"T1": {"token": "xoxc-x"}}}')
        assert config is not None
        assert list(config["teams"]) == ["T1"]

    def test_returns_none_for_unrelated_json(self):
        assert sw.extract_local_config(b'{"settings": {"theme": "dark"}}') is None

    def test_ignores_a_config_with_no_teams(self):
        assert sw.extract_local_config(b"localConfig_v2" + b'{"teams":{}}') is None

    def test_tolerates_braces_inside_strings(self):
        teams = dict(TEAMS)
        teams["T333"] = {"id": "T333CCCCCC", "name": 'Odd } "quoted" {name', "token": "xoxc-odd"}
        config = sw.extract_local_config(_local_config(teams))
        assert sorted(config["teams"]) == ["T111", "T222", "T333"]


class TestProximityFallback:
    def test_recovers_teams_from_a_truncated_blob(self):
        truncated = _local_config(TEAMS)[:-40]
        teams = sw.find_teams_by_proximity(truncated)
        assert "T111AAAAAA" in teams
        assert teams["T111AAAAAA"]["token"] == "xoxc-tr"

    def test_picks_up_name_and_domain_when_nearby(self):
        teams = sw.find_teams_by_proximity(_local_config(TEAMS))
        assert teams["T222BBBBBB"]["domain"] == "epsilon-asi"

    def test_returns_nothing_without_a_team_id(self):
        assert sw.find_teams_by_proximity(b'{"token":"xoxc-orphan"}') == {}


class TestAccountsFromTeams:
    def test_skips_teams_with_no_token(self):
        teams = {"T1": {"id": "T111AAAAAA", "token": "xoxc-a"}, "T2": {"id": "T222BBBBBB"}}
        assert [a["team_id"] for a in sw.accounts_from_teams(teams)] == ["T111AAAAAA"]

    def test_skips_non_xoxc_tokens(self):
        teams = {"T1": {"id": "T111AAAAAA", "token": "xoxb-bot"}}
        assert sw.accounts_from_teams(teams) == []

    def test_skips_malformed_team_ids(self):
        teams = {"junk": {"id": "not-a-team", "token": "xoxc-a"}}
        assert sw.accounts_from_teams(teams) == []

    def test_falls_back_to_the_map_key_for_the_id(self):
        teams = {"T111AAAAAA": {"token": "xoxc-a"}}
        assert sw.accounts_from_teams(teams)[0]["team_id"] == "T111AAAAAA"

    def test_handles_an_empty_map(self):
        assert sw.accounts_from_teams(None) == []
        assert sw.accounts_from_teams({}) == []


class TestAliasFor:
    def test_prefers_the_domain(self):
        assert sw.alias_for({"domain": "acme", "name": "Acme Inc"}, set()) == "acme"

    def test_falls_back_to_a_sanitised_name(self):
        assert sw.alias_for({"team_name": "Epsilon ASI"}, set()) == "epsilon-asi"

    def test_falls_back_to_the_team_id(self):
        assert sw.alias_for({"team_id": "T111AAAAAA"}, set()) == "t111aaaaaa"

    def test_deduplicates(self):
        assert sw.alias_for({"domain": "acme"}, {"acme"}) == "acme-2"
        assert sw.alias_for({"domain": "acme"}, {"acme", "acme-2"}) == "acme-3"


def _accounts():
    return [
        {"team_id": "T111AAAAAA", "team_name": "Taskrabbit", "domain": "taskrabbit", "xoxc": "xoxc-tr"},
        {"team_id": "T222BBBBBB", "team_name": "Epsilon ASI", "domain": "epsilon-asi", "xoxc": "xoxc-ep"},
    ]


@pytest.fixture(params=["module", "setup-script"])
def merge(request):
    """Both merge implementations, so every behaviour below is checked twice."""
    if request.param == "module":
        return lambda existing, accounts, xoxd, default=None: sw.merge_workspaces(
            existing, accounts, xoxd, default
        )
    return lambda existing, accounts, xoxd, default=None: setup.merge_workspaces_config(
        existing, accounts, xoxd, default or ""
    )


class TestMerge:
    def test_builds_a_config_from_scratch(self, merge):
        config = merge({}, _accounts(), "xoxd-shared")
        assert config["xoxd"] == "xoxd-shared"
        assert sorted(config["accounts"]) == ["epsilon-asi", "taskrabbit"]
        assert config["accounts"]["taskrabbit"]["xoxc"] == "xoxc-tr"
        assert config["default"] in config["accounts"]

    def test_keys_are_ordered_for_a_readable_file(self, merge):
        config = merge({}, _accounts(), "xoxd-shared")
        assert list(config)[:3] == ["default", "xoxd", "accounts"]

    def test_rerun_updates_tokens_without_renaming(self, merge):
        existing = {
            "default": "work",
            "accounts": {"work": {"team_id": "T111AAAAAA", "xoxc": "xoxc-old", "logs_channel": "C1"}},
        }
        config = merge(existing, _accounts(), "xoxd-new")
        assert "taskrabbit" not in config["accounts"]
        assert config["accounts"]["work"]["xoxc"] == "xoxc-tr"
        assert config["accounts"]["work"]["logs_channel"] == "C1"
        assert config["default"] == "work"

    def test_preserves_accounts_the_extractor_never_saw(self, merge):
        """A bot-token account has no xoxc in local storage to find."""
        existing = {"accounts": {"ci": {"team_id": "T999ZZZZZZ", "bot_token": "xoxb-1"}}}
        config = merge(existing, _accounts(), "xoxd-shared")
        assert config["accounts"]["ci"]["bot_token"] == "xoxb-1"
        assert len(config["accounts"]) == 3

    def test_per_account_xoxd_is_removed_in_favour_of_the_shared_one(self, merge):
        existing = {
            "accounts": {"work": {"team_id": "T111AAAAAA", "xoxc": "old", "xoxd": "xoxd-stale"}}
        }
        config = merge(existing, _accounts(), "xoxd-new")
        assert "xoxd" not in config["accounts"]["work"]
        assert config["xoxd"] == "xoxd-new"

    def test_an_explicit_default_is_honoured(self, merge):
        config = merge({}, _accounts(), "xoxd-shared", "epsilon-asi")
        assert config["default"] == "epsilon-asi"

    def test_an_unknown_default_falls_back(self, merge):
        config = merge({"default": "gone"}, _accounts(), "xoxd-shared")
        assert config["default"] in config["accounts"]

    def test_an_empty_xoxd_leaves_the_existing_one_alone(self, merge):
        config = merge({"xoxd": "xoxd-keep", "accounts": {}}, [], "")
        assert config["xoxd"] == "xoxd-keep"

    def test_team_name_is_recorded(self, merge):
        config = merge({}, _accounts(), "xoxd-shared")
        assert config["accounts"]["epsilon-asi"]["team_name"] == "Epsilon ASI"


class TestMergeParity:
    """The two implementations must agree, or workspaces.json depends on which
    script last wrote it."""

    @pytest.mark.parametrize(
        "existing",
        [
            {},
            {"default": "work", "accounts": {"work": {"team_id": "T111AAAAAA", "xoxc": "old"}}},
            {"accounts": {"ci": {"team_id": "T999ZZZZZZ", "bot_token": "xoxb-1"}}},
            {"xoxd": "xoxd-old", "default": "missing", "accounts": {}},
        ],
    )
    def test_same_output_for_the_same_input(self, existing):
        from copy import deepcopy

        a = sw.merge_workspaces(deepcopy(existing), _accounts(), "xoxd-shared", None)
        b = setup.merge_workspaces_config(deepcopy(existing), _accounts(), "xoxd-shared", "")
        assert a == b

    def test_aliases_agree(self):
        team = {"team_id": "T111AAAAAA", "team_name": "Epsilon ASI", "domain": ""}
        assert sw.alias_for(team, set()) == setup.alias_for(team, set())


class TestWriters:
    def test_workspaces_file_is_owner_only(self, tmp_path):
        path = tmp_path / "workspaces.json"
        sw.write_workspaces(path, {"default": "a", "accounts": {}})
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert json.loads(path.read_text())["default"] == "a"

    def test_setup_writer_is_owner_only(self, tmp_path):
        path = tmp_path / "workspaces.json"
        setup.write_private(path, json.dumps({"default": "a"}))
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_tokens_env_is_owner_only_and_parsable(self, tmp_path):
        path = tmp_path / "tokens.env"
        sw.write_tokens_env(path, "xoxc-a", "xoxd-b", "T111AAAAAA", "test")
        text = path.read_text()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert "SLACK_MCP_XOXC_TOKEN=xoxc-a" in text
        assert "SLACK_MCP_XOXD_TOKEN=xoxd-b" in text
        assert "# Team ID: T111AAAAAA" in text

    def test_write_does_not_leave_a_temp_file_behind(self, tmp_path):
        sw.write_workspaces(tmp_path / "workspaces.json", {"accounts": {}})
        assert [p.name for p in tmp_path.iterdir()] == ["workspaces.json"]

    def test_read_workspaces_tolerates_junk(self, tmp_path):
        path = tmp_path / "workspaces.json"
        path.write_text("{not json")
        assert sw.read_workspaces(path) == {}
        assert setup.read_workspaces(path) == {}

    def test_read_workspaces_rejects_a_non_object(self, tmp_path):
        path = tmp_path / "workspaces.json"
        path.write_text("[1, 2]")
        assert sw.read_workspaces(path) == {}
        assert setup.read_workspaces(path) == {}


class TestRoundTrip:
    def test_blob_to_config(self, tmp_path):
        """The whole desktop path: storage blob in, workspaces.json out."""
        config = sw.extract_local_config(_local_config(TEAMS))
        accounts = sw.accounts_from_teams(config["teams"])
        merged = sw.merge_workspaces({}, accounts, "xoxd-shared")
        path = tmp_path / "workspaces.json"
        sw.write_workspaces(path, merged)

        written = json.loads(path.read_text())
        assert written["xoxd"] == "xoxd-shared"
        assert written["accounts"]["taskrabbit"]["team_id"] == "T111AAAAAA"
        assert written["accounts"]["epsilon-asi"]["xoxc"] == "xoxc-ep"
