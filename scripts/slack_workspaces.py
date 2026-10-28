#!/usr/bin/env python3
"""Shared helpers for turning extracted Slack tokens into workspaces.json.

The Slack desktop app and the browser both keep one `xoxc` token per workspace
in a `localConfig_v2.teams` map, and a single `d` cookie shared across every
workspace that login is signed into. This module converts that shape into the
config slack-mcp reads, and merges it into any file already on disk.

Standard library only: it is imported by scripts that run before slack-mcp (and
its dependencies) are installed.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

__all__ = [
    "accounts_from_teams",
    "alias_for",
    "extract_local_config",
    "find_teams_by_proximity",
    "merge_workspaces",
    "read_workspaces",
    "write_tokens_env",
    "write_workspaces",
]

TEAM_ID = r"T[A-Z0-9]{6,}"


# ── Parsing localConfig_v2 out of raw bytes ──────────────────────────────────

def _decode_candidates(blob: bytes) -> Iterator[str]:
    """Yield plausible text decodings of a leveldb value.

    Chromium stores localStorage values with a one-byte prefix: 0x01 for
    single-byte text, 0x00 for UTF-16LE. That prefix also shifts UTF-16 text
    off even-byte alignment, and the slice we get here may start anywhere, so
    both alignments are tried.
    """
    yield blob.decode("latin-1", errors="replace")
    for offset in (0, 1):
        try:
            yield blob[offset:].decode("utf-16-le", errors="replace")
        except (UnicodeDecodeError, LookupError):  # pragma: no cover - defensive
            pass


def _match_object(text: str, start: int) -> str | None:
    """Return the JSON object beginning at `start`, honouring strings/escapes."""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None  # truncated across a block boundary


def _parse_at(text: str, start: int) -> dict[str, Any] | None:
    """Parse the object starting at `start`, if it is one and has teams."""
    candidate = _match_object(text, start)
    if not candidate:
        return None  # truncated across a block boundary
    try:
        parsed = json.loads(candidate)
    except ValueError:
        return None
    if isinstance(parsed, dict) and parsed.get("teams"):
        return parsed
    return None


def _object_starts(text: str, before: int, limit: int = 8) -> Iterator[int]:
    """Candidate '{' positions at or before `before`, nearest first."""
    found = 0
    i = text.rfind("{", 0, before + 1)
    while i >= 0 and found < limit:
        yield i
        found += 1
        i = text.rfind("{", 0, i)


def extract_local_config(blob: bytes) -> dict[str, Any] | None:
    """Parse the localConfig_v2 object out of a raw storage blob.

    Returns the decoded object, or None when it cannot be recovered intact —
    leveldb may have split it across blocks, in which case the caller should
    fall back to find_teams_by_proximity.
    """
    for text in _decode_candidates(blob):
        # Preferred: anchor on the key, then take the object that follows.
        for match in re.finditer(r"localConfig_v2", text):
            brace = text.find("{", match.end())
            if brace >= 0 and (parsed := _parse_at(text, brace)) is not None:
                return parsed

        # Chromium stores a localStorage key single-byte and its value as
        # UTF-16LE when it contains non-Latin-1 text, so the key and the JSON
        # can decode cleanly in different candidates. Anchor on the payload.
        for match in re.finditer(r'"teams"\s*:', text):
            for start in _object_starts(text, match.start()):
                if (parsed := _parse_at(text, start)) is not None:
                    return parsed
    return None


def find_teams_by_proximity(blob: bytes, window: int = 4000) -> dict[str, dict[str, Any]]:
    """Recover team records without parsing the whole config.

    Used when localConfig_v2 is truncated. For each xoxc token, the enclosing
    team is the nearest team ID behind it; `name` and `domain` are picked up
    from the same neighbourhood when present. Field order inside a team record
    is not guaranteed, so this looks both ways.
    """
    teams: dict[str, dict[str, Any]] = {}
    for text in _decode_candidates(blob):
        for match in re.finditer(r"xoxc-[A-Za-z0-9-]+", text):
            token = match.group(0)
            before = text[max(0, match.start() - window) : match.start()]
            ids = list(re.finditer(rf'"({TEAM_ID})"', before))
            if not ids:
                continue
            last = ids[-1]
            team_id = last.group(1)
            if team_id in teams:
                continue
            # Bound the neighbourhood to this team's own record: it starts at
            # the team ID we just matched, so anything earlier belongs to the
            # previous team and would hand us its name and domain.
            around = before[last.start() :] + text[match.end() : match.end() + window]
            entry: dict[str, Any] = {"id": team_id, "token": token}
            if (name := re.search(r'"name"\s*:\s*"([^"]{1,80})"', around)):
                entry["name"] = name.group(1)
            if (domain := re.search(r'"domain"\s*:\s*"([a-z0-9][a-z0-9-]{0,60})"', around)):
                entry["domain"] = domain.group(1)
            teams[team_id] = entry
    return teams


# ── Normalising teams into account records ──────────────────────────────────

def alias_for(team: dict[str, Any], taken: set[str]) -> str:
    """A short, stable alias for a workspace, unique within `taken`.

    Accepts either a raw localConfig_v2 team (name/id) or a normalised account
    record (team_name/team_id).
    """
    raw = str(
        team.get("domain")
        or team.get("team_name")
        or team.get("name")
        or team.get("team_id")
        or team.get("id")
        or "workspace"
    )
    alias = re.sub(r"[^a-z0-9_-]", "-", raw.lower()).strip("-") or "workspace"
    if alias not in taken:
        return alias
    for suffix in range(2, 100):
        candidate = f"{alias}-{suffix}"
        if candidate not in taken:
            return candidate
    return f"{alias}-{team.get('team_id') or team.get('id') or 'x'}".lower()


def accounts_from_teams(teams: dict[str, Any] | None) -> list[dict[str, str]]:
    """Turn a localConfig_v2 teams map into account records, best first.

    Only workspaces with a usable xoxc token are returned; signed-out teams
    linger in that map with no token.
    """
    records: list[dict[str, str]] = []
    for key, team in (teams or {}).items():
        if not isinstance(team, dict):
            continue
        token = str(team.get("token") or "")
        if not token.startswith("xoxc-"):
            continue
        team_id = str(team.get("id") or key or "")
        if not re.fullmatch(TEAM_ID, team_id):
            continue
        records.append(
            {
                "team_id": team_id,
                "team_name": str(team.get("name") or ""),
                "domain": str(team.get("domain") or ""),
                "xoxc": token,
            }
        )
    records.sort(key=lambda r: (r["domain"] or r["team_name"] or r["team_id"]).lower())
    return records


# ── workspaces.json ─────────────────────────────────────────────────────────

def read_workspaces(path: Path) -> dict[str, Any]:
    """Load an existing config, or an empty one when absent or unreadable."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def merge_workspaces(
    existing: dict[str, Any],
    accounts: list[dict[str, str]],
    xoxd: str,
    default: str | None = None,
) -> dict[str, Any]:
    """Fold freshly extracted accounts into an existing config.

    Accounts are matched on team ID, so re-running never renames a workspace or
    drops settings added by hand (logs_channel, a bot_token). Workspaces the
    extractor did not see are preserved untouched — a bot-token-only account has
    no xoxc to find.
    """
    merged = dict(existing)
    current: dict[str, Any] = dict(merged.get("accounts") or {})

    by_team: dict[str, str] = {}
    for alias, entry in current.items():
        if isinstance(entry, dict) and entry.get("team_id"):
            by_team[str(entry["team_id"])] = alias

    for record in accounts:
        alias = by_team.get(record["team_id"]) or alias_for(record, set(current))
        entry = dict(current.get(alias) or {})
        entry["team_id"] = record["team_id"]
        if record["team_name"]:
            entry["team_name"] = record["team_name"]
        entry["xoxc"] = record["xoxc"]
        # A per-account xoxd would shadow the shared one we are about to write.
        entry.pop("xoxd", None)
        entry.pop("xoxd_token", None)
        current[alias] = entry
        by_team[record["team_id"]] = alias

    merged["accounts"] = current
    if xoxd:
        merged["xoxd"] = xoxd

    chosen = default or merged.get("default")
    if chosen not in current:
        chosen = next(iter(current), None)
    if chosen:
        merged["default"] = chosen

    # Keep the keys in a predictable order for a readable, diffable file.
    return {
        key: merged[key]
        for key in ("default", "xoxd", "accounts", *sorted(set(merged) - {"default", "xoxd", "accounts"}))
        if key in merged
    }


def _write_private(path: Path, text: str) -> None:
    """Write a 0600 file, creating it with those permissions from the start."""
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


def write_workspaces(path: Path, config: dict[str, Any]) -> None:
    """Persist workspaces.json with owner-only permissions."""
    _write_private(Path(path), json.dumps(config, indent=2) + "\n")


def write_tokens_env(path: Path, xoxc: str, xoxd: str, team_id: str = "", source: str = "") -> None:
    """Write the single-workspace tokens.env that older setups still read."""
    lines = [
        f"# Slack session tokens (auto-generated{f' by {source}' if source else ''})",
        f"# Generated: {datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')}",
    ]
    if team_id:
        lines.append(f"# Team ID: {team_id}")
    lines += ["", f"SLACK_MCP_XOXC_TOKEN={xoxc}", f"SLACK_MCP_XOXD_TOKEN={xoxd}", ""]
    _write_private(Path(path), "\n".join(lines))
