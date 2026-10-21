import argparse
import contextvars
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Literal
import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings
from mcp.types import ToolAnnotations
import re
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path

__version__ = "1.0.0"


def log(msg: str) -> None:
    """Write diagnostic output to stderr (stdout is reserved for JSON-RPC in stdio mode)."""
    print(msg, file=sys.stderr, flush=True)


READ_ONLY_ENV_VAR = "SLACK_MCP_READ_ONLY"


def _is_read_only() -> bool:
    """True when operators want browse/search only — no Slack state changes."""
    v = os.environ.get(READ_ONLY_ENV_VAR, "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _deny_if_read_only() -> None:
    if _is_read_only():
        raise RuntimeError(
            f"This server is running in read-only mode ({READ_ONLY_ENV_VAR}). "
            "Mutating Slack operations (post message, DM, reactions, commands, join channel) are disabled."
        )


# Parse CLI args before tool registration so --read-only can exclude write tools,
# and so --help/--version print and exit before the token checks in main().
_parser = argparse.ArgumentParser(
    prog="slack-mcp",
    description="MCP server for Slack (stdio by default). Configured via environment "
                "variables; Slack session tokens are also read from a tokens.env file, "
                "and multiple workspaces from a workspaces.json file.",
    epilog="Key env vars: SLACK_BOT_TOKEN OR SLACK_XOXC_TOKEN+SLACK_XOXD_TOKEN; "
           "MCP_TRANSPORT (default stdio), LOGS_CHANNEL_ID, SLACK_TEAM_ID, "
           "SLACK_MCP_DATA (default ~/.local/share/slack-mcp), "
           "SLACK_MCP_TOKENS_FILE (default <data>/tokens.env), "
           "SLACK_MCP_WORKSPACES_FILE (default <data>/workspaces.json, for "
           "multiple workspaces), SLACK_MCP_READ_ONLY. "
           "See the README for the full list.",
)
_parser.add_argument(
    "--read-only",
    action="store_true",
    help="Disable all mutating Slack tools (same as SLACK_MCP_READ_ONLY=true).",
)
_parser.add_argument("--version", action="version", version=f"slack-mcp {__version__}")
_args, _ = _parser.parse_known_args()  # ignore extra args an MCP client may pass through
if _args.read_only:
    os.environ[READ_ONLY_ENV_VAR] = "true"


def _register_tool(annotations):
    if annotations.readOnlyHint is False and _is_read_only():
        return lambda f: f
    return mcp.tool(annotations=annotations)


SLACK_API_BASE = "https://slack.com/api"

# Data directory: user-handle cache and, by default, tokens.env. Uses the XDG
# data location so it lines up with the token files written by
# scripts/slack-refresh-tokens and sourced by the container wrapper. Override
# with SLACK_MCP_DATA; container mode can mount a volume there.
DATA_DIR = Path(os.environ.get("SLACK_MCP_DATA", str(Path.home() / ".local" / "share" / "slack-mcp")))
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass
REVERSE_CACHE_TTL_HOURS = 24

# Per-workspace tokens and settings. When this file is absent the server falls
# back to the single-workspace environment variables, which is how it behaved
# before multi-workspace support.
WORKSPACES_FILE = Path(os.environ.get("SLACK_MCP_WORKSPACES_FILE", str(DATA_DIR / "workspaces.json")))
DEFAULT_ACCOUNT_ALIAS = "default"

# Slack session tokens may also come from a dotenv-style file (default
# <data>/tokens.env — the file scripts/slack-refresh-tokens writes) so the
# `slack-mcp` command works without the caller exporting them. Loaded before the
# token globals below so a SLACK_BOT_TOKEN placed in the file also takes effect.
TOKENS_FILE = Path(os.environ.get("SLACK_MCP_TOKENS_FILE", str(DATA_DIR / "tokens.env")))


def _load_tokens_file(path: Path) -> None:
    """Populate missing token env vars from a dotenv-style file (bare KEY=VALUE,
    '#' comments, blank lines). tokens.env uses SLACK_MCP_XOXC_TOKEN /
    SLACK_MCP_XOXD_TOKEN; map those onto the SLACK_XOXC_TOKEN / SLACK_XOXD_TOKEN
    the server reads. Real environment variables take precedence."""
    aliases = {
        "SLACK_MCP_XOXC_TOKEN": "SLACK_XOXC_TOKEN",
        "SLACK_MCP_XOXD_TOKEN": "SLACK_XOXD_TOKEN",
    }
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    loaded = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")  # split on first '='; values may contain '='
        target = aliases.get(key.strip(), key.strip())
        if target not in os.environ:  # real env wins
            os.environ[target] = value.strip().strip('"').strip("'")
            loaded += 1
    if loaded:
        log(f"slack-mcp: loaded {loaded} token(s) from {path}")


_load_tokens_file(TOKENS_FILE)

MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
LOGS_CHANNEL_ID = os.environ.get("LOGS_CHANNEL_ID", "")
OUTPUT_FORMAT = os.environ.get("OUTPUT_FORMAT", "compact").lower()
# Required for Slack Enterprise Grid: workspace team ID (e.g. T027XXXXX).
# Without this, conversations.create returns cannot_create_channel on org-level installs.
SLACK_TEAM_ID = os.environ.get("SLACK_TEAM_ID", "")

mcp = FastMCP("slack")


def _get_session_tokens_from_env() -> tuple[str, str] | None:
    """Return (xoxc, xoxd) from environment if both are set, else None.

    Accepts either SLACK_XOXC_TOKEN / SLACK_XOXD_TOKEN or their
    SLACK_MCP_* variants (the token file written by slack-refresh-tokens).
    """
    xoxc = os.environ.get("SLACK_XOXC_TOKEN") or os.environ.get("SLACK_MCP_XOXC_TOKEN", "")
    xoxd = os.environ.get("SLACK_XOXD_TOKEN") or os.environ.get("SLACK_MCP_XOXD_TOKEN", "")
    if xoxc and xoxd:
        return xoxc, xoxd
    return None


@dataclass
class SlackAccount:
    """One Slack workspace the server can act in, with its own tokens and caches.

    Channel names and user IDs are workspace-scoped, so each account keeps its
    own caches — sharing them across workspaces resolves #general to whichever
    workspace was cached first, silently.
    """

    alias: str
    team_id: str = ""
    team_name: str = ""
    bot_token: str = ""
    xoxc_token: str = ""
    xoxd_token: str = ""
    logs_channel_id: str = ""
    channel_cache: dict[str, str] = field(default_factory=dict)
    user_cache: dict[str, str] = field(default_factory=dict)
    user_cache_loaded: bool = False
    custom_emoji: set[str] | None = None

    @property
    def uses_bot_token(self) -> bool:
        return bool(self.bot_token)

    @property
    def has_tokens(self) -> bool:
        return bool(self.bot_token or (self.xoxc_token and self.xoxd_token))

    @property
    def label(self) -> str:
        detail = self.team_name or self.team_id
        return f"{self.alias} ({detail})" if detail else self.alias

    @property
    def cache_dir(self) -> Path:
        # Keyed on team ID so renaming an alias does not orphan the cache.
        return DATA_DIR / "accounts" / (self.team_id or self.alias)

    @property
    def user_cache_file(self) -> Path:
        return self.cache_dir / "user_cache.json"

    @property
    def reverse_user_cache_file(self) -> Path:
        return self.cache_dir / "reverse_user_cache.json"

    def auth(self) -> tuple[dict[str, str], dict[str, str]] | None:
        """Return (headers, cookies) for this account, or None if it has no tokens."""
        if self.bot_token:
            return (
                {
                    "Authorization": f"Bearer {self.bot_token}",
                    "Content-Type": "application/json",
                    "User-Agent": "MCP-Server/1.0",
                },
                {},
            )
        if self.xoxc_token and self.xoxd_token:
            return (
                {
                    "Authorization": f"Bearer {self.xoxc_token}",
                    "Content-Type": "application/json",
                    "User-Agent": "MCP-Server/1.0",
                },
                {"d": self.xoxd_token},
            )
        return None


_accounts: dict[str, SlackAccount] = {}
_default_alias: str = ""

# Set per task during fan-out so the shared helpers (caches, activity logging)
# act on the right workspace. Unset means "use the default account".
_active_account: contextvars.ContextVar["SlackAccount | None"] = contextvars.ContextVar(
    "slack_active_account", default=None
)


def _migrate_legacy_caches(account: SlackAccount) -> None:
    """Move pre-multi-workspace cache files into the default account's directory."""
    for old, new in (
        (DATA_DIR / ".user_cache.json", account.user_cache_file),
        (DATA_DIR / ".reverse_user_cache.json", account.reverse_user_cache_file),
    ):
        if old.exists() and not new.exists():
            try:
                new.parent.mkdir(parents=True, exist_ok=True)
                old.replace(new)
                log(f"slack-mcp: migrated {old.name} -> {new}")
            except OSError as e:
                log(f"slack-mcp: could not migrate {old.name}: {e}")


def _load_accounts() -> None:
    """Build the account registry from workspaces.json, falling back to the
    single-workspace environment variables when that file is absent."""
    global _accounts, _default_alias
    _accounts = {}
    _default_alias = ""

    config: dict[str, Any] = {}
    if WORKSPACES_FILE.exists():
        try:
            config = json.loads(WORKSPACES_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log(f"slack-mcp: could not read {WORKSPACES_FILE}: {e}")

    # One Slack login shares a single `d` cookie across every workspace it is
    # signed into, so xoxd may be declared once at the top level.
    shared_xoxd = config.get("xoxd") or config.get("xoxd_token") or ""

    for alias, entry in (config.get("accounts") or {}).items():
        if not isinstance(entry, dict):
            log(f"slack-mcp: skipping malformed account '{alias}' in {WORKSPACES_FILE}")
            continue
        account = SlackAccount(
            alias=alias,
            team_id=entry.get("team_id", ""),
            team_name=entry.get("team_name", ""),
            bot_token=entry.get("bot_token", ""),
            xoxc_token=entry.get("xoxc_token") or entry.get("xoxc", ""),
            xoxd_token=entry.get("xoxd_token") or entry.get("xoxd", "") or shared_xoxd,
            logs_channel_id=entry.get("logs_channel_id") or entry.get("logs_channel", ""),
        )
        if not account.has_tokens:
            log(f"slack-mcp: account '{alias}' has no usable tokens - skipping")
            continue
        _accounts[alias] = account

    if _accounts:
        requested = config.get("default", "")
        _default_alias = requested if requested in _accounts else next(iter(_accounts))
        if requested and requested not in _accounts:
            log(f"slack-mcp: default '{requested}' is not a configured account; using '{_default_alias}'")
        if SLACK_BOT_TOKEN or _get_session_tokens_from_env():
            log(f"slack-mcp: {WORKSPACES_FILE.name} defines accounts; ignoring tokens from the environment")
        return

    # No usable accounts in the file: fall back to a single env-configured one.
    xoxc, xoxd = _get_session_tokens_from_env() or ("", "")
    account = SlackAccount(
        alias=DEFAULT_ACCOUNT_ALIAS,
        team_id=SLACK_TEAM_ID,
        bot_token=SLACK_BOT_TOKEN,
        xoxc_token=xoxc,
        xoxd_token=xoxd,
        logs_channel_id=LOGS_CHANNEL_ID,
    )
    _accounts[DEFAULT_ACCOUNT_ALIAS] = account
    _default_alias = DEFAULT_ACCOUNT_ALIAS


def default_account() -> SlackAccount:
    """The account used when a tool is given no workspace."""
    if not _accounts:
        _load_accounts()
    return _accounts[_default_alias]


def _current_account() -> SlackAccount:
    """The account the running tool should act in."""
    active = _active_account.get()
    return active if active is not None else default_account()


@contextmanager
def _using_account(account: SlackAccount):
    token = _active_account.set(account)
    try:
        yield account
    finally:
        _active_account.reset(token)


def _find_account(key: str) -> SlackAccount | None:
    if key in _accounts:
        return _accounts[key]
    lowered = key.lower()
    for account in _accounts.values():
        if account.alias.lower() == lowered:
            return account
    for account in _accounts.values():
        if lowered in (account.team_id.lower(), account.team_name.lower()) and lowered:
            return account
    return None


def resolve_accounts(workspace: str = "") -> list[SlackAccount]:
    """Map a workspace selector onto accounts.

    "" selects the default account, "all" (or "*") every configured one, and
    anything else is a comma-separated list of aliases, team IDs, or team names.
    """
    if not _accounts:
        _load_accounts()
    selector = (workspace or "").strip()
    if not selector:
        return [default_account()]
    if selector.lower() in ("all", "*"):
        return list(_accounts.values())

    chosen: list[SlackAccount] = []
    unknown: list[str] = []
    for part in selector.split(","):
        key = part.strip()
        if not key:
            continue
        account = _find_account(key)
        if account is None:
            unknown.append(key)
        elif account not in chosen:
            chosen.append(account)
    if unknown:
        known = ", ".join(_accounts) or "none configured"
        raise ValueError(
            f"Unknown workspace(s): {', '.join(unknown)}. Configured: {known}"
        )
    return chosen


async def _fan_out(
    accounts: list[SlackAccount],
    fn: "Callable[[SlackAccount], Coroutine[Any, Any, Any]]",
) -> dict[str, Any]:
    """Run `fn` against each account concurrently, isolating per-account failures.

    asyncio.gather wraps each coroutine in its own task, and a task gets a copy
    of the current context, so the active account set inside one cannot leak
    into a sibling.
    """

    async def _one(account: SlackAccount) -> Any:
        with _using_account(account):
            return await fn(account)

    results = await asyncio.gather(
        *(_one(account) for account in accounts), return_exceptions=True
    )
    out: dict[str, Any] = {}
    for account, result in zip(accounts, results):
        if isinstance(result, BaseException):
            log(f"slack-mcp: {account.alias}: {result}")
            out[account.alias] = {"ok": False, "error": str(result)}
        else:
            out[account.alias] = result
    return out


_load_accounts()


async def make_request(
    url: str,
    method: str = "POST",
    payload: dict[str, Any] | None = None,
    account: SlackAccount | None = None,
) -> dict[str, Any] | None:
    """Call the Slack API as `account`, or as whichever account is active."""
    cookies: dict[str, str] = {}

    acct = account if account is not None else _current_account()
    if (acct_auth := acct.auth()) is not None:
        headers, cookies = acct_auth
    elif MCP_TRANSPORT != "stdio":
        request_headers = mcp.get_context().request_context.request.headers
        if "X-Slack-Bot-Token" in request_headers:
            bot_token = request_headers["X-Slack-Bot-Token"]
            headers = {
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json",
            }
        else:
            xoxc_token = request_headers["X-Slack-Web-Token"]
            xoxd_token = request_headers["X-Slack-Cookie-Token"]
            headers = {
                "Authorization": f"Bearer {xoxc_token}",
                "Content-Type": "application/json",
                "User-Agent": request_headers.get("User-Agent", "MCP-Server/1.0"),
            }
            cookies = {"d": xoxd_token}
    else:
        log("Error: stdio mode requires SLACK_XOXC_TOKEN and SLACK_XOXD_TOKEN (or SLACK_BOT_TOKEN)")
        return None

    async with httpx.AsyncClient(cookies=cookies) as client:
        try:
            if method.upper() == "GET":
                response = await client.request(
                    method,
                    url,
                    headers=headers,
                    params=payload,
                    timeout=30.0,
                )
            else:
                response = await client.request(
                    method,
                    url,
                    headers=headers,
                    json=payload,
                    timeout=30.0,
                )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            log(str(e))
            return None


async def log_to_slack(message: str, account: SlackAccount | None = None):
    if _is_read_only():
        log(f"[read-only] {message}")
        return
    acct = account if account is not None else _current_account()
    if not acct.logs_channel_id:
        log(message)
        return
    await post_message(acct.logs_channel_id, message, skip_log=True)


def parse_timestamp(date_str: str, is_end_of_range: bool = False) -> str:
    """Convert various date formats to Slack Unix timestamp.

    Args:
        date_str: Unix timestamp or ISO 8601 date string
        is_end_of_range: If True and date has no time, use end of day (23:59:59.999999)
                        If False and date has no time, use start of day (00:00:00)

    Returns:
        Unix timestamp as string with microsecond precision
    """
    if not date_str:
        return ""

    # Already a Unix timestamp (with or without microseconds)
    if re.match(r"^\d+(\.\d+)?$", date_str):
        return date_str

    # Parse ISO 8601 date
    try:
        # Check if it's a date-only format (no time component)
        is_date_only = re.match(r"^\d{4}-\d{2}-\d{2}$", date_str)

        if is_date_only:
            # Parse date and set time based on whether it's start or end of range
            dt = datetime.fromisoformat(date_str)
            if is_end_of_range:
                # End of day: 23:59:59.999999
                dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999, tzinfo=timezone.utc)
            else:
                # Start of day: 00:00:00
                dt = dt.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
        else:
            # Has time component, parse as-is
            # Handle both with and without timezone
            if 'Z' in date_str:
                date_str = date_str.replace('Z', '+00:00')
            dt = datetime.fromisoformat(date_str)
            # If no timezone info, assume UTC
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

        # Convert to Unix timestamp with microsecond precision
        return f"{dt.timestamp():.6f}"
    except ValueError as e:
        log(f"Error parsing date '{date_str}': {e}")
        return ""


def _load_user_cache(account: SlackAccount | None = None) -> None:
    """Load one account's user cache from disk."""
    acct = account if account is not None else _current_account()
    try:
        if acct.user_cache_file.exists():
            with open(acct.user_cache_file, 'r') as f:
                acct.user_cache = json.load(f)
            log(f"Loaded {len(acct.user_cache)} user handles from cache for {acct.label}")
    except Exception as e:
        log(f"Error loading user cache for {acct.alias}: {e}")
        acct.user_cache = {}
    acct.user_cache_loaded = True


def _save_user_cache(account: SlackAccount | None = None) -> None:
    """Save one account's user cache to disk."""
    acct = account if account is not None else _current_account()
    try:
        acct.cache_dir.mkdir(parents=True, exist_ok=True)
        with open(acct.user_cache_file, 'w') as f:
            json.dump(acct.user_cache, f, indent=2)
    except Exception as e:
        log(f"Error saving user cache for {acct.alias}: {e}")




def _load_reverse_user_cache(account: SlackAccount | None = None) -> list[dict[str, str]]:
    """Load one account's reverse user cache. Returns empty list if stale or missing."""
    acct = account if account is not None else _current_account()
    try:
        if acct.reverse_user_cache_file.exists():
            with open(acct.reverse_user_cache_file, 'r') as f:
                data = json.load(f)
            fetched_at = datetime.fromisoformat(data.get("fetched_at", "2000-01-01T00:00:00+00:00"))
            age_hours = (datetime.now(timezone.utc) - fetched_at).total_seconds() / 3600
            if age_hours < REVERSE_CACHE_TTL_HOURS:
                users = data.get("users", [])
                log(f"Loaded reverse user cache ({len(users)} users, {age_hours:.1f}h old)")
                return users
            log(f"Reverse user cache expired ({age_hours:.1f}h old)")
    except Exception as e:
        log(f"Error loading reverse user cache: {e}")
    return []


def _save_reverse_user_cache(
    users: list[dict[str, str]], account: SlackAccount | None = None
) -> None:
    """Save one account's reverse user cache to disk with a timestamp.

    Emails are excluded from the persisted payload to avoid storing PII on disk.
    """
    acct = account if account is not None else _current_account()
    try:
        sanitized = [{k: v for k, v in u.items() if k != "email"} for u in users]
        data = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "users": sanitized,
        }
        acct.cache_dir.mkdir(parents=True, exist_ok=True)
        with open(acct.reverse_user_cache_file, 'w') as f:
            json.dump(data, f, indent=2)
        log(f"Saved reverse user cache ({len(sanitized)} users)")
    except Exception as e:
        log(f"Error saving reverse user cache: {e}")


async def _fetch_all_users(account: SlackAccount | None = None) -> list[dict[str, str]]:
    """Fetch all workspace users via users.list with pagination. Returns simplified user records."""
    acct = account if account is not None else _current_account()
    cached = _load_reverse_user_cache(acct)
    if cached:
        return cached

    url = f"{SLACK_API_BASE}/users.list"
    all_users: list[dict[str, str]] = []
    cursor = None

    while True:
        payload: dict[str, Any] = {"limit": 200}
        if cursor:
            payload["cursor"] = cursor

        data = await make_request(url, method="GET", payload=payload, account=acct)
        if not data or not data.get("ok"):
            error_msg = data.get("error", "Unknown error") if data else "No response"
            log(f"Error fetching users.list for {acct.alias}: {error_msg}")
            break

        for member in data.get("members", []):
            if member.get("deleted") or member.get("is_bot"):
                continue
            profile = member.get("profile", {})
            all_users.append({
                "id": member.get("id", ""),
                "handle": member.get("name", ""),
                "real_name": member.get("real_name", ""),
                "display_name": profile.get("display_name", ""),
                "email": profile.get("email", ""),
            })

        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    if all_users:
        _save_reverse_user_cache(all_users, acct)
        for u in all_users:
            display = u["display_name"] or u["real_name"] or u["handle"] or u["id"]
            acct.user_cache[u["id"]] = display
        _save_user_cache(acct)

    log(f"Fetched {len(all_users)} users from {acct.label}")
    return all_users

async def get_user_handle(user_id: str, account: SlackAccount | None = None) -> str:
    """Get user handle by ID with caching. Returns user_id if lookup fails.

    User IDs are workspace-scoped, so the cache consulted is the acting
    account's own.
    """
    acct = account if account is not None else _current_account()

    if not user_id:
        return ""

    if not acct.user_cache_loaded:
        _load_user_cache(acct)

    # Check cache first
    if user_id in acct.user_cache:
        return acct.user_cache[user_id]

    # Cache miss - fetch user info
    url = f"{SLACK_API_BASE}/users.info"
    payload = {"user": user_id}
    data = await make_request(url, method="GET", payload=payload, account=acct)

    if data and data.get("ok"):
        user = data.get("user", {})
        profile = user.get("profile", {})
        # Prefer display_name, fall back to real_name, then name
        handle = (
            profile.get("display_name")
            or user.get("real_name")
            or user.get("name")
            or user_id
        )
        acct.user_cache[user_id] = handle
        _save_user_cache(acct)  # Persist to disk
        return handle

    # If lookup fails, cache the user_id itself to avoid repeated failed lookups
    acct.user_cache[user_id] = user_id
    _save_user_cache(acct)  # Persist to disk
    return user_id


async def replace_user_mentions(text: str) -> str:
    """Replace user ID mentions (<@USERID>) with handles (@handle)."""
    if not text:
        return text

    # Find all user mentions in the format <@USERID>
    mention_pattern = r'<@([A-Z0-9]+)>'
    matches = re.finditer(mention_pattern, text)

    # Process each mention
    replacements = {}
    for match in matches:
        user_id = match.group(1)
        if user_id not in replacements:
            handle = await get_user_handle(user_id)
            replacements[user_id] = handle

    # Replace all mentions with handles
    for user_id, handle in replacements.items():
        text = text.replace(f'<@{user_id}>', f'@{handle}')

    return text


async def filter_message_fields(message: dict[str, Any]) -> dict[str, Any] | str:
    """Filter message to only essential fields to reduce token usage."""
    # Extract essential fields
    text = message.get("text", "")
    user_id = message.get("user", "")
    ts = message.get("ts", "")
    thread_ts = message.get("thread_ts", "")

    # Extract channel info (present in search results)
    channel = message.get("channel", {})
    channel_id = channel.get("id", "") if isinstance(channel, dict) else ""
    channel_name = channel.get("name", "") if isinstance(channel, dict) else ""

    # Get user handle instead of ID
    user_handle = await get_user_handle(user_id) if user_id else ""

    # Replace user mentions in text with handles
    text = await replace_user_mentions(text)

    if OUTPUT_FORMAT == "json":
        # Return structured JSON format with handle instead of ID
        filtered = {
            "text": text,
            "user": user_handle,
            "ts": ts,
        }
        if thread_ts:
            filtered["thread_ts"] = thread_ts
        if channel_id:
            filtered["channel_id"] = channel_id
        if channel_name:
            filtered["channel_name"] = channel_name
        return filtered
    else:
        # Return compact text format (default)
        result = f"[{ts}] @{user_handle}: {text}"
        if channel_id:
            result += f" [channel:{channel_id}|{channel_name}]"
        if thread_ts and thread_ts != ts:
            result += f" [thread:{thread_ts}]"
        return result


# Validate and convert thread_ts if needed
def convert_thread_ts(ts: str) -> str:
    # If ts is already in the correct format, return as is
    if re.match(r"^\d+\.\d+$", ts):
        return ts
    # If ts is a long integer string (from Slack URL), convert it
    if re.match(r"^\d{16}$", ts):
        return f"{ts[:10]}.{ts[10:]}"
    return ""


async def get_thread_replies(channel_id: str, thread_ts: str) -> list[dict[str, Any]]:
    """Get all replies in a thread."""
    url = f"{SLACK_API_BASE}/conversations.replies"
    payload = {"channel": channel_id, "ts": thread_ts}

    data = await make_request(url, method="GET", payload=payload)

    if not data or not data.get("ok"):
        error_msg = data.get("error", "Unknown error") if data else "No response from Slack API"
        log(f"Error getting thread replies: {error_msg}")
        return []

    # Returns all messages including the parent, so we skip the first one
    messages = data.get("messages", [])
    return messages[1:] if len(messages) > 1 else []


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
async def get_thread(
    channel_id: str,
    thread_ts: str,
    limit: int = 100
) -> list[dict[str, Any] | str]:
    """Get all messages in a thread given a channel ID and the parent message timestamp.

    Use this to read a full conversation thread before replying to it.
    The thread_ts is the timestamp of the parent message that started the thread.
    """
    await log_to_slack(f"Getting thread {thread_ts} in channel <#{channel_id}> (limit: {limit})")
    url = f"{SLACK_API_BASE}/conversations.replies"
    payload = {
        "channel": channel_id,
        "ts": convert_thread_ts(thread_ts),
        "limit": limit,
    }

    data = await make_request(url, method="GET", payload=payload)

    if not data or not data.get("ok"):
        error_msg = data.get("error", "Unknown error") if data else "No response from Slack API"
        log(f"Error getting thread: {error_msg}")
        return []

    messages = data.get("messages", [])
    log(f"Retrieved {len(messages)} messages from thread {thread_ts}")

    # Pre-fetch all unique user handles
    unique_users = {msg.get("user") for msg in messages if msg.get("user")}
    mention_pattern = r'<@([A-Z0-9]+)>'
    for msg in messages:
        text = msg.get("text", "")
        if text:
            unique_users.update(re.findall(mention_pattern, text))
    for user_id in unique_users:
        await get_user_handle(user_id)

    return await asyncio.gather(*[filter_message_fields(msg) for msg in messages])


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
async def get_channel_history(
    channel_id: str,
    limit: int = 1000,
    oldest: str = "",
    latest: str = "",
    include_threads: bool = False
) -> list[dict[str, Any] | str]:
    """Get the history of a channel with pagination support. Limit parameter controls max messages to fetch (default 1000).

    Optional date filtering (accepts ISO 8601 dates or Unix timestamps):
    - oldest: Only messages after this date (e.g., "2024-01-15" or "2024-01-15T10:30:00")
    - latest: Only messages before this date (e.g., "2024-01-20" or "2024-01-20T18:00:00")
    - include_threads: If True, also fetch all replies in threads (default False)

    Note: For date-only formats, 'oldest' defaults to start of day (00:00:00) and 'latest' to end of day (23:59:59).
    """
    await log_to_slack(f"Getting history of channel <#{channel_id}> (limit: {limit}, include_threads: {include_threads})")
    url = f"{SLACK_API_BASE}/conversations.history"

    # Parse timestamp parameters
    oldest_ts = parse_timestamp(oldest, is_end_of_range=False)
    latest_ts = parse_timestamp(latest, is_end_of_range=True)

    all_messages = []
    cursor = None

    while len(all_messages) < limit:
        payload = {"channel": channel_id, "limit": min(200, limit - len(all_messages))}
        if oldest_ts:
            payload["oldest"] = oldest_ts
        if latest_ts:
            payload["latest"] = latest_ts
        if cursor:
            payload["cursor"] = cursor

        data = await make_request(url, method="GET", payload=payload)

        if not data or not data.get("ok"):
            error_msg = data.get("error", "Unknown error") if data else "No response from Slack API"
            log(f"Error getting channel history: {error_msg}")
            break

        messages = data.get("messages", [])
        all_messages.extend(messages)

        # Check if there are more messages
        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    log(f"Retrieved {len(all_messages)} messages from channel {channel_id}")

    # Fetch thread replies if requested
    if include_threads:
        thread_messages = []
        for msg in all_messages:
            # Check if message has replies (is a parent message)
            reply_count = msg.get("reply_count", 0)
            if reply_count > 0:
                thread_ts = msg.get("ts")
                if thread_ts:
                    replies = await get_thread_replies(channel_id, thread_ts)
                    thread_messages.extend(replies)

        all_messages.extend(thread_messages)
        log(f"Retrieved {len(thread_messages)} additional messages from threads")

    # Pre-fetch all unique user handles to avoid duplicate API calls
    # Include both message authors and users mentioned in text
    unique_users = {msg.get("user") for msg in all_messages if msg.get("user")}

    # Extract user IDs from mentions in message text
    mention_pattern = r'<@([A-Z0-9]+)>'
    for msg in all_messages:
        text = msg.get("text", "")
        if text:
            mentioned_users = re.findall(mention_pattern, text)
            unique_users.update(mentioned_users)

    # Fetch all unique users
    for user_id in unique_users:
        await get_user_handle(user_id)

    # Filter messages to reduce token usage (now all users are cached)
    return await asyncio.gather(*[filter_message_fields(msg) for msg in all_messages])


async def _load_channels_to_cache(account: SlackAccount | None = None) -> bool:
    """Load all channels into the account's cache with pagination. True if successful."""
    acct = account if account is not None else _current_account()

    url = f"{SLACK_API_BASE}/conversations.list"
    acct.channel_cache.clear()
    cursor = None

    while True:
        payload = {
            "exclude_archived": "true",
            "types": "public_channel,private_channel",
            "limit": 200,
        }
        if cursor:
            payload["cursor"] = cursor

        data = await make_request(url, method="GET", payload=payload, account=acct)

        if not data or not data.get("ok"):
            error_msg = data.get("error", "Unknown error") if data else "No response from Slack API"
            log(f"Error loading channels to cache for {acct.alias}: {error_msg}")
            return bool(acct.channel_cache)

        for channel in data.get("channels", []):
            channel_name = channel.get("name", "")
            channel_id = channel.get("id", "")
            if channel_name and channel_id:
                acct.channel_cache[channel_name] = channel_id

        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    log(f"Loaded {len(acct.channel_cache)} channels into cache for {acct.label}")
    return True


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
async def get_channel_id_by_name(channel_name: str) -> str:
    """Get the channel ID by channel name. The channel name can be with or without the # prefix."""
    # Remove # prefix if present
    clean_name = channel_name.lstrip("#")
    await log_to_slack(f"Looking up channel ID for channel name: {clean_name}")
    acct = _current_account()

    # Check cache first
    if clean_name in acct.channel_cache:
        log(f"Channel '{clean_name}' found in cache")
        return acct.channel_cache[clean_name]

    # Cache miss - load all channels
    log(f"Cache miss for '{clean_name}', loading channels...")
    if await _load_channels_to_cache(acct):
        # Check cache again after loading
        if clean_name in acct.channel_cache:
            return acct.channel_cache[clean_name]

    log(f"Channel '{clean_name}' not found")
    return ""


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
async def refresh_channel_cache(workspace: str = "") -> bool:
    """Refresh the channel cache. Use this when new channels are created or if channel lookups are failing.

    Args:
        workspace: Which workspace to refresh; empty for the default account,
            "all" for every configured one. See list_workspaces.

    Returns True only if every selected workspace refreshed successfully.
    """
    accounts = resolve_accounts(workspace)
    await log_to_slack(f"Refreshing channel cache for {len(accounts)} workspace(s)")
    results = await _fan_out(accounts, _load_channels_to_cache)
    return all(bool(r) for r in results.values())


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
async def refresh_user_cache(workspace: str = "") -> int:
    """Clear the user cache. Use this when user handles are outdated or if user lookups are failing.

    Args:
        workspace: Which workspace to clear; empty for the default account,
            "all" for every configured one. See list_workspaces.

    Returns the total number of cached entries cleared.
    """
    accounts = resolve_accounts(workspace)
    await log_to_slack(f"Clearing user cache for {len(accounts)} workspace(s)")

    cleared = 0
    for acct in accounts:
        count = len(acct.user_cache)
        acct.user_cache.clear()
        acct.user_cache_loaded = False
        cleared += count
        for path in (acct.user_cache_file, acct.reverse_user_cache_file):
            try:
                if path.exists():
                    path.unlink()
            except OSError as e:
                log(f"Cleared {acct.alias} user cache in memory but failed to delete {path.name}: {e}")
        log(f"Cleared {count} user cache entries for {acct.label}")

    return cleared



@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True))
async def resolve_user_id(query: str) -> list[dict[str, str]]:
    """Resolve a Slack user ID from a name, @handle, or email address.

    Searches the full workspace member list (cached for 24h).
    Returns up to 5 matches sorted by relevance: exact handle > exact email >
    case-insensitive name > partial substring match.
    Each result contains id, handle, real_name, display_name, and email.
    """
    await log_to_slack(f"Resolving user ID for: {query}")
    users = await _fetch_all_users(_current_account())
    if not users:
        return []

    q = query.strip().lstrip("@").lower()
    if not q:
        return []

    exact_handle: list[dict[str, str]] = []
    exact_email: list[dict[str, str]] = []
    exact_name: list[dict[str, str]] = []
    partial: list[dict[str, str]] = []

    for u in users:
        handle = u.get("handle", "").lower()
        email = u.get("email", "").lower()
        real = u.get("real_name", "").lower()
        display = u.get("display_name", "").lower()

        if handle == q:
            exact_handle.append(u)
        elif email == q or email.split("@")[0] == q:
            exact_email.append(u)
        elif real == q or display == q:
            exact_name.append(u)
        elif q in handle or q in real or q in display or q in email:
            partial.append(u)

    results = exact_handle + exact_email + exact_name + partial
    return results[:5]


@_register_tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
async def post_message(
    channel_id: str,
    message: str,
    thread_ts: str = "",
    skip_log: bool = False,
    unfurl_links: bool = True,
    unfurl_media: bool = True,
    blocks: str = "",
) -> dict:
    """Post a message to a channel. Optionally pass Block Kit blocks as a JSON string for rich formatting (e.g. nested lists via rich_text blocks). When blocks is provided, message is used as the plaintext fallback.

    Returns a dict {"ok": bool, "ts": str}. On success, "ts" is the posted message's timestamp, which can be passed back as thread_ts to post threaded replies.
    """
    _deny_if_read_only()
    if not skip_log:
        await log_to_slack(f"Posting message to channel <#{channel_id}>: {message}")
    await join_channel(channel_id, skip_log=skip_log)
    url = f"{SLACK_API_BASE}/chat.postMessage"
    payload = {"channel": channel_id, "text": message}
    if blocks:
        try:
            payload["blocks"] = json.loads(blocks)
        except json.JSONDecodeError as e:
            log(f"Error: invalid blocks JSON: {e}")
            return {"ok": False, "ts": ""}
    if thread_ts:
        payload["thread_ts"] = convert_thread_ts(thread_ts)
    if not unfurl_links:
        payload["unfurl_links"] = False
    if not unfurl_media:
        payload["unfurl_media"] = False
    data = await make_request(url, payload=payload)
    ok = bool(data and data.get("ok"))
    return {"ok": ok, "ts": data.get("ts", "") if ok else ""}


@_register_tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
async def post_command(
    channel_id: str, command: str, text: str, skip_log: bool = False
) -> bool:
    """Post a command to a channel."""
    _deny_if_read_only()
    if not skip_log:
        await log_to_slack(
            f"Posting command to channel <#{channel_id}>: {command} {text}"
        )
    await join_channel(channel_id, skip_log=skip_log)
    url = f"{SLACK_API_BASE}/chat.command"
    payload = {"channel": channel_id, "command": command, "text": text}
    data = await make_request(url, payload=payload)
    return bool(data and data.get("ok"))


@_register_tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
async def add_reaction(channel_id: str, message_ts: str, reaction: str) -> bool:
    """Add a reaction to a message."""
    _deny_if_read_only()
    await log_to_slack(
        f"Adding reaction to message {message_ts} in channel <#{channel_id}>: :{reaction}:"
    )
    url = f"{SLACK_API_BASE}/reactions.add"
    payload = {
        "channel": channel_id,
        "name": reaction,
        "timestamp": convert_thread_ts(message_ts),
    }
    data = await make_request(url, payload=payload)
    return bool(data and data.get("ok"))

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
async def get_reactions(
    channel_id: str, message_ts: str, full: bool = True
) -> list[dict] | None:
    """Get reactions to a message."""
    await log_to_slack(
        f"Getting reaction to message {message_ts} in channel <#{channel_id}>"
    )
    url = f"{SLACK_API_BASE}/reactions.get"
    payload = {
        "channel": channel_id,
        "timestamp": convert_thread_ts(message_ts),
        "full": full,
    }
    data = await make_request(url, method="GET", payload=payload)
    if data and data.get("ok"):
        message = data.get("message", {})
        return message.get("reactions", [])
    return None


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
async def whoami() -> str:
    """Checks authentication & identity."""
    await log_to_slack("Checking authentication & identity")
    url = f"{SLACK_API_BASE}/auth.test"
    data = await make_request(url)
    return data.get("user") if data else None


@_register_tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
async def join_channel(channel_id: str, skip_log: bool = False) -> bool:
    """Join a channel."""
    _deny_if_read_only()
    if not skip_log:
        await log_to_slack(f"Joining channel <#{channel_id}>")
    url = f"{SLACK_API_BASE}/conversations.join"
    payload = {"channel": channel_id}
    data = await make_request(url, payload=payload)
    return bool(data and data.get("ok"))


@_register_tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
async def create_channel(name: str, is_private: bool = False) -> str | None:
    """Create a new Slack channel.

    Args:
        name: Channel name (will be sanitized: lowercase, hyphens, no spaces)
        is_private: Create as private channel (default: False for public)

    Returns:
        Channel ID on success, None on failure
    """
    _deny_if_read_only()

    sanitized_name = name.lower()
    sanitized_name = sanitized_name.replace(" ", "-")
    sanitized_name = re.sub(r'[^a-z0-9\-_]', '', sanitized_name)
    sanitized_name = sanitized_name[:80]

    channel_type = "private" if is_private else "public"
    await log_to_slack(f"Creating {channel_type} channel: {sanitized_name}")

    url = f"{SLACK_API_BASE}/conversations.create"
    payload: dict[str, Any] = {
        "name": sanitized_name,
        "is_private": is_private
    }
    acct = _current_account()
    if acct.team_id:
        payload["team_id"] = acct.team_id

    data = await make_request(url, payload=payload)

    if not data or not data.get("ok"):
        error_msg = data.get("error", "Unknown error") if data else "No response from Slack API"
        log(f"Error creating channel: {error_msg}")
        return None

    channel_id = data.get("channel", {}).get("id")
    if channel_id:
        acct.channel_cache[sanitized_name] = channel_id
        log(f"Successfully created channel {sanitized_name} (ID: {channel_id})")

    return channel_id


@_register_tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
async def invite_users_to_channel(channel_id: str, user_ids: list[str]) -> bool:
    """Invite multiple users to a channel.

    Args:
        channel_id: Target channel ID (C...)
        user_ids: List of user IDs to invite (U...)

    Returns:
        True if all invitations succeeded, False otherwise
    """
    _deny_if_read_only()

    if not user_ids:
        log("Error: user_ids list is empty")
        return False

    users_str = ",".join(user_ids)
    user_mentions = ", ".join([f"<@{uid}>" for uid in user_ids])
    await log_to_slack(f"Inviting {len(user_ids)} user(s) to channel <#{channel_id}>: {user_mentions}")

    url = f"{SLACK_API_BASE}/conversations.invite"
    payload = {
        "channel": channel_id,
        "users": users_str
    }

    data = await make_request(url, payload=payload)

    if not data or not data.get("ok"):
        error_msg = data.get("error", "Unknown error") if data else "No response from Slack API"
        log(f"Error inviting users to channel: {error_msg}")
        return False

    log(f"Successfully invited {len(user_ids)} user(s) to channel {channel_id}")
    return True


@_register_tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
async def rename_channel(channel_id: str, new_name: str) -> bool:
    """Rename a Slack channel.

    Args:
        channel_id: Target channel ID (C...)
        new_name: New channel name (will be sanitized: lowercase, hyphens, no spaces)

    Returns:
        True if rename succeeded, False otherwise
    """
    _deny_if_read_only()

    sanitized_name = new_name.lower()
    sanitized_name = sanitized_name.replace(" ", "-")
    sanitized_name = re.sub(r'[^a-z0-9\-_]', '', sanitized_name)
    sanitized_name = sanitized_name[:80]

    await log_to_slack(f"Renaming channel <#{channel_id}> to: {sanitized_name}")

    url = f"{SLACK_API_BASE}/conversations.rename"
    payload = {
        "channel": channel_id,
        "name": sanitized_name
    }

    data = await make_request(url, payload=payload)

    if not data or not data.get("ok"):
        error_msg = data.get("error", "Unknown error") if data else "No response from Slack API"
        log(f"Error renaming channel: {error_msg}")
        return False

    channel = data.get("channel", {})
    actual_name = channel.get("name", sanitized_name)
    log(f"Successfully renamed channel {channel_id} to {actual_name}")

    # Update channel cache with new name if we have it
    channel_cache = _current_account().channel_cache
    old_names_to_remove = [k for k, v in channel_cache.items() if v == channel_id]
    for old_name in old_names_to_remove:
        del channel_cache[old_name]
    channel_cache[actual_name] = channel_id

    return True


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
async def list_joined_channels(
    exclude_archived: bool = True,
    limit: int = 1000,
    types: str = "public_channel,private_channel",
) -> list[dict[str, Any]]:
    """List channels the authenticated user is a member of.

    Uses Slack's users.conversations API. By default returns public and private
    channels only. To include DMs and group DMs, set types to e.g.
    \"public_channel,private_channel,im,mpim\".
    """
    await log_to_slack(
        f"Listing joined channels (limit: {limit}, types: {types}, exclude_archived: {exclude_archived})"
    )
    url = f"{SLACK_API_BASE}/users.conversations"
    all_channels: list[dict[str, Any]] = []
    cursor: str | None = None

    while len(all_channels) < limit:
        payload: dict[str, Any] = {
            "types": types,
            "limit": min(200, limit - len(all_channels)),
            "exclude_archived": "true" if exclude_archived else "false",
        }
        if cursor:
            payload["cursor"] = cursor

        data = await make_request(url, method="GET", payload=payload)

        if not data or not data.get("ok"):
            error_msg = data.get("error", "Unknown error") if data else "No response from Slack API"
            log(f"Error listing joined channels: {error_msg}")
            break

        for ch in data.get("channels", []):
            entry: dict[str, Any] = {
                "id": ch.get("id", ""),
                "name": ch.get("name", ""),
                "is_private": ch.get("is_private", False),
                "is_archived": ch.get("is_archived", False),
            }
            if ch.get("is_im"):
                entry["is_im"] = True
            if ch.get("is_mpim"):
                entry["is_mpim"] = True
            all_channels.append(entry)

        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    log(f"Listed {len(all_channels)} joined channels")
    return all_channels


async def get_random_deleted_user() -> str | None:
    """Get a random deleted user ID for clearing usergroups.

    Slack API doesn't support empty usergroups, so we use a deleted user
    as a workaround to effectively clear the usergroup.

    Returns:
        User ID of a deleted user, or None if none found
    """
    url = f"{SLACK_API_BASE}/users.list"
    cursor = None

    while True:
        payload: dict[str, Any] = {"limit": 200}
        if cursor:
            payload["cursor"] = cursor

        data = await make_request(url, method="GET", payload=payload)

        if not data or not data.get("ok"):
            log("Error fetching users list for deleted user lookup")
            return None

        for user in data.get("members", []):
            if user.get("deleted") is True:
                user_id = user.get("id")
                log(f"Found deleted user: {user_id}")
                return user_id

        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    log("Could not find a deleted user for clearing usergroup")
    return None


@_register_tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
async def clear_usergroup(usergroup_id: str) -> bool:
    """Clear all users from a Slack usergroup.

    Since Slack API doesn't support empty usergroups, this uses a workaround
    by setting the usergroup to contain only a randomly selected deleted user.
    This effectively clears the usergroup.

    Args:
        usergroup_id: The usergroup ID (S...) to clear

    Returns:
        True if usergroup was cleared successfully, False otherwise
    """
    _deny_if_read_only()

    await log_to_slack(f"Clearing usergroup {usergroup_id}")

    # Get a deleted user to use as placeholder
    deleted_user = await get_random_deleted_user()
    if not deleted_user:
        log("Failed to clear usergroup: no deleted user found")
        return False

    url = f"{SLACK_API_BASE}/usergroups.users.update"
    payload = {
        "usergroup": usergroup_id,
        "users": deleted_user
    }

    data = await make_request(url, payload=payload)

    if not data or not data.get("ok"):
        # Slack can throw invalid_users error when clearing groups,
        # but it may still clear the group successfully
        error = data.get("error") if data else "Unknown error"
        if error == "invalid_users":
            log(f"Received invalid_users error but usergroup {usergroup_id} may still be cleared")
            return True
        log(f"Error clearing usergroup: {error}")
        return False

    log(f"Successfully cleared usergroup {usergroup_id}")
    return True


@_register_tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
async def update_usergroup_members(usergroup_id: str, user_ids: list[str]) -> bool:
    """Set the members of a Slack usergroup, replacing the current membership entirely.

    Args:
        usergroup_id: The usergroup ID (S...) to update
        user_ids: List of user IDs (U...) that should be the members. Must contain
                  at least one user — use clear_usergroup to remove all members.

    Returns:
        True if the membership was updated successfully, False otherwise
    """
    _deny_if_read_only()

    if not user_ids:
        log("Error: user_ids list is empty — use clear_usergroup to remove all members")
        return False

    users_str = ",".join(user_ids)
    user_mentions = ", ".join([f"<@{uid}>" for uid in user_ids])
    await log_to_slack(f"Updating usergroup {usergroup_id} to {len(user_ids)} member(s): {user_mentions}")

    url = f"{SLACK_API_BASE}/usergroups.users.update"
    payload = {
        "usergroup": usergroup_id,
        "users": users_str,
    }

    data = await make_request(url, payload=payload)

    if not data or not data.get("ok"):
        error_msg = data.get("error", "Unknown error") if data else "No response from Slack API"
        log(f"Error updating usergroup members: {error_msg}")
        return False

    log(f"Successfully updated usergroup {usergroup_id} to {len(user_ids)} member(s)")
    return True


@_register_tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
async def send_dm(
    user_id: str,
    message: str,
    unfurl_links: bool = True,
    unfurl_media: bool = True,
    blocks: str = "",
) -> bool:
    """Send a direct message to a user. Optionally pass Block Kit blocks as a JSON string for rich formatting (e.g. nested lists via rich_text blocks). When blocks is provided, message is used as the plaintext fallback."""
    _deny_if_read_only()
    await log_to_slack(f"Sending direct message to user <@{user_id}>: {message}")
    url = f"{SLACK_API_BASE}/conversations.open"
    payload = {"users": user_id, "return_dm": True}
    data = await make_request(url, payload=payload)
    if data and data.get("ok"):
        result = await post_message(
            data.get("channel", {}).get("id"),
            message,
            skip_log=True,
            unfurl_links=unfurl_links,
            unfurl_media=unfurl_media,
            blocks=blocks,
        )
        return bool(result.get("ok"))
    return False


@_register_tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
async def send_group_dm(
    user_ids: list[str],
    message: str,
    unfurl_links: bool = True,
    unfurl_media: bool = True,
    blocks: str = "",
) -> bool:
    """Send a message to a group DM (multi-party direct message).

    Args:
        user_ids: List of 2+ user IDs to include in group DM
        message: Message text to send

    Returns:
        True if message sent successfully, False otherwise
    """
    _deny_if_read_only()

    if len(user_ids) < 2:
        log("Error: group DM requires at least 2 users")
        return False

    users_str = ",".join(user_ids)
    user_mentions = ", ".join([f"<@{uid}>" for uid in user_ids])
    await log_to_slack(
        f"Sending group DM to {len(user_ids)} users ({user_mentions}) "
        f"(message_length={len(message)})"
    )

    url = f"{SLACK_API_BASE}/conversations.open"
    payload = {"users": users_str, "return_im": True}
    data = await make_request(url, payload=payload)

    if not data or not data.get("ok"):
        error_msg = data.get("error", "Unknown error") if data else "No response from Slack API"
        log(f"Error opening group DM: {error_msg}")
        return False

    channel_id = data.get("channel", {}).get("id")
    if not channel_id:
        log("Error: No channel ID returned from conversations.open")
        return False

    return bool((await post_message(
        channel_id,
        message,
        skip_log=True,
        unfurl_links=unfurl_links,
        unfurl_media=unfurl_media,
        blocks=blocks,
    )).get("ok"))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
async def search_messages(
    query: str, sort: Literal["timestamp", "score"] = "timestamp", limit: int = 1000
) -> list[dict[str, Any] | str]:
    """Search for messages in the workspace with pagination support. Limit parameter controls max results to fetch (default 1000)."""
    await log_to_slack(f"Searching for messages: {query} (limit: {limit})")
    url = f"{SLACK_API_BASE}/search.messages"

    all_matches = []
    page = 1

    while len(all_matches) < limit:
        payload = {
            "query": query,
            "sort": sort,
            "count": min(100, limit - len(all_matches)),  # Slack max is 100 per page
            "page": page
        }

        data = await make_request(url, method="GET", payload=payload)

        if not data or not data.get("ok"):
            error_msg = data.get("error", "Unknown error") if data else "No response from Slack API"
            log(f"Error searching messages: {error_msg}")
            break

        messages_data = data.get("messages", {})
        matches = messages_data.get("matches", [])
        all_matches.extend(matches)

        # Check pagination info
        total_pages = messages_data.get("pagination", {}).get("page_count", 1)
        if page >= total_pages or len(matches) == 0:
            break

        page += 1

    log(f"Retrieved {len(all_matches)} search results for query: {query}")

    # Pre-fetch all unique user handles to avoid duplicate API calls
    # Include both message authors and users mentioned in text
    unique_users = {msg.get("user") for msg in all_matches if msg.get("user")}

    # Extract user IDs from mentions in message text
    mention_pattern = r'<@([A-Z0-9]+)>'
    for msg in all_matches:
        text = msg.get("text", "")
        if text:
            mentioned_users = re.findall(mention_pattern, text)
            unique_users.update(mentioned_users)

    # Fetch all unique users
    for user_id in unique_users:
        await get_user_handle(user_id)

    # Filter messages to reduce token usage (now all users are cached)
    return await asyncio.gather(*[filter_message_fields(msg) for msg in all_matches])


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
async def search_channel_messages(
    channel_id: str,
    query: str,
    sort: Literal["timestamp", "score"] = "timestamp",
    limit: int = 100,
) -> list[dict[str, Any] | str]:
    """Search for messages within a specific channel.

    Uses Slack's search API with an 'in:<channel>' filter.

    Args:
        channel_id: The channel ID to search within.
        query: The search query text.
        sort: Sort results by "timestamp" (newest first) or "score" (relevance).
        limit: Max number of results to return (default 100).
    """
    await log_to_slack(f"Searching in channel <#{channel_id}> for: {query} (limit: {limit})")
    url = f"{SLACK_API_BASE}/search.messages"

    all_matches = []
    page = 1
    scoped_query = f"in:<#{channel_id}> {query}"

    while len(all_matches) < limit:
        payload = {
            "query": scoped_query,
            "sort": sort,
            "count": min(100, limit - len(all_matches)),
            "page": page,
        }

        data = await make_request(url, method="GET", payload=payload)

        if not data or not data.get("ok"):
            error_msg = data.get("error", "Unknown error") if data else "No response from Slack API"
            log(f"Error searching channel messages: {error_msg}")
            break

        messages_data = data.get("messages", {})
        matches = messages_data.get("matches", [])
        all_matches.extend(matches)

        total_pages = messages_data.get("pagination", {}).get("page_count", 1)
        if page >= total_pages or len(matches) == 0:
            break

        page += 1

    log(f"Retrieved {len(all_matches)} search results for query in channel {channel_id}")

    unique_users = {msg.get("user") for msg in all_matches if msg.get("user")}
    mention_pattern = r'<@([A-Z0-9]+)>'
    for msg in all_matches:
        text = msg.get("text", "")
        if text:
            unique_users.update(re.findall(mention_pattern, text))
    for user_id in unique_users:
        await get_user_handle(user_id)

    return await asyncio.gather(*[filter_message_fields(msg) for msg in all_matches])


def _parse_status_expiration(value: str) -> int:
    """Convert a status expiry to a Unix timestamp (0 means it never clears).

    Accepts a relative offset (30m, 2h, 1d), an ISO 8601 date/time, or a Unix
    timestamp.
    """
    v = (value or "").strip()
    if not v:
        return 0
    if (m := re.fullmatch(r"(\d+)\s*([mhd])", v, re.IGNORECASE)):
        seconds = int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2).lower()]
        return int(datetime.now(timezone.utc).timestamp()) + seconds
    ts = parse_timestamp(v)
    if not ts:
        raise ValueError(
            f"Could not parse expiration '{value}'. Use a relative offset (30m, 2h, 1d), "
            "an ISO 8601 date/time, or a Unix timestamp."
        )
    return int(float(ts))


async def _fetch_custom_emoji(account: SlackAccount) -> set[str]:
    """Custom emoji names for one workspace, cached for the process lifetime."""
    if account.custom_emoji is not None:
        return account.custom_emoji
    data = await make_request(f"{SLACK_API_BASE}/emoji.list", method="GET", account=account)
    account.custom_emoji = set(data.get("emoji", {})) if data and data.get("ok") else set()
    return account.custom_emoji


async def _custom_emoji_gaps(emoji: str, accounts: list[SlackAccount]) -> set[str]:
    """Aliases where `emoji` is a custom emoji elsewhere but missing here.

    emoji.list only returns *custom* emoji, so absence on its own proves
    nothing — every standard emoji is absent too. But an emoji that exists as a
    custom one in another workspace and not in this one really will render
    blank here, and that is the cross-workspace mistake worth catching.
    """
    name = emoji.strip().strip(":")
    if not name or len(accounts) < 2:
        return set()
    per_account = await asyncio.gather(
        *(_fetch_custom_emoji(a) for a in accounts), return_exceptions=True
    )
    present: set[str] = set()
    missing: set[str] = set()
    for account, names in zip(accounts, per_account):
        if isinstance(names, BaseException):
            continue
        (present if name in names else missing).add(account.alias)
    return missing if present else set()


def _user_token_only(account: SlackAccount, action: str) -> dict[str, Any] | None:
    """Reject bot-token accounts for profile changes, which they cannot make."""
    if account.uses_bot_token:
        return {
            "ok": False,
            "error": (
                f"bot tokens cannot {action} for a user — a bot token only changes the "
                "bot's own profile. Configure xoxc/xoxd user tokens for this workspace."
            ),
        }
    return None


@_register_tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
async def set_status(
    text: str = "",
    emoji: str = "",
    expiration: str = "",
    workspace: str = "all",
) -> dict[str, Any]:
    """Set your Slack status, by default in every configured workspace at once.

    Args:
        text: Status text, e.g. "In a meeting". Empty clears the text.
        emoji: Status emoji, with or without colons, e.g. ":calendar:" or
            "calendar". Empty clears it.
        expiration: When the status should clear — a relative offset (30m, 2h,
            1d), an ISO 8601 date/time, or a Unix timestamp. Empty means never.
        workspace: "all" (default) for every configured workspace, or a
            comma-separated list of aliases or team IDs. See list_workspaces.

    Returns a per-workspace map of results; one workspace failing does not stop
    the others. Requires user (xoxc) tokens — a bot token cannot set your status.
    """
    _deny_if_read_only()
    accounts = resolve_accounts(workspace)
    expires_at = _parse_status_expiration(expiration)
    normalized = f":{emoji.strip().strip(':')}:" if emoji.strip() else ""
    gaps = await _custom_emoji_gaps(normalized, accounts) if normalized else set()

    async def _apply(account: SlackAccount) -> dict[str, Any]:
        if (refusal := _user_token_only(account, "set a status")) is not None:
            return refusal
        await log_to_slack(f"Setting status to {normalized} {text}".strip(), account=account)
        data = await make_request(
            f"{SLACK_API_BASE}/users.profile.set",
            payload={
                "profile": {
                    "status_text": text,
                    "status_emoji": normalized,
                    "status_expiration": expires_at,
                }
            },
            account=account,
        )
        if not data or not data.get("ok"):
            return {"ok": False, "error": (data or {}).get("error", "no response from Slack API")}
        result: dict[str, Any] = {
            "ok": True,
            "status_text": text,
            "status_emoji": normalized,
            "status_expiration": expires_at,
        }
        if account.alias in gaps:
            result["warning"] = (
                f"{normalized} is a custom emoji in another workspace but not this one — "
                "it will render blank here"
            )
        return result

    return await _fan_out(accounts, _apply)


@_register_tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
async def clear_status(workspace: str = "all") -> dict[str, Any]:
    """Clear your Slack status text and emoji, by default in every workspace at once.

    Args:
        workspace: "all" (default) for every configured workspace, or a
            comma-separated list of aliases or team IDs.
    """
    return await set_status(text="", emoji="", expiration="", workspace=workspace)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
async def get_status(workspace: str = "all") -> dict[str, Any]:
    """Read your current Slack status in each configured workspace.

    Args:
        workspace: "all" (default) for every configured workspace, or a
            comma-separated list of aliases or team IDs.
    """
    accounts = resolve_accounts(workspace)

    async def _read(account: SlackAccount) -> dict[str, Any]:
        data = await make_request(
            f"{SLACK_API_BASE}/users.profile.get", method="GET", account=account
        )
        if not data or not data.get("ok"):
            return {"ok": False, "error": (data or {}).get("error", "no response from Slack API")}
        profile = data.get("profile", {})
        return {
            "ok": True,
            "status_text": profile.get("status_text", ""),
            "status_emoji": profile.get("status_emoji", ""),
            "status_expiration": profile.get("status_expiration", 0),
        }

    return await _fan_out(accounts, _read)


@_register_tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
async def set_presence(
    presence: Literal["auto", "away"] = "auto", workspace: str = "all"
) -> dict[str, Any]:
    """Set your presence in every configured workspace at once.

    Args:
        presence: "auto" follows your actual activity; "away" forces away.
        workspace: "all" (default) for every configured workspace, or a
            comma-separated list of aliases or team IDs.
    """
    _deny_if_read_only()
    accounts = resolve_accounts(workspace)

    async def _apply(account: SlackAccount) -> dict[str, Any]:
        if (refusal := _user_token_only(account, "set presence")) is not None:
            return refusal
        await log_to_slack(f"Setting presence to {presence}", account=account)
        data = await make_request(
            f"{SLACK_API_BASE}/users.setPresence", payload={"presence": presence}, account=account
        )
        if not data or not data.get("ok"):
            return {"ok": False, "error": (data or {}).get("error", "no response from Slack API")}
        return {"ok": True, "presence": presence}

    return await _fan_out(accounts, _apply)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
async def list_workspaces(verify: bool = False) -> list[dict[str, Any]]:
    """List the Slack workspaces this server is configured to act in.

    Call this first to learn the aliases that the `workspace` argument on other
    tools accepts.

    Args:
        verify: Also call auth.test for each workspace to confirm the tokens
            still work and fill in the team name and signed-in user. Costs one
            API call per workspace.
    """
    accounts = resolve_accounts("all")
    rows: list[dict[str, Any]] = [
        {
            "alias": a.alias,
            "team_id": a.team_id,
            "team_name": a.team_name,
            "auth": "bot_token" if a.uses_bot_token else "user_token",
            "is_default": a.alias == _default_alias,
            "can_set_status": not a.uses_bot_token,
        }
        for a in accounts
    ]
    if not verify:
        return rows

    async def _check(account: SlackAccount) -> dict[str, Any]:
        data = await make_request(f"{SLACK_API_BASE}/auth.test", account=account)
        if not data or not data.get("ok"):
            return {"ok": False, "error": (data or {}).get("error", "no response from Slack API")}
        return {
            "ok": True,
            "team": data.get("team", ""),
            "team_id": data.get("team_id", ""),
            "user": data.get("user", ""),
        }

    checked = await _fan_out(accounts, _check)
    for row in rows:
        result = checked.get(row["alias"], {})
        row["verified"] = bool(result.get("ok"))
        if result.get("ok"):
            row["team_name"] = result.get("team") or row["team_name"]
            row["user"] = result.get("user", "")
            account = _find_account(row["alias"])
            if account is not None:
                account.team_name = row["team_name"]
                # Only fill a blank team ID: it keys the on-disk cache directory.
                if not account.team_id:
                    account.team_id = result.get("team_id", "")
                row["team_id"] = account.team_id
        else:
            row["error"] = result.get("error", "")
    return rows


def main() -> None:
    _load_accounts()
    accounts = list(_accounts.values())
    usable = [a for a in accounts if a.has_tokens]

    if DEFAULT_ACCOUNT_ALIAS in _accounts:
        _migrate_legacy_caches(_accounts[DEFAULT_ACCOUNT_ALIAS])

    for account in usable:
        _load_user_cache(account)
        if account.uses_bot_token and not account.bot_token.startswith("xoxb-"):
            log(
                f"Warning: bot token for '{account.alias}' does not start with xoxb- "
                "— is this a valid bot token?"
            )

    if usable:
        log(f"slack-mcp: {len(usable)} workspace(s) configured, default '{_default_alias}'")
        for account in usable:
            kind = "bot token (scoped)" if account.uses_bot_token else "user session token (full access)"
            default_marker = " [default]" if account.alias == _default_alias else ""
            log(f"slack-mcp:   {account.label} — {kind}{default_marker}")
        if not any(not a.uses_bot_token for a in usable):
            log(
                "slack-mcp: every workspace uses a bot token; set_status, clear_status and "
                "set_presence will refuse, since bot tokens cannot change a user's profile."
            )
    elif MCP_TRANSPORT == "stdio":
        log(
            "Error: stdio mode requires Slack tokens — either SLACK_XOXC_TOKEN and "
            f"SLACK_XOXD_TOKEN (or SLACK_BOT_TOKEN), or accounts in {WORKSPACES_FILE}"
        )
        sys.exit(1)
    else:
        log("slack-mcp: no server-side tokens — expecting per-request auth headers")
    if _is_read_only():
        log(
            f"slack-mcp: read-only mode is active ({READ_ONLY_ENV_VAR}); "
            "write tools are not registered; audit lines go to stderr only."
        )
    if MCP_TRANSPORT != "stdio":
        host = os.environ.get("FASTMCP_HOST", "0.0.0.0")
        port = int(os.environ.get("FASTMCP_PORT", "8000"))
        allowed_hosts = os.environ.get("ALLOWED_HOSTS", "")
        mcp.settings.host = host
        mcp.settings.port = port
        mcp.settings.transport_security.allowed_hosts += [
                h.strip() for h in allowed_hosts.split(",") if h.strip()
        ]
    mcp.run(transport=MCP_TRANSPORT)


if __name__ == "__main__":
    main()
