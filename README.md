# slack-mcp

A Model Context Protocol (MCP) server that enables AI assistants to interact with Slack workspaces. This server provides a bridge between AI tools and Slack, allowing you to read messages, post content, and manage Slack channels programmatically through MCP-compatible clients.

Disclaimer: This is not an official Red Hat tool.

## What is this and why should I use it?

This MCP server transforms your Slack workspaces into an AI-accessible environment. It provides 27 tools for comprehensive Slack interaction, across one workspace or several at once:

**Message & Thread Operations**

- Read channel history with date filtering and thread support
- Post messages and replies to threads
- Search messages across workspace or within specific channels
- Execute Slack commands

**Channel Management**

- List, join, create, and rename channels
- Look up channel IDs by name
- Invite users to channels

**Reactions & Users**

- Add and view emoji reactions
- Send direct messages and group DMs
- Manage usergroups (clear members)

**Status & Presence**

- Set, clear, and read your status across every workspace at once
- Set presence (auto/away) across every workspace at once

**Utility**

- Check authentication status
- List configured workspaces and verify their tokens
- Per-workspace cache management for performance

### Key Benefits

- **Multiple Workspaces**: Hold several Slack accounts at once and act in one, some, or all of them
- **Seamless Integration**: Connect your AI assistant directly to Slack without manual copy-pasting
- **Automated Workflows**: Build AI-powered Slack bots that can read, analyze, and respond to messages
- **Enhanced Productivity**: Let AI help manage notifications, summarize conversations, or automate routine Slack tasks
- **Real-time Collaboration**: Enable AI assistants to participate in team discussions and provide instant insights

### Use Cases

- **Team Assistant**: Have an AI that can read team updates and provide summaries
- **Notification Manager**: Automatically categorize and respond to incoming messages
- **Knowledge Base**: AI that can search through channel history and provide context
- **Meeting Scheduler**: AI that can read meeting requests and help coordinate schedules

## Setting Up with Claude Code

This repo ships as a Claude Code plugin with a guided setup skill. Claude will walk you through the entire process — no manual config editing required.

Run the setup script. It handles everything — venv, Playwright, token extraction, wrapper script, and Claude Code registration. The only interaction required is logging in to Slack when the browser opens, and entering an optional channel ID for server logs, if desired.

It extracts **every workspace that login is signed in to**, not just one, and writes them to `workspaces.json`. Sign in to all the workspaces you want Claude to reach before returning to the terminal.

```bash
python3 <(curl -fsSL https://raw.githubusercontent.com/redhat-community-ai-tools/slack-mcp/main/scripts/setup-slack-mcp.py)
```

Or clone the repo first and run it locally:

```bash
git clone https://github.com/redhat-community-ai-tools/slack-mcp
python3 slack-mcp/scripts/setup-slack-mcp.py
```

**Options:**


| Flag                                 | Description                                                               |
| ------------------------------------ | ------------------------------------------------------------------------- |
| `--logs-channel DXXXXXXXXX`          | Slack channel ID for server logs (optional; logs go to stderr if omitted) |
| `--workspace https://myco.slack.com` | Specific Slack workspace to open                                          |
| `--refresh-tokens`                   | Re-extract tokens when they expire (skips all other steps)                |
| `--skip-verify`                      | Skip the post-setup smoke test                                            |

> **Note:** the wrapper this writes runs the published container image, which
> only gains multi-workspace support once it is rebuilt from a tree that has it.
> Point it at a local build with `SLACK_MCP_IMAGE=...`, or install the server
> directly with [uv](#running-as-a-uv-tool-local-no-container).


When tokens expire, just run:

```bash
python3 slack-mcp/scripts/setup-slack-mcp.py --refresh-tokens
```

### Desktop App Token Refresh (Linux)

If you have the [Slack desktop app](https://slack.com/downloads/linux) installed, you can refresh tokens without opening a browser:

```bash
slack-mcp/scripts/slack-refresh-tokens --validate
```

This reads tokens directly from the desktop app's local storage on disk — no DevTools, no Playwright, no manual steps. Requires the Slack app to be signed in.

Every signed-in workspace is extracted: Slack keeps one `xoxc` token per workspace in `localConfig_v2.teams`, alongside the single `d` cookie they all share.


| Flag                | Description                                                                              |
| ------------------- | ---------------------------------------------------------------------------------------- |
| `--validate`        | Verify every workspace's token against Slack's API after extraction                      |
| `--env`             | Print the default workspace's tokens as env vars to stdout (for piping into other tools) |
| `--output FILE`     | Write `tokens.env` to a custom path (default: `~/.local/share/slack-mcp/tokens.env`)     |
| `--workspaces FILE` | Write `workspaces.json` to a custom path (default: `<data>/workspaces.json`)             |
| `--default ALIAS`   | Make `ALIAS` the default workspace                                                       |
| `--slack-dir DIR`   | Override the auto-detected Slack data directory                                          |


Both files are written with `0600` permissions. `tokens.env` keeps carrying the
default workspace alone, so older setups and container images keep working.

**Requirements:** `python3`, `python3-cryptography`, `secret-tool` (libsecret/gnome-keyring), `curl`

This is useful for CI hooks or session startup scripts that need to silently refresh tokens before launching the MCP server.

---

## Bot token authentication (recommended)

For better security, use a Slack App bot token (`xoxb-`) instead of browser session tokens. Bot tokens provide:

- **Scoped access** — only the OAuth permissions you grant, not full user access
- **Distinct identity** — actions appear as the bot, not as your user account
- **Central management** — IT can audit and revoke via the Slack admin panel
- **No browser DevTools** — tokens are generated once in the Slack App settings

### Setup

1. Create a Slack App at [api.slack.com/apps](https://api.slack.com/apps)
2. Add OAuth scopes: `channels:read`, `channels:history`, `channels:manage`, `groups:read`, `groups:history`, `groups:write`, `chat:write`, `reactions:read`, `reactions:write`, `search:read`, `users:read`, `commands`, `mpim:write`
3. Install to your workspace and copy the Bot User OAuth Token (`xoxb-...`)
4. Invite the bot to channels it needs access to

### Running with a bot token

Set `SLACK_BOT_TOKEN` instead of `SLACK_XOXC_TOKEN`/`SLACK_XOXD_TOKEN`:

```json
{
  "mcpServers": {
    "slack": {
      "command": "podman",
      "args": [
        "run", "-i", "--rm",
        "-e", "SLACK_BOT_TOKEN",
        "-e", "LOGS_CHANNEL_ID",
        "quay.io/redhat-ai-tools/slack-mcp"
      ],
      "env": {
        "SLACK_BOT_TOKEN": "xoxb-...",
        "LOGS_CHANNEL_ID": "C7000000"
      }
    }
  }
}
```

`LOGS_CHANNEL_ID` is optional. When omitted, tool activity is written to **stderr** instead of posted to Slack.

If both `SLACK_BOT_TOKEN` and `SLACK_XOXC_TOKEN`/`SLACK_XOXD_TOKEN` are set, the bot token takes precedence.

## Multiple workspaces

The server can hold several Slack workspaces at once and act in any of them.
Both token extraction tools write this file for you — see
[Setting Up with Claude Code](#setting-up-with-claude-code) and
[Desktop App Token Refresh](#desktop-app-token-refresh-linux). It lives at
`~/.local/share/slack-mcp/workspaces.json` (override with
`SLACK_MCP_WORKSPACES_FILE`) and looks like this:

```json
{
  "default": "work",
  "xoxd": "xoxd-...",
  "accounts": {
    "work":      { "team_id": "T0000001", "team_name": "Work",      "xoxc": "xoxc-...", "logs_channel": "C7000000" },
    "community": { "team_id": "T0000002", "team_name": "Community", "xoxc": "xoxc-..." },
    "ci":        { "team_id": "T0000003", "bot_token": "xoxb-..." }
  }
}
```

One Slack login shares a single `d` cookie across every workspace it is signed
into, so `xoxd` is declared once at the top level; a per-account `xoxd` (or
`bot_token`) overrides it. Each account gets its own `xoxc`.

- `default` names the account used when a tool is given no `workspace`. If it
  is missing or unknown, the first account wins.
- Each account caches channels and users separately, under
  `~/.local/share/slack-mcp/accounts/<team_id>/`. Channel names and user IDs are
  workspace-scoped, so a shared cache would resolve `#general` to whichever
  workspace was cached first — and `create_channel` sends each workspace's own
  `team_id`, which Enterprise Grid requires.
- **When `workspaces.json` exists, it is the only source of accounts** —
  `SLACK_BOT_TOKEN` and `SLACK_XOXC_TOKEN`/`SLACK_XOXD_TOKEN` in the environment
  are ignored, and the server logs a line saying so. Without the file, those
  variables define a single account called `default`, exactly as before.

Aliases come from each workspace's Slack domain, so `acme.slack.com` becomes
`acme`. Run `list_workspaces` to see them, or `list_workspaces(verify=true)` to
also check each token against `auth.test`.

Re-running either extractor matches accounts on team ID, so it refreshes tokens
in place: it never renames a workspace, and it keeps settings you added by hand
(`logs_channel`, a `bot_token`). Workspaces it does not find — a bot-token-only
account has no `xoxc` in local storage — are left untouched.

### Selecting workspaces

Every tool takes a `workspace` argument (the one exception is
`list_workspaces`, which always reports them all):

| Value | Meaning |
| --- | --- |
| omitted / `""` | the `default` account |
| `"work"` | one account, by alias, team ID, or team name (case-insensitive) |
| `"all"` or `"*"` | every configured account |
| `"work,community"` | several, in the order given |

An unknown name is rejected before any API call rather than silently skipped.

**Most tools act in one workspace.** Posting a message, joining a channel or
renaming one happens somewhere specific, so those tools reject `"all"` with an
error naming the workspaces it matched rather than guessing. Reading is the same:
channel IDs and user IDs only mean anything inside their own workspace.

**Fan-out is opt-in per tool**, and limited to operations where "everywhere" is
the obvious intent: the status and presence tools (which default to `"all"`), and
`refresh_channel_cache` / `refresh_user_cache`. Those run the workspaces
concurrently and return a per-workspace map, so one expired token does not stop
the rest:

```json
{
  "work":      { "ok": true, "status_text": "Heads down", "status_emoji": ":no_bell:", "status_expiration": 0 },
  "community": { "ok": false, "error": "token_expired" }
}
```

Nested calls stay put: when the activity log posts to a workspace's
`logs_channel` during a fan-out, it posts to *that* workspace, not the default.

## Status across every workspace

`set_status` defaults to `workspace: "all"` — one call updates your status
everywhere:

```
set_status(text="In a meeting", emoji=":calendar:", expiration="1h")
```

- `emoji` takes either form, `:calendar:` or `calendar`.
- `expiration` accepts a relative offset (`30m`, `2h`, `1d`), an ISO 8601
  date/time, or a Unix timestamp. Omit it and the status stays until cleared.
- `clear_status` wipes text and emoji; `get_status` reads back what each
  workspace currently shows.
- `set_presence("away" | "auto")` fans out the same way.

Two things worth knowing:

**Status requires user tokens.** A bot token can only change the bot's own
profile, never yours, so `set_status`, `clear_status`, and `set_presence` refuse
any `xoxb-` account with a clear error instead of a confusing one from Slack.
Workspaces configured with a bot token are listed by `list_workspaces` with
`can_set_status: false`.

**Custom emoji do not cross workspaces.** `:team-standup:` set everywhere will
render blank wherever it is not defined. When an emoji exists as a custom emoji
in one of your workspaces but not another, the result for that workspace carries
a `warning`. Standard Slack emoji never warn.

## Read-only mode

For agents or automation that should **browse and search** Slack without posting, reacting, running commands, or joining channels, enable read-only mode.

- **Environment variable:** set `SLACK_MCP_READ_ONLY` to a truthy value (`1`, `true`, `yes`, or `on`, case-insensitive).
- **CLI:** pass `--read-only` when starting the server (e.g. `slack-mcp --read-only`, equivalent to setting the variable).

In read-only mode, tools that mutate Slack state (`post_message`, `send_dm`, `post_command`, `add_reaction`, `join_channel`) raise a clear error. Read tools (history, search, threads, `whoami`, channel listing, cache refresh helpers, and so on) behave as usual. Tool activity that would normally be mirrored to `LOGS_CHANNEL_ID` is written to **stderr** instead.

On startup, the server logs a line to stderr when read-only mode is active.

For Podman or Docker, add `-e SLACK_MCP_READ_ONLY=true` (and the matching key in `env`) when you want the container to run read-only.

## Running as a uv tool (local, no container)

Prefer not to run a container? Install slack-mcp as a [uv](https://docs.astral.sh/uv/) tool. This puts a `slack-mcp` command on your PATH that runs the server directly.

Requires uv and Python ≥ 3.10.

```bash
# from a clone of this repo
uv tool install .

# or straight from git
uv tool install git+https://github.com/redhat-community-ai-tools/slack-mcp
```

Run `slack-mcp --help` for a summary of flags and environment variables.

### Tokens from `tokens.env` (recommended)

`slack-mcp` reads Slack session tokens from `~/.local/share/slack-mcp/tokens.env` when they are not already set in the environment — the same file the token tooling writes:

```bash
scripts/slack-refresh-tokens          # or: python3 scripts/setup-slack-mcp.py
```

Then the MCP client config needs **no `env` block** — just the command:

```json
{
  "mcpServers": {
    "slack": {
      "command": "slack-mcp"
    }
  }
}
```

The file uses `SLACK_MCP_XOXC_TOKEN` / `SLACK_MCP_XOXD_TOKEN`; the server maps those onto the `SLACK_XOXC_TOKEN` / `SLACK_XOXD_TOKEN` it uses. Override the path with `SLACK_MCP_TOKENS_FILE`. **Real environment variables always take precedence over the file**, so you can still override per-client.

This path configures a single workspace. When `workspaces.json` is present it wins, and both `tokens.env` and the environment are ignored for account configuration — see [Multiple workspaces](#multiple-workspaces).

### Tokens from the client config (alternative)

To keep tokens in the MCP config instead of a file, pass them in `env`:

```json
{
  "mcpServers": {
    "slack": {
      "command": "slack-mcp",
      "env": {
        "SLACK_XOXC_TOKEN": "xoxc-...",
        "SLACK_XOXD_TOKEN": "xoxd-...",
        "LOGS_CHANNEL_ID": "C7000000"
      }
    }
  }
}
```

A bot token works the same way — set `SLACK_BOT_TOKEN` in `env` instead of the `xoxc`/`xoxd` pair. If the client does not inherit your PATH, use the absolute path (`~/.local/bin/slack-mcp` after `uv tool install`), or launch via `uv run --directory /path/to/slack-mcp slack-mcp`.

The user cache and the default `tokens.env` live under `~/.local/share/slack-mcp/` (override with `SLACK_MCP_DATA`).

Claude Code (bot token):

```bash
claude mcp add slack -e SLACK_BOT_TOKEN=xoxb-... -- slack-mcp
```

Or, with `tokens.env` already in place, no env needed:

```bash
claude mcp add slack -- slack-mcp
```

## Running with Podman or Docker

You can run the slack-mcp server in a container using Podman or Docker:

Example configuration for running with Podman:

```json
{
  "mcpServers": {
    "slack": {
      "command": "podman",
      "args": [
        "run",
        "-i",
        "--rm",
        "-e", "SLACK_XOXC_TOKEN",
        "-e", "SLACK_XOXD_TOKEN",
        "-e", "MCP_TRANSPORT",
        "-e", "LOGS_CHANNEL_ID",
        "quay.io/redhat-ai-tools/slack-mcp"
      ],
      "env": {
        "SLACK_XOXC_TOKEN": "xoxc-...",
        "SLACK_XOXD_TOKEN": "xoxd-...",
        "MCP_TRANSPORT": "stdio",
        "LOGS_CHANNEL_ID": "C7000000"
      }
    }
  }
}
```

`LOGS_CHANNEL_ID` is optional. When omitted, tool activity is written to **stderr** instead of posted to Slack.

## Activity logging

By default, tool activity is written to **stderr** (visible in your terminal or process logs). To mirror activity to a Slack channel instead, set `LOGS_CHANNEL_ID` to any channel the bot or session user has access to — a self-DM or a DM with Slackbot works well for personal use.

```bash
LOGS_CHANNEL_ID=C7000000
```

In read-only mode, `LOGS_CHANNEL_ID` is ignored and all activity is always written to stderr.

## Running with non-stdio transport

To run the server with a non-stdio transport (such as SSE), set the `MCP_TRANSPORT` environment variable to a value other than `stdio` (e.g., `sse`).

Example configuration to connect to a non-stdio MCP server:

```json
{
  "mcpServers": {
    "slack": {
      "url": "https://slack-mcp.example.com/sse",
      "headers": {
        "X-Slack-Web-Token": "xoxc-...",
        "X-Slack-Cookie-Token": "xoxd-..."
      }
    }
  }
}
```

Extract your Slack XOXC and XOXD tokens easily using browser extensions or Selenium automation: [https://github.com/maorfr/slack-token-extractor](https://github.com/maorfr/slack-token-extractor).