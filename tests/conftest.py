"""Shared test setup.

Slack credentials and the data directory are resolved at import time in
slack_mcp_server, so they are pinned here — before the module is imported —
to keep tests off the developer's real tokens and cache files.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

_TMP_DATA = tempfile.mkdtemp(prefix="slack-mcp-tests-")
os.environ["SLACK_MCP_DATA"] = _TMP_DATA
os.environ["SLACK_MCP_TOKENS_FILE"] = str(Path(_TMP_DATA) / "tokens.env")
os.environ["SLACK_MCP_WORKSPACES_FILE"] = str(Path(_TMP_DATA) / "workspaces.json")
for _var in (
    "SLACK_BOT_TOKEN",
    "SLACK_XOXC_TOKEN",
    "SLACK_XOXD_TOKEN",
    "SLACK_MCP_XOXC_TOKEN",
    "SLACK_MCP_XOXD_TOKEN",
    "LOGS_CHANNEL_ID",
    "SLACK_TEAM_ID",
    "SLACK_MCP_READ_ONLY",
):
    os.environ.pop(_var, None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import slack_mcp_server as sms  # noqa: E402


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    """Install a two-workspace registry with 'taskrabbit' as the default."""
    monkeypatch.setattr(sms, "DATA_DIR", tmp_path)
    registry = {
        "taskrabbit": sms.SlackAccount(
            alias="taskrabbit",
            team_id="T111",
            team_name="Taskrabbit",
            xoxc_token="xoxc-tr",
            xoxd_token="xoxd-shared",
        ),
        "epsilon": sms.SlackAccount(
            alias="epsilon",
            team_id="T222",
            team_name="Epsilon ASI",
            xoxc_token="xoxc-ep",
            xoxd_token="xoxd-shared",
        ),
    }
    monkeypatch.setattr(sms, "_accounts", registry)
    monkeypatch.setattr(sms, "_default_alias", "taskrabbit")
    return registry


@pytest.fixture
def responder():
    """Build a make_request side effect that dispatches on the URL.

    Keys are matched as substrings of the URL; values are either a response
    dict or a callable taking (account, payload).
    """

    def _make(mapping=None, default=None):
        mapping = mapping or {}
        fallback = {"ok": True} if default is None else default

        async def _respond(url, method="POST", payload=None, account=None):
            for fragment, value in mapping.items():
                if fragment in url:
                    return value(account, payload) if callable(value) else value
            return fallback

        return _respond

    return _make


@pytest.fixture
def quiet_log(monkeypatch):
    """Silence the Slack activity log so tools don't try to post."""
    from unittest.mock import AsyncMock

    mock = AsyncMock()
    monkeypatch.setattr(sms, "log_to_slack", mock)
    return mock
