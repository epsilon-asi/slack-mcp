#!/usr/bin/env python3
"""
setup-slack-mcp.py — One-command Slack MCP setup for Claude Code

What this script does (automatically):
  1. Checks prerequisites (python3, podman)
  2. Creates a virtual environment and installs Playwright + Chromium
  3. Pulls the Slack MCP container image
  4. Opens a browser to extract session tokens for every Slack workspace
     you are signed in to (only manual step: log in when the browser opens)
  5. Prompts for a Slack channel ID to use for MCP server logs
  6. Writes a wrapper script that launches the MCP server
  7. Registers the MCP server in ~/.claude.json

Usage:
  python3 setup-slack-mcp.py
  python3 setup-slack-mcp.py --refresh-tokens                    # re-extract tokens
  python3 setup-slack-mcp.py --set-logs-channel DXXXXXXXXX       # skip the prompt
  python3 setup-slack-mcp.py --workspace https://myco.slack.com  # open a specific workspace
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


# ── Paths ────────────────────────────────────────────────────────────────────

INSTALL_DIR     = Path(os.environ.get("SLACK_MCP_DATA") or Path.home() / ".local" / "share" / "slack-mcp")
VENV_DIR        = INSTALL_DIR / ".venv"
TOKENS_FILE     = INSTALL_DIR / "tokens.env"
WORKSPACES_FILE = INSTALL_DIR / "workspaces.json"
WRAPPER_SCRIPT  = INSTALL_DIR / "run-slack-mcp.sh"
CLAUDE_SETTINGS = Path.home() / ".claude.json"

# Override when running a locally built image — the published one only gains
# multi-workspace support once it is rebuilt from a tree that has it.
MCP_IMAGE       = os.environ.get("SLACK_MCP_IMAGE", "quay.io/redhat-ai-tools/slack-mcp")
MCP_SERVER_NAME = "slack"
PROFILE_DIR     = Path.home() / ".slack-token-extractor" / "browser-profile"

DEFAULT_WORKSPACE = "https://app.slack.com/client/"


# ── Helpers ──────────────────────────────────────────────────────────────────

def banner(msg: str) -> None:
    width = 60
    print(f"\n{'─' * width}")
    print(f"  {msg}")
    print(f"{'─' * width}")


def run(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, **kwargs)


def venv_bin(name: str) -> Path:
    return VENV_DIR / "bin" / name


# ── Steps ────────────────────────────────────────────────────────────────────

def check_prerequisites() -> None:
    banner("Checking prerequisites")

    errors = []

    # podman (or docker as fallback)
    container_runtime = shutil.which("podman") or shutil.which("docker")
    if not container_runtime:
        errors.append("  ✗ Neither 'podman' nor 'docker' found — install one to continue.")
    else:
        runtime_name = Path(container_runtime).name
        print(f"  ✓ {runtime_name}: {container_runtime}")

    # python3 venv module
    result = subprocess.run(
        [sys.executable, "-m", "venv", "--help"],
        capture_output=True,
    )
    if result.returncode != 0:
        errors.append(f"  ✗ python3 venv module not available (try: sudo apt install python3-venv)")
    else:
        print(f"  ✓ python3: {sys.version.split()[0]}")

    if errors:
        print("\nPrerequisite check failed:")
        for e in errors:
            print(e)
        sys.exit(1)


def setup_venv() -> Path:
    """Create venv and install Playwright + Chromium. Returns path to venv python."""
    banner("Setting up Python environment")

    INSTALL_DIR.mkdir(parents=True, exist_ok=True)

    if not VENV_DIR.exists():
        print(f"  Creating venv at {VENV_DIR} ...")
        run([sys.executable, "-m", "venv", str(VENV_DIR)])
    else:
        print(f"  ✓ Venv already exists")

    pip    = venv_bin("pip")
    python = venv_bin("python")

    # Install / upgrade playwright
    result = subprocess.run([str(pip), "show", "playwright"], capture_output=True)
    if result.returncode != 0:
        print("  Installing playwright ...")
        run([str(pip), "install", "--quiet", "playwright"])
    else:
        print("  ✓ playwright already installed")

    # Check if Chromium browser binary exists
    chromium_check = subprocess.run(
        [str(python), "-c",
         "from playwright.sync_api import sync_playwright; "
         "p = sync_playwright().start(); "
         "path = p.chromium.executable_path; "
         "p.stop(); "
         "import sys, pathlib; sys.exit(0 if pathlib.Path(path).exists() else 1)"],
        capture_output=True,
    )
    if chromium_check.returncode != 0:
        print("  Installing Chromium for Playwright (one-time download) ...")
        run([str(venv_bin("playwright")), "install", "chromium"])
    else:
        print("  ✓ Chromium already installed")

    return python


def pull_image() -> None:
    banner("Pulling Slack MCP container image")

    # Detect runtime
    runtime = "podman" if shutil.which("podman") else "docker"

    result = subprocess.run(
        [runtime, "image", "exists", MCP_IMAGE] if runtime == "podman"
        else [runtime, "image", "inspect", MCP_IMAGE],
        capture_output=True,
    )
    if result.returncode == 0:
        print(f"  ✓ Image already present: {MCP_IMAGE}")
    else:
        print(f"  Pulling {MCP_IMAGE} ...")
        run([runtime, "pull", MCP_IMAGE])
        print(f"  ✓ Image ready")


def tokens_exist() -> bool:
    if WORKSPACES_FILE.exists():
        config = read_workspaces(WORKSPACES_FILE)
        if any(
            isinstance(e, dict) and (e.get("xoxc") or e.get("bot_token"))
            for e in (config.get("accounts") or {}).values()
        ):
            return True
    if not TOKENS_FILE.exists():
        return False
    text = TOKENS_FILE.read_text()
    return "SLACK_MCP_XOXC_TOKEN=xoxc-" in text and "SLACK_MCP_XOXD_TOKEN=" in text


# ── workspaces.json ──────────────────────────────────────────────────────────
#
# This script is published for `python3 <(curl ...)`, so it stays standalone
# rather than importing scripts/slack_workspaces.py. Both writers are covered by
# tests/test_workspaces_config.py, which asserts they agree on the schema.

def read_workspaces(path: Path) -> dict:
    """Load an existing config, or an empty one when absent or unreadable."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def alias_for(account: dict, taken: set) -> str:
    """A short, stable alias for a workspace, unique within `taken`."""
    raw = str(account.get("domain") or account.get("team_name") or account.get("team_id") or "workspace")
    alias = re.sub(r"[^a-z0-9_-]", "-", raw.lower()).strip("-") or "workspace"
    if alias not in taken:
        return alias
    for suffix in range(2, 100):
        if (candidate := f"{alias}-{suffix}") not in taken:
            return candidate
    return f"{alias}-{account.get('team_id', 'x')}".lower()


def merge_workspaces_config(
    existing: dict, accounts: list, xoxd: str, default: str = "", logs_channel: str = ""
) -> dict:
    """Fold freshly extracted accounts into an existing config.

    Accounts are matched on team ID, so re-running never renames a workspace or
    drops hand-added settings. Workspaces the browser did not see are preserved:
    a bot-token-only account has no xoxc to find.
    """
    merged = dict(existing)
    current = dict(merged.get("accounts") or {})

    by_team = {
        str(entry["team_id"]): alias
        for alias, entry in current.items()
        if isinstance(entry, dict) and entry.get("team_id")
    }

    for account in accounts:
        alias = by_team.get(account["team_id"]) or alias_for(account, set(current))
        entry = dict(current.get(alias) or {})
        entry["team_id"] = account["team_id"]
        if account.get("team_name"):
            entry["team_name"] = account["team_name"]
        entry["xoxc"] = account["xoxc"]
        # A per-account xoxd would shadow the shared one written below.
        entry.pop("xoxd", None)
        entry.pop("xoxd_token", None)
        current[alias] = entry
        by_team[account["team_id"]] = alias

    merged["accounts"] = current
    if xoxd:
        merged["xoxd"] = xoxd

    chosen = default or merged.get("default")
    if chosen not in current:
        chosen = next(iter(current), None)
    if chosen:
        merged["default"] = chosen
        # The logs channel is an ID in one workspace, so it belongs to one account.
        if logs_channel:
            current[chosen]["logs_channel"] = logs_channel

    return {
        key: merged[key]
        for key in ("default", "xoxd", "accounts", *sorted(set(merged) - {"default", "xoxd", "accounts"}))
        if key in merged
    }


def write_private(path: Path, text: str) -> None:
    """Write a 0600 file, created with those permissions from the start."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    os.chmod(path, 0o600)


# Inline extraction script — run inside the venv so Playwright is available.
_EXTRACT_SCRIPT = r'''
import json, os, re, sys
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

workspace_url    = sys.argv[1]
payload_file     = sys.argv[2]
profile_dir      = Path(sys.argv[3])
channel_id_file  = sys.argv[4]

channel_id = ""

profile_dir.mkdir(parents=True, exist_ok=True)

print("  Launching browser ...")
print("  Waiting for user to log in, then fetching tokens...")
with sync_playwright() as p:
    browser = p.chromium.launch_persistent_context(
        str(profile_dir),
        headless=False,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        viewport={"width": 1280, "height": 800},
    )

    page = browser.pages[0] if browser.pages else browser.new_page()
    page.goto(workspace_url)

    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except PWTimeout:
        pass  # continue anyway

    if any(x in page.url for x in ("signin", "sign_in", "ssb/signin")):
        print()
        print("  " + "=" * 56)
        print("  Log in to Slack in the browser window that just opened.")
        print()
        print("  The MCP server logs its activity to a Slack channel so you can")
        print("  monitor what it's doing. Navigate to any channel or DM you want")
        print("  to use (a self-DM or Slackbot DM works great) and note the")
        print("  channel ID from the URL:")
        print("    - Self-DM: click your own name in the left sidebar")
        print("    - Slackbot: click 'Slackbot' in the left sidebar")
        print("    - Channel:  click the channel name")
        print()
        print("    https://app.slack.com/client/TXXXXXXXX/DXXXXXXXXX")
        print("                                ^^^^^^^^^ team ID   ^^^^^^^^^^^ channel ID")
        print()
        print("  Then come back here.")
        print("  " + "=" * 56)
        print()
        while True:
            url_or_id = input("  Paste the full URL from the browser address bar: ").strip()
            # Accept a full URL or just a channel ID
            # /client/TEAM_ID/CHANNEL_ID (app.slack.com)
            url_match = re.search(r"/client/([A-Z0-9]+)/([A-Z0-9]+)", url_or_id)
            if url_match:
                user_team_id = url_match.group(1)
                channel_id = url_match.group(2)
                break
            # /archives/CHANNEL_ID (enterprise Slack, e.g. redhat.enterprise.slack.com)
            archives_match = re.search(r"/archives/([A-Z0-9]+)", url_or_id)
            if archives_match:
                channel_id = archives_match.group(1)
                user_team_id = None
                break
            # Fallback: bare channel ID (legacy behavior)
            bare = url_or_id.lstrip("/")
            if bare[:1] in ("C", "D", "G") and len(bare) >= 9:
                channel_id = bare
                user_team_id = None
                break
            print("  Could not parse that. Paste the full URL from the address bar, e.g.:")
            print("    https://app.slack.com/client/T01234567/D092BLQB0FR")
            print("    https://your-org.enterprise.slack.com/archives/D092BLQB0FR")
        print()
        answer = input("  Proceed with fetching Slack tokens? [Y/n] ").strip().lower()
        if answer not in ("", "y", "yes"):
            print("  Aborted.")
            browser.close()
            sys.exit(1)
        print()
        print("  Fetching tokens... The browser window will close in about a minute.")

        # Navigate the browser to the Slack client page so we can extract tokens
        if user_team_id:
            target_url = f"https://app.slack.com/client/{user_team_id}/{channel_id}"
        else:
            target_url = f"https://app.slack.com/client/"
        page.goto(target_url)
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except PWTimeout:
            pass
    else:
        user_team_id = None

    # Team ID — try multiple sources
    m = re.search(r"/client/([A-Z0-9]+)", page.url)
    team_id = m.group(1) if m else None

    if not team_id:
        # Try localStorage (multiple known keys)
        team_id = page.evaluate("""() => {
            try {
                // Try localConfig_v2 first
                const c = JSON.parse(localStorage.getItem('localConfig_v2') || '{}');
                const teams = Object.keys(c.teams || {});
                if (teams.length > 0) return teams[0];
            } catch {}
            try {
                // Try redux store
                const r = JSON.parse(localStorage.getItem('reduxStore') || '{}');
                if (r.teams) {
                    const teams = Object.keys(r.teams);
                    if (teams.length > 0) return teams[0];
                }
            } catch {}
            try {
                // Scan all localStorage keys for team IDs
                for (let i = 0; i < localStorage.length; i++) {
                    const key = localStorage.key(i);
                    const val = localStorage.getItem(key) || '';
                    const m = val.match(/"team_id"\\s*:\\s*"(T[A-Z0-9]+)"/);
                    if (m) return m[1];
                }
            } catch {}
            return null;
        }""")

    if not team_id and user_team_id:
        team_id = user_team_id

    if not team_id:
        print("  ERROR: Could not determine team ID. Is Slack fully loaded?")
        browser.close()
        sys.exit(1)

    print(f"  Found team ID: {team_id}")

    # Every workspace this login is signed in to. Slack keeps one xoxc token
    # per workspace in localConfig_v2.teams; teams with no token are signed out.
    accounts = page.evaluate("""() => {
        try {
            const c = JSON.parse(localStorage.localConfig_v2 || '{}');
            return Object.entries(c.teams || {}).map(([key, t]) => ({
                team_id: (t && t.id) || key || '',
                team_name: (t && t.name) || '',
                domain: (t && t.domain) || '',
                xoxc: (t && t.token) || '',
            })).filter(t => t.xoxc.startsWith('xoxc-') && t.team_id);
        } catch { return []; }
    }""") or []

    if not accounts:
        # Last-resort: a single token scraped from the page HTML.
        xoxc = page.evaluate("""() => {
            const m = document.body.innerHTML.match(/"token":"(xoxc-[^"]+)"/);
            return m ? m[1] : null;
        }""")
        if xoxc:
            accounts = [{"team_id": team_id, "team_name": "", "domain": "", "xoxc": xoxc}]

    if not accounts:
        print("  ERROR: Could not extract any XOXC token.")
        browser.close()
        sys.exit(1)

    # Put the workspace the browser is currently on first; it becomes the default.
    accounts.sort(key=lambda a: a["team_id"] != team_id)

    print(f"  ✓ Found {len(accounts)} workspace(s):")
    for a in accounts:
        print(f"      {a['domain'] or a['team_id']:<24} {a['team_name']}")

    # XOXD cookie
    xoxd = next(
        (c["value"] for c in browser.cookies()
         if c["name"] == "d" and "slack.com" in c["domain"]),
        None,
    )

    if not xoxd:
        print("  ERROR: Could not extract XOXD token (cookie 'd' not found).")
        browser.close()
        sys.exit(1)

    print("  ✓ Found XOXD token (shared by every workspace)")
    browser.close()

# Hand the raw findings back to the parent, which owns the on-disk format.
Path(payload_file).write_text(json.dumps({"xoxd": xoxd, "accounts": accounts}))
os.chmod(payload_file, 0o600)

if channel_id:
    Path(channel_id_file).write_text(channel_id)
'''


def extract_tokens(python: Path, workspace_url: str, refresh: bool) -> tuple:
    """Returns (logs_channel_id_from_browser, payload_or_None)."""
    banner("Extracting Slack session tokens")

    if tokens_exist() and not refresh:
        print(f"  ✓ Tokens already exist at {WORKSPACES_FILE}")
        print("    Run with --refresh-tokens to re-extract.")
        return "", None

    print("  A browser window is about to open. Before it does, read the directions below:")
    print()
    print("    1. Log in to Slack when the browser opens. Every workspace this")
    print("       login is signed in to will be picked up, so sign in to all of")
    print("       them if you want Claude to reach them all.")
    print("    2. The MCP server logs its activity to a Slack channel so you can")
    print("       monitor what it's doing. Navigate to the channel or DM you want")
    print("       to use (a self-DM or Slackbot DM works great) and note the")
    print("       channel ID from the URL:")
    print("         https://app.slack.com/client/TXXXXXXXX/DXXXXXXXXX")
    print("                                                ^^^^^^^^^^^^ this part")
    print("       (IDs start with C, D, or G)")
    print("    3. Return here — you will be prompted to enter the channel ID")
    print("       and confirm before tokens are fetched")
    print()
    answer = input("  Continue? [Y/n] ").strip().lower()
    if answer not in ("", "y", "yes"):
        print("  Aborted.")
        sys.exit(0)
    print()

    extract_file = INSTALL_DIR / "_extract_tokens.py"
    extract_file.write_text(_EXTRACT_SCRIPT)

    channel_id_file = INSTALL_DIR / "channel_id.tmp"
    channel_id_file.unlink(missing_ok=True)
    payload_file = INSTALL_DIR / "workspaces.payload.json"
    payload_file.unlink(missing_ok=True)

    try:
        run([
            str(python),
            str(extract_file),
            workspace_url,
            str(payload_file),
            str(PROFILE_DIR),
            str(channel_id_file),
        ])
    except subprocess.CalledProcessError:
        print("\n  Token extraction failed. See output above.")
        sys.exit(1)
    finally:
        extract_file.unlink(missing_ok=True)

    payload = None
    if payload_file.exists():
        try:
            payload = json.loads(payload_file.read_text())
        except ValueError:
            print("  Warning: could not read the extracted token payload.")
        finally:
            payload_file.unlink(missing_ok=True)

    channel_id = ""
    if channel_id_file.exists():
        channel_id = channel_id_file.read_text().strip()
        channel_id_file.unlink()
    return channel_id, payload


def save_workspaces(payload: dict, logs_channel: str) -> dict:
    """Merge the extracted workspaces into workspaces.json and tokens.env."""
    banner("Writing workspace configuration")

    config = merge_workspaces_config(
        read_workspaces(WORKSPACES_FILE),
        (payload or {}).get("accounts") or [],
        (payload or {}).get("xoxd") or "",
        logs_channel=logs_channel,
    )
    accounts = config.get("accounts") or {}
    if not accounts:
        print("  No workspaces configured yet — nothing to write.")
        return config

    write_private(WORKSPACES_FILE, json.dumps(config, indent=2) + "\n")
    default_alias = config.get("default", "")
    print(f"  ✓ Written: {WORKSPACES_FILE}")
    for alias, entry in accounts.items():
        marker = "  [default]" if alias == default_alias else ""
        print(f"      {alias:<24} {entry.get('team_id', ''):<14}{marker}")

    # tokens.env still carries the default workspace, for setups (and container
    # images) that predate multi-workspace support.
    entry = accounts.get(default_alias) or {}
    if entry.get("xoxc") and config.get("xoxd"):
        write_private(
            TOKENS_FILE,
            "# Slack session tokens (auto-generated by setup-slack-mcp.py)\n"
            f"# Team ID: {entry.get('team_id', '')}\n\n"
            f"SLACK_MCP_XOXC_TOKEN={entry['xoxc']}\n"
            f"SLACK_MCP_XOXD_TOKEN={config['xoxd']}\n",
        )
        print(f"  ✓ Written: {TOKENS_FILE} (default workspace only)")

    return config


def write_wrapper(logs_channel: str) -> None:
    banner("Writing MCP wrapper script")

    runtime = "podman" if shutil.which("podman") else "docker"

    # Mount the data directory rather than passing tokens as env vars: the
    # server reads workspaces.json and tokens.env from there itself, and writes
    # its per-workspace caches back. :Z relabels for SELinux under podman.
    mount = f"{INSTALL_DIR}:/data" + (":Z" if runtime == "podman" else "")

    # Build env args as a list so there are no stray line-continuation issues
    env_lines = [
        f'  -v "{mount}" \\',
        "  -e SLACK_MCP_DATA=/data \\",
        "  -e MCP_TRANSPORT=stdio \\",
    ]
    if logs_channel:
        # Only used when no workspaces.json is present; otherwise each account
        # carries its own logs_channel.
        env_lines.append(f'  -e LOGS_CHANNEL_ID="{logs_channel}" \\')

    env_block = "\n".join(env_lines)

    content = f"""\
#!/usr/bin/env bash
# Slack MCP wrapper — auto-generated by setup-slack-mcp.py
# Re-run setup-slack-mcp.py to regenerate or update this file.
set -euo pipefail

DATA="{INSTALL_DIR}"

if [[ ! -f "$DATA/workspaces.json" && ! -f "$DATA/tokens.env" ]]; then
  echo "Error: no Slack tokens found in $DATA." >&2
  echo "Re-run: python3 setup-slack-mcp.py --refresh-tokens" >&2
  exit 1
fi

exec {runtime} run -i --rm \\
{env_block}
  {MCP_IMAGE}
"""
    WRAPPER_SCRIPT.write_text(content)
    WRAPPER_SCRIPT.chmod(0o755)
    print(f"  ✓ Written: {WRAPPER_SCRIPT}")


def register_mcp() -> None:
    banner("Registering MCP server in Claude Code settings")

    settings: dict = {}
    if CLAUDE_SETTINGS.exists():
        try:
            settings = json.loads(CLAUDE_SETTINGS.read_text())
        except json.JSONDecodeError:
            print(f"  Warning: could not parse existing {CLAUDE_SETTINGS}, will overwrite.")

    settings.setdefault("mcpServers", {})[MCP_SERVER_NAME] = {
        "type": "stdio",
        "command": str(WRAPPER_SCRIPT),
        "args": [],
        "env": {},
    }

    CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"  ✓ Registered '{MCP_SERVER_NAME}' in {CLAUDE_SETTINGS}")


def prompt_logs_channel() -> str:
    """Interactively guide the user to find and enter their Slack logs channel ID."""
    banner("Slack Logs Channel ID (optional)")

    print("""  The MCP server can mirror tool activity to a Slack channel.
  Any channel works — a self-DM or DM with Slackbot is easiest.
  Press Enter to skip and log to stderr only.
""")

    while True:
        channel_id = input("  Enter channel ID (or press Enter to skip): ").strip()
        if not channel_id:
            return ""
        if channel_id[:1] in ("C", "D", "G") and len(channel_id) >= 9:
            return channel_id
        print("  Not a valid Slack ID (expected C/D/G followed by digits). Try again.")


def verify() -> None:
    """Quick smoke-test: run the MCP server and send a JSON-RPC ping."""
    banner("Verifying MCP server")

    def msg(**kwargs) -> bytes:
        return (json.dumps(kwargs) + "\n").encode()

    payload = (
        msg(jsonrpc="2.0", id=1, method="initialize", params={
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "setup-slack-mcp", "version": "1.0"},
        })
        + msg(jsonrpc="2.0", method="notifications/initialized", params={})
        + msg(jsonrpc="2.0", id=2, method="tools/list", params={})
    )

    result = subprocess.run(
        [str(WRAPPER_SCRIPT)],
        input=payload,
        capture_output=True,
        timeout=30,
    )

    if result.returncode == 0 and b"tools" in result.stdout:
        print("  ✓ MCP server responded successfully")
    else:
        print("  ⚠  Could not verify MCP server. It may still work — check the tokens if you see issues.")
        if result.stderr:
            print(f"  stderr: {result.stderr.decode()[:300]}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Set up the Slack MCP plugin for Claude Code",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 setup-slack-mcp.py
  python3 setup-slack-mcp.py --refresh-tokens
  python3 setup-slack-mcp.py --set-logs-channel C01234567
  python3 setup-slack-mcp.py --workspace https://myco.slack.com
        """,
    )
    parser.add_argument(
        "--refresh-tokens",
        action="store_true",
        help="Re-extract Slack tokens even if they already exist",
    )
    parser.add_argument(
        "--set-logs-channel",
        default="",
        metavar="CHANNEL_ID",
        help="Slack channel ID for MCP server log output (optional; prompted interactively if not provided, press Enter to skip)",
    )
    parser.add_argument(
        "--workspace",
        default="",
        metavar="URL",
        help=f"Slack workspace URL to open in the browser (default: {DEFAULT_WORKSPACE})",
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="Skip the smoke-test after setup",
    )
    args = parser.parse_args()

    print()
    print("╔═══════════════════════════════════════════════════════════╗")
    print("║       Slack MCP Setup for Claude Code                     ║")
    print("╚═══════════════════════════════════════════════════════════╝")
    print()
    print("  One manual step required: log in to Slack when the")
    print("  browser opens. Everything else is automatic.")

    check_prerequisites()
    python = setup_venv()
    pull_image()
    channel_id_from_browser, payload = extract_tokens(
        python, args.workspace or DEFAULT_WORKSPACE, refresh=args.refresh_tokens
    )
    logs_channel = args.set_logs_channel or channel_id_from_browser or prompt_logs_channel()
    config = save_workspaces(payload, logs_channel)
    write_wrapper(logs_channel)
    register_mcp()

    if not args.skip_verify:
        verify()

    print()
    print("╔═══════════════════════════════════════════════════════════╗")
    print("║  Setup complete!                                          ║")
    print("╚═══════════════════════════════════════════════════════════╝")
    print()
    aliases = list((config.get("accounts") or {}).keys())
    print(f"  MCP server name : {MCP_SERVER_NAME}")
    print(f"  Wrapper script  : {WRAPPER_SCRIPT}")
    print(f"  Workspaces      : {WORKSPACES_FILE}")
    if aliases:
        print(f"                    {', '.join(aliases)} (default: {config.get('default', '')})")
    print(f"  Claude settings : {CLAUDE_SETTINGS}")
    print()
    print("  Start a new Claude Code session to activate the plugin.")
    print("  Test it by asking: 'What are my Slack workspaces?'")
    if len(aliases) > 1:
        print("  Then try: 'Set my Slack status to :calendar: In a meeting everywhere'")
    print()
    print("  To refresh tokens when they expire:")
    print("    python3 setup-slack-mcp.py --refresh-tokens")
    print()


if __name__ == "__main__":
    main()
