#!/usr/bin/env python3
"""LP Slack CLI — агент-оптимизированный wrapper над slack_sdk + xoxc/xoxd токенами.

Principles:
- URL = first-class input (parse channel+ts, агент не таскает channel_id)
- Minimal stdout (ts/url/dest), raw JSON только с --json
- Notify self on channel-post (public/private, top-level+thread); silent для DM/MPIM
- Auto-split >4k (Slack hard limit), сохраняет code-fences
- Audit log ~/.cache/slack-cli/audit.jsonl
- Auto-refresh токенов при 401 (LastPass cookie-sync; CDP fallback via env)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# ---------------------------------------------------------------------------
# Paths / state
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
EXTRACT_SCRIPT = ROOT / "extract_tokens.py"

CACHE_DIR = Path.home() / ".cache" / "slack-cli"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
AUDIT_LOG = CACHE_DIR / "audit.jsonl"
LAST_STATE = CACHE_DIR / "last.json"
CHANNEL_INFO_CACHE = CACHE_DIR / "channel-info.json"
SELF_DM_CACHE = CACHE_DIR / "self-dm.json"
USER_CACHE = CACHE_DIR / "users.json"

CONFIG_FILE = Path.home() / ".config" / "slack-cli" / "config.toml"

MAX_CHARS = 3900  # Slack hard limit 4000, reserve for split marker

# ---------------------------------------------------------------------------
# MSYS/WSL → Windows path normalization (BUG-001)
# ---------------------------------------------------------------------------
# The harness shell is Git Bash (MSYS): tab-completion and $TMPDIR emit POSIX
# paths like `/c/Users/x`, `/mnt/c/Users/x`, `/tmp/x`. Python on Windows is
# native, so `open('/c/..')` resolves to `C:\c\..` (drive-root of CWD) and
# `/tmp/x` → `\tmp\x` — every path-taking command (download -o, upload <file>,
# files --download, @file args) failed and the agent had to hand-feed `C:/`.
# Mirrors ~/.claude/scripts/lp_pathutil.win_path (inlined to keep this CLI
# self-contained); additionally maps the single-segment `/tmp` mount, which in
# Git-for-Windows points at %TEMP% == tempfile.gettempdir() (verified in-env).
_MSYS_DRIVE_RE = re.compile(r"^/([a-zA-Z])(/.*)?$")
_WSL_DRIVE_RE = re.compile(r"^/mnt/([a-zA-Z])(/.*)?$")


def win_path(p):
    """Normalize an MSYS/WSL POSIX path to native Windows form. Idempotent.

    `/c/..`→`C:/..`, `/mnt/c/..`→`C:/..`, `/tmp[/..]`→%TEMP%[/..]. Native,
    relative, UNC, `-` (stdin) and empty/None paths pass through unchanged.
    """
    if not p or not isinstance(p, str) or p == "-":
        return p
    m = _WSL_DRIVE_RE.match(p)
    if m:
        return f'{m.group(1).upper()}:{m.group(2) or "/"}'
    m = _MSYS_DRIVE_RE.match(p)
    if m:
        return f'{m.group(1).upper()}:{m.group(2) or "/"}'
    if p == "/tmp" or p.startswith("/tmp/"):
        rest = p[len("/tmp"):].lstrip("/")
        base = tempfile.gettempdir()
        return os.path.join(base, *rest.split("/")) if rest else base
    return p


def win_path_at(p):
    """Like win_path, but transparently handles the `@file` convention."""
    if isinstance(p, str) and p.startswith("@"):
        return "@" + win_path(p[1:])
    return win_path(p)


# ---------------------------------------------------------------------------
# Token handling
# ---------------------------------------------------------------------------
def _load_env() -> dict:
    out = {}
    if not ENV_FILE.exists():
        return out
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _workspace_url() -> str:
    """Workspace URL for token (re)extraction — from $SLACK_WORKSPACE_URL or .env."""
    env = _load_env()
    return (os.environ.get("SLACK_WORKSPACE_URL")
            or env.get("SLACK_WORKSPACE_URL") or "").rstrip("/")


def _refresh_tokens() -> None:
    """(Re)obtain xoxc/xoxd tokens. Pluggable — no hardcoded personal/secret flow:

    1. $SLACK_TOKEN_REFRESH_CMD (env or .env): run it; it must (re)write .env with
       SLACK_XOXC/SLACK_XOXD. Wire any local secret store / browser flow here.
    2. else extract_tokens.py (bundled CDP browser extractor) if present.
    3. else raise with actionable guidance.
    """
    env = _load_env()
    cmd = os.environ.get("SLACK_TOKEN_REFRESH_CMD") or env.get("SLACK_TOKEN_REFRESH_CMD")
    if cmd:
        print("→ refreshing Slack tokens via SLACK_TOKEN_REFRESH_CMD...", file=sys.stderr)
        subprocess.run(cmd, shell=True, check=True)
        return
    if EXTRACT_SCRIPT.exists():
        ws = _workspace_url()
        print("→ refreshing Slack tokens via extract_tokens.py...", file=sys.stderr)
        subprocess.run([sys.executable, str(EXTRACT_SCRIPT)] + ([ws] if ws else []),
                       check=True)
        return
    raise RuntimeError(
        "Slack tokens missing/expired. Set SLACK_XOXC/SLACK_XOXD in .env, or run "
        "`python extract_tokens.py <workspace-url>`, or set SLACK_TOKEN_REFRESH_CMD "
        "to a command that rewrites .env with fresh tokens."
    )


def _make_client(retry: bool = True) -> WebClient:
    env = _load_env()
    xoxc = env.get("SLACK_XOXC") or os.environ.get("SLACK_XOXC")
    xoxd = env.get("SLACK_XOXD") or os.environ.get("SLACK_XOXD")
    if not xoxc or not xoxd:
        if retry:
            _refresh_tokens()
            return _make_client(retry=False)
        raise RuntimeError("SLACK_XOXC/SLACK_XOXD missing")
    return WebClient(token=xoxc, headers={"Cookie": f"d={xoxd}"})


def _call(fn, *, _client: Optional[WebClient] = None, **kwargs):
    """Wrapper: auto-refresh on 401, backoff on 429."""
    client = _client or _make_client()
    tries = 0
    while True:
        tries += 1
        try:
            return getattr(client, fn)(**kwargs)
        except SlackApiError as e:
            err = e.response.get("error", "")
            if err in ("invalid_auth", "not_authed", "token_expired") and tries == 1:
                _refresh_tokens()
                client = _make_client()
                continue
            if err == "ratelimited" and tries < 5:
                delay = int(e.response.headers.get("Retry-After", "2"))
                print(f"→ rate limited, sleeping {delay}s", file=sys.stderr)
                time.sleep(delay)
                continue
            raise


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------
URL_RE = re.compile(
    r"https?://[^/]*slack\.com/archives/(?P<channel>[A-Z0-9]+)/p(?P<ts>\d+)"
    r"(?:\?[^#]*thread_ts=(?P<thread_ts>[\d.]+))?"
)


def parse_url(url: str) -> tuple[str, str, Optional[str]]:
    """Returns (channel_id, message_ts, thread_ts_or_None)."""
    m = URL_RE.search(url)
    if not m:
        raise ValueError(f"not a slack URL: {url}")
    channel = m.group("channel")
    raw_ts = m.group("ts")
    # pTSVVVVVV → TS.VVVVVV
    msg_ts = f"{raw_ts[:-6]}.{raw_ts[-6:]}"
    thread_ts = m.group("thread_ts")
    return channel, msg_ts, thread_ts


def make_url(team_domain: str, channel: str, ts: str, thread_ts: Optional[str] = None) -> str:
    ts_compact = ts.replace(".", "")
    url = f"https://{team_domain}/archives/{channel}/p{ts_compact}"
    if thread_ts:
        url += f"?thread_ts={thread_ts}&cid={channel}"
    return url


# ---------------------------------------------------------------------------
# Target resolution (#channel, @user, URL, C-id, D-id)
# ---------------------------------------------------------------------------
def _load_cache(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_cache(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def resolve_target(target: str, client: WebClient) -> str:
    """'#general' / '@user' / 'C123' / 'D123' / URL → channel_id."""
    if target == "-":
        state = _load_cache(LAST_STATE)
        if not state.get("channel"):
            raise ValueError("no last channel — use explicit target")
        return state["channel"]
    if "slack.com/archives/" in target:
        return parse_url(target)[0]
    # channel/DM ID, в т.ч. с ошибочным префиксом #/@ (скопирован как "#C05...")
    bare = target.lstrip("#@")
    if bare[:1] in ("C", "D", "G") and len(bare) >= 9 and bare[1:].isalnum():
        return bare
    if target.startswith("#"):
        return _resolve_channel_name(target[1:], client)
    if target.startswith("@"):
        return _resolve_user_dm(target[1:], client)
    # fallback: try as name
    try:
        return _resolve_channel_name(target, client)
    except Exception:
        return _resolve_user_dm(target, client)


def _resolve_channel_name(name: str, client: WebClient) -> str:
    cache = _load_cache(CHANNEL_INFO_CACHE)
    for cid, info in cache.items():
        if info.get("name") == name:
            return cid
    # populate via conversations.list (paginated)
    cursor = None
    while True:
        resp = _call("conversations_list", _client=client, limit=200,
                     types="public_channel,private_channel", cursor=cursor)
        for ch in resp.get("channels", []):
            cache[ch["id"]] = {"name": ch.get("name"), "is_channel": ch.get("is_channel"),
                               "is_group": ch.get("is_group"), "is_private": ch.get("is_private")}
            if ch.get("name") == name:
                _save_cache(CHANNEL_INFO_CACHE, cache)
                return ch["id"]
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    _save_cache(CHANNEL_INFO_CACHE, cache)
    raise ValueError(f"channel not found: #{name}")


def _resolve_user_dm(name: str, client: WebClient) -> str:
    """@username → D-channel (opens IM)."""
    user_id = _resolve_user_id(name, client)
    resp = _call("conversations_open", _client=client, users=user_id)
    return resp["channel"]["id"]


def _resolve_user_id(name: str, client: WebClient) -> str:
    if name.startswith("U") and len(name) >= 9:
        return name
    cache = _load_cache(USER_CACHE)
    for uid, info in cache.items():
        if info.get("name") == name or info.get("display_name") == name or info.get("real_name") == name:
            return uid
    cursor = None
    while True:
        resp = _call("users_list", _client=client, limit=200, cursor=cursor)
        for u in resp.get("members", []):
            profile = u.get("profile", {})
            cache[u["id"]] = {
                "name": u.get("name"),
                "display_name": profile.get("display_name"),
                "real_name": profile.get("real_name"),
            }
            if (u.get("name") == name or profile.get("display_name") == name
                    or profile.get("real_name") == name):
                _save_cache(USER_CACHE, cache)
                return u["id"]
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    _save_cache(USER_CACHE, cache)
    raise ValueError(f"user not found: @{name}")


# ---------------------------------------------------------------------------
# Destination introspection
# ---------------------------------------------------------------------------
@dataclass
class Dest:
    channel: str
    kind: str   # 'channel' | 'private_channel' | 'dm' | 'mpim'
    name: Optional[str]  # для pretty-print
    thread_ts: Optional[str] = None

    @property
    def is_channel_like(self) -> bool:
        return self.kind in ("channel", "private_channel")


def dest_of(channel_id: str, thread_ts: Optional[str], client: WebClient) -> Dest:
    cache = _load_cache(CHANNEL_INFO_CACHE)
    info = cache.get(channel_id)
    if not info or info.get("_full") is None:
        resp = _call("conversations_info", _client=client, channel=channel_id)
        ch = resp["channel"]
        info = {
            "name": ch.get("name"),
            "is_channel": ch.get("is_channel", False),
            "is_group": ch.get("is_group", False),
            "is_private": ch.get("is_private", False),
            "is_im": ch.get("is_im", False),
            "is_mpim": ch.get("is_mpim", False),
            "user": ch.get("user"),
            "_full": True,
        }
        cache[channel_id] = info
        _save_cache(CHANNEL_INFO_CACHE, cache)
    if info.get("is_im"):
        kind = "dm"
        name = f"@{_user_name(info.get('user'), client)}" if info.get("user") else channel_id
    elif info.get("is_mpim"):
        kind = "mpim"
        name = info.get("name") or channel_id
    elif info.get("is_private") or info.get("is_group"):
        kind = "private_channel"
        name = f"#{info.get('name')}" if info.get("name") else channel_id
    else:
        kind = "channel"
        name = f"#{info.get('name')}" if info.get("name") else channel_id
    return Dest(channel=channel_id, kind=kind, name=name, thread_ts=thread_ts)


def _user_name(user_id: Optional[str], client: WebClient) -> str:
    if not user_id:
        return "?"
    cache = _load_cache(USER_CACHE)
    if user_id in cache:
        c = cache[user_id]
        return c.get("display_name") or c.get("name") or user_id
    try:
        resp = _call("users_info", _client=client, user=user_id)
        u = resp["user"]
        profile = u.get("profile", {})
        cache[user_id] = {"name": u.get("name"), "display_name": profile.get("display_name"),
                          "real_name": profile.get("real_name")}
        _save_cache(USER_CACHE, cache)
        return profile.get("display_name") or u.get("name") or user_id
    except Exception:
        return user_id


# ---------------------------------------------------------------------------
# Config & notify
# ---------------------------------------------------------------------------
def _should_notify_channels() -> bool:
    # priority: flag (caller) > env > config > default(True)
    env = os.environ.get("SLACK_NOTIFY_CHANNELS")
    if env is not None:
        return env.lower() not in ("0", "false", "no", "off")
    if CONFIG_FILE.exists():
        import tomllib  # py3.11+
        try:
            data = tomllib.loads(CONFIG_FILE.read_text())
            v = data.get("notify", {}).get("channels")
            if v is not None:
                return bool(v)
        except Exception:
            pass
    return True


def _self_id(client: WebClient) -> str:
    """Current user id — resolved lazily via auth.test, cached to disk."""
    cache = _load_cache(SELF_DM_CACHE)
    if cache.get("user_id"):
        return cache["user_id"]
    uid = _call("auth_test", _client=client).get("user_id")
    cache["user_id"] = uid
    _save_cache(SELF_DM_CACHE, cache)
    return uid


def _self_dm_id(client: WebClient) -> str:
    cache = _load_cache(SELF_DM_CACHE)
    if "dm_id" in cache:
        return cache["dm_id"]
    resp = _call("conversations_open", _client=client, users=_self_id(client))
    dm_id = resp["channel"]["id"]
    cache = _load_cache(SELF_DM_CACHE)
    cache["dm_id"] = dm_id
    _save_cache(SELF_DM_CACHE, cache)
    return dm_id


def notify_self(client: WebClient, dest: Dest, urls: list[str]) -> None:
    n = len(urls)
    suffix = f" [{n} parts]" if n > 1 else ""
    loc = dest.name or dest.channel
    if dest.thread_ts:
        loc += " (thread)"
    text = f":mega: agent posted in {loc}{suffix} → {urls[0]}"
    try:
        _call("chat_postMessage", _client=client, channel=_self_dm_id(client),
              text=text, unfurl_links=False)
    except Exception as e:
        print(f"WARN: notify_self failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------
def audit(action: str, **fields) -> None:
    rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "action": action, **fields}
    with AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Text splitting (preserves code fences)
# ---------------------------------------------------------------------------
def split_text(text: str, limit: int = MAX_CHARS) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    buf: list[str] = []
    buf_len = 0
    in_fence = False
    fence_marker = "```"
    for line in text.splitlines(keepends=True):
        line_len = len(line)
        if buf_len + line_len > limit and buf:
            chunk = "".join(buf)
            if in_fence:
                chunk += "```\n"  # close fence
            parts.append(chunk.rstrip())
            buf = []
            buf_len = 0
            if in_fence:
                buf.append("```\n")  # reopen
                buf_len += 4
        if fence_marker in line:
            in_fence = not in_fence
        buf.append(line)
        buf_len += line_len
    if buf:
        parts.append("".join(buf).rstrip())
    return parts


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------
def save_last(channel: str, ts: Optional[str] = None, thread_ts: Optional[str] = None) -> None:
    state = {"channel": channel}
    if ts:
        state["ts"] = ts
    if thread_ts:
        state["thread_ts"] = thread_ts
    _save_cache(LAST_STATE, state)


def team_domain(client: WebClient) -> str:
    cache = _load_cache(CACHE_DIR / "team.json")
    if "domain" in cache:
        return cache["domain"]
    resp = _call("team_info", _client=client)
    domain = f"{resp['team']['domain']}.slack.com"
    _save_cache(CACHE_DIR / "team.json", {"domain": domain})
    return domain


def _human_size(size: Optional[int]) -> str:
    value = size or 0
    if value < 1_000_000:
        return f"{value // 1024}KB"
    return f"{value // 1_000_000}MB"


def _indent_text(text: str, prefix: str = "  ") -> str:
    return text.replace("\n", f"\n{prefix}")


def _archive_host() -> str:
    """Workspace host for archive URLs: cached team domain → $SLACK_WORKSPACE_URL → fallback."""
    cache = _load_cache(CACHE_DIR / "team.json")
    if cache.get("domain"):
        return cache["domain"]
    ws = _workspace_url()
    if ws:
        return urllib.parse.urlsplit(ws).netloc or "app.slack.com"
    return "app.slack.com"


def _msg_permalink(channel: str, ts: Optional[str], thread_ts: Optional[str] = None) -> str:
    """Построить archive-URL сообщения из channel+ts (для привязки файла к сообщению)."""
    if not channel or not ts:
        return ""
    url = f"https://{_archive_host()}/archives/{channel}/p{str(ts).replace('.', '')}"
    if thread_ts and thread_ts != ts:
        url += f"?thread_ts={thread_ts}&cid={channel}"
    return url


def _message_from_url(url: str, client: WebClient) -> tuple[str, Optional[dict]]:
    channel_id, ts, _ = parse_url(url)
    resp = _call("conversations_history", _client=client, channel=channel_id,
                 latest=ts, inclusive=True, limit=1)
    msgs = resp.get("messages", [])
    if msgs and msgs[0].get("ts") == ts:
        return channel_id, msgs[0]

    target_ts = float(ts)
    history = _call("conversations_history", _client=client, channel=channel_id,
                    latest=ts, inclusive=True, limit=200)
    for candidate in history.get("messages", []):
        if not candidate.get("reply_count"):
            continue
        candidate_ts = candidate.get("ts")
        if not candidate_ts or float(candidate_ts) > target_ts:
            continue
        latest_reply = candidate.get("latest_reply")
        if latest_reply and float(latest_reply) < target_ts:
            continue
        replies = _call("conversations_replies", _client=client, channel=channel_id,
                        ts=candidate_ts, limit=200)
        for reply in replies.get("messages", []):
            if reply.get("ts") == ts:
                return channel_id, reply

    return channel_id, msgs[0] if msgs else None


def _messages_for_target(target: str, client: WebClient, limit: int,
                         force_thread: bool = False) -> tuple[str, list[dict]]:
    if "slack.com/archives/" in target and "thread_ts" in target:
        channel_id, _, thread_ts = parse_url(target)
        resp = _call("conversations_replies", _client=client, channel=channel_id,
                     ts=thread_ts, limit=limit)
        return channel_id, resp.get("messages", [])

    if "slack.com/archives/" in target:
        channel_id, msg = _message_from_url(target, client)
        # Авто-разворот треда — только если URL указывает на РОДИТЕЛЯ треда
        # (thread_ts == ts) с ответами, либо явно запрошен --thread.
        # URL на конкретный reply (thread_ts != ts) без --thread → само сообщение,
        # а не весь родительский тред.
        should_read_thread = bool(
            msg and msg.get("thread_ts") and (
                force_thread
                or (msg.get("thread_ts") == msg.get("ts") and msg.get("reply_count"))
            )
        )
        if should_read_thread:
            resp = _call("conversations_replies", _client=client, channel=channel_id,
                         ts=msg["thread_ts"], limit=limit)
            return channel_id, resp.get("messages", [])
        return channel_id, [msg] if msg else []

    channel_id = resolve_target(target, client)
    resp = _call("conversations_history", _client=client, channel=channel_id, limit=limit)
    return channel_id, list(reversed(resp.get("messages", [])))


def _slack_auth_headers() -> dict[str, str]:
    env = _load_env()
    xoxc = env.get("SLACK_XOXC") or os.environ.get("SLACK_XOXC")
    xoxd = env.get("SLACK_XOXD") or os.environ.get("SLACK_XOXD")
    if not xoxc or not xoxd:
        raise RuntimeError("SLACK_XOXC/SLACK_XOXD missing")
    # Keep xoxd RAW (URL-encoded) — same rule as _make_client(): the Cookie header
    # `d=...` must match exactly what the browser sends. unquote() here corrupts tokens
    # containing %2B/%2F/%3D → files.slack.com auth fails and returns the login HTML page.
    return {
        "Authorization": f"Bearer {xoxc}",
        "Cookie": f"d={xoxd}",
    }


def _file_label(file_info: dict) -> str:
    """Самодостаточная строка вложения: что есть + что это + КАК ДОСТАТЬ (F-id + готовая команда).
    Стабильный префикс `[file <Fid>]` — машинно-парсимо (agentic-cli)."""
    fid = file_info.get("id") or "?"
    title = file_info.get("title") or file_info.get("name") or fid or "file"
    pretty_type = file_info.get("pretty_type") or file_info.get("mimetype") or "file"
    mode = file_info.get("mode")
    if mode == "snippet":
        pretty_type = f"{pretty_type}, snippet"
    hint = f" → slack download {fid}" if fid and fid != "?" else ""
    return f"[file {fid}] {title} ({pretty_type}, {_human_size(file_info.get('size'))}){hint}"


def _snippet_text(file_info: dict, max_bytes: int = 64 * 1024) -> Optional[str]:
    if file_info.get("mode") != "snippet":
        return None
    url = file_info.get("url_private_download") or file_info.get("url_private")
    if url:
        import urllib.request

        req = urllib.request.Request(url, headers=_slack_auth_headers())
        with urllib.request.urlopen(req) as response:
            raw = response.read(max_bytes + 1)
        truncated = len(raw) > max_bytes
        if truncated:
            raw = raw[:max_bytes]
        text = raw.decode("utf-8", errors="replace")
        if truncated:
            return text.rstrip("\n") + "\n...[truncated]"
        return text.rstrip("\n")
    preview = (file_info.get("preview") or "").rstrip("\n")
    if not preview:
        return None
    if file_info.get("preview_is_truncated"):
        return preview + "\n...[preview truncated by Slack]"
    return preview


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def emit(payload: dict, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for k, v in payload.items():
            if isinstance(v, list):
                for item in v:
                    print(f"{k}={item}")
            else:
                print(f"{k}={v}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def read_text_arg(arg: str) -> str:
    if arg == "-":
        return sys.stdin.read()
    if arg.startswith("@"):
        raw = arg[1:]
        # normalize MSYS/WSL/tmp path (BUG-001); try native/cwd, then ROOT-relative
        for cand in (Path(win_path(raw)), ROOT / raw):
            if cand.exists():
                return cand.read_text(encoding="utf-8")
    return arg


def cmd_send(args):
    client = _make_client()
    channel_id = resolve_target(args.target, client)
    thread_ts = None
    if args.thread:
        if "slack.com/archives/" in args.thread:
            _, parent_ts, url_thread = parse_url(args.thread)
            thread_ts = url_thread or parent_ts
        else:
            thread_ts = args.thread
    text = read_text_arg(args.text)
    dest = dest_of(channel_id, thread_ts, client)
    domain = team_domain(client)

    parts = split_text(text) if not args.no_split else [text]
    urls: list[str] = []
    tss: list[str] = []
    first_ts = None
    for i, part in enumerate(parts):
        resp = _call("chat_postMessage", _client=client,
                     channel=channel_id, text=part,
                     thread_ts=thread_ts, unfurl_links=False)
        ts = resp["ts"]
        tss.append(ts)
        urls.append(make_url(domain, channel_id, ts, thread_ts))
        if i == 0:
            first_ts = ts
    save_last(channel_id, first_ts, thread_ts)
    audit("send", dest=dest.kind, name=dest.name, channel=channel_id,
          thread_ts=thread_ts, parts=len(parts), url=urls[0])

    # Notify self on channel-destinations
    notify_flag = args.notify if args.notify is not None else _should_notify_channels()
    if dest.is_channel_like and notify_flag:
        notify_self(client, dest, urls)

    print(f"→ dest={dest.kind} {dest.name}{' (thread)' if thread_ts else ''}", file=sys.stderr)
    emit({"ts": first_ts, "url": urls[0], "dest": dest.kind,
          "parts": len(parts), **({"urls": urls} if len(parts) > 1 else {})}, args.json)


def cmd_reply(args):
    # reply <url> <text>
    channel_id, parent_ts, url_thread = parse_url(args.url)
    thread_ts = url_thread or parent_ts
    # Re-use send via mimicking args namespace
    fake = argparse.Namespace(
        target=channel_id, text=args.text, thread=thread_ts,
        json=args.json, no_split=args.no_split, notify=args.notify,
    )
    cmd_send(fake)


def cmd_edit(args):
    client = _make_client()
    channel_id, ts, _ = parse_url(args.url)
    text = read_text_arg(args.text)
    resp = _call("chat_update", _client=client, channel=channel_id, ts=ts,
                 text=text, parse="none")
    audit("edit", channel=channel_id, ts=ts)
    emit({"ts": resp["ts"], "channel": channel_id, "ok": True}, args.json)


def cmd_delete(args):
    client = _make_client()
    channel_id, ts, _ = parse_url(args.url)
    _call("chat_delete", _client=client, channel=channel_id, ts=ts)
    audit("delete", channel=channel_id, ts=ts)
    emit({"ok": True, "channel": channel_id, "ts": ts}, args.json)


def cmd_react(args):
    client = _make_client()
    channel_id, ts, _ = parse_url(args.url)
    name = args.emoji.strip(":")
    _call("reactions_add", _client=client, channel=channel_id, timestamp=ts, name=name)
    audit("react", channel=channel_id, ts=ts, emoji=name)
    emit({"ok": True}, args.json)


def cmd_unreact(args):
    client = _make_client()
    channel_id, ts, _ = parse_url(args.url)
    name = args.emoji.strip(":")
    _call("reactions_remove", _client=client, channel=channel_id, timestamp=ts, name=name)
    audit("unreact", channel=channel_id, ts=ts, emoji=name)
    emit({"ok": True}, args.json)


# TASK-001 — slack read: рендерить Block Kit table/data_table в текст (сейчас (table) placeholder) + чинить --js…
# Fallback-тексты, которые наши же команды (table/blocks/md) ставят как `text`,
# когда настоящее содержимое живёт в blocks. Их не печатаем, если есть блоки.
_BLOCK_PLACEHOLDERS = {"(table)", "(blocks)", "(data_table)"}


def _rt_to_str(el) -> str:
    """rich_text element/section → plain text (рекурсивно)."""
    if isinstance(el, str):
        return el
    if not isinstance(el, dict):
        return str(el)
    t = el.get("type")
    if t == "text":
        return el.get("text", "")
    if t == "link":
        return el.get("text") or el.get("url", "")
    if t == "emoji":
        return f":{el.get('name', '')}:"
    if t == "user":
        return f"<@{el.get('user_id', '')}>"
    if t == "rich_text_list":
        items = ["• " + "".join(_rt_to_str(e) for e in it.get("elements", []))
                 for it in el.get("elements", [])]
        return "\n".join(items)
    if "elements" in el:  # rich_text / rich_text_section / preformatted / quote
        return "".join(_rt_to_str(e) for e in el["elements"])
    if isinstance(el.get("text"), str):
        return el["text"]
    return ""


def _cell_text(cell) -> str:
    if isinstance(cell, dict):
        ct = cell.get("type")
        if ct in ("raw_text", "raw_number"):
            return str(cell.get("text", ""))
        return _rt_to_str(cell).strip()
    return str(cell)


def _render_table_block(block) -> str:
    """table/data_table block → выровненная по колонкам текстовая таблица."""
    rows = block.get("rows") or []
    grid = [[_cell_text(c) for c in row] for row in rows]
    ncols = max((len(r) for r in grid), default=0)
    widths = [0] * ncols
    for r in grid:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))
    lines = []
    for ri, r in enumerate(grid):
        lines.append(" | ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip())
        if ri == 0 and len(grid) > 1:  # header separator
            lines.append("-+-".join("-" * widths[i] for i in range(len(r))))
    cap = block.get("caption")
    table = "\n".join(lines)
    return f"[{cap}]\n{table}" if cap and block.get("type") == "data_table" else table


def cmd_read(args):
    client = _make_client()
    _, msgs = _messages_for_target(args.target, client, args.limit, force_thread=args.thread)

    if args.json:
        # фильтруем None (reply/parent не найден) — иначе json мог быть пустым/ломаным
        print(json.dumps([m for m in msgs if m], ensure_ascii=False, indent=2))
        return
    total_files = 0
    for m in msgs:
        user = _user_name(m.get("user") or m.get("bot_id"), client)
        ts = m.get("ts", "?")
        tstr = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts))) if "." in ts else ts
        print(f"--- @{user}  {tstr}  ts={ts}")
        text = (m.get("text") or "").rstrip("\n")
        blocks = m.get("blocks") or []
        table_blocks = [b for b in blocks if b.get("type") in ("table", "data_table")]
        # text не печатаем, если он лишь fallback-плейсхолдер ((table)/(blocks)) или
        # дублирует caption data_table — содержимое и так в отрендеренных блоках
        _captions = {b.get("caption") for b in table_blocks if b.get("caption")}
        if text and table_blocks and (text in _BLOCK_PLACEHOLDERS or text in _captions):
            text = ""
        if text:
            print(f"  {_indent_text(text)}")
        for tb in table_blocks:
            print(f"  {_indent_text(_render_table_block(tb))}")
        files = m.get("files", []) or []
        if not text and not table_blocks and not files:
            print("  ")
        for file_info in files:
            total_files += 1
            print(f"  {_file_label(file_info)}")
            snippet = _snippet_text(file_info)
            if snippet:
                print(f"    {_indent_text(snippet, prefix='    ')}")
    # agentic: сводка + no-silent-truncation guard
    if total_files:
        print(f"[summary] сообщений: {len(msgs)}, вложений: {total_files} "
              f"(достать: slack download <F-id>)", file=sys.stderr)
    if len(msgs) >= args.limit:
        print(f"[warn] показано {len(msgs)} == --limit {args.limit}: вывод МОГ быть обрезан, "
              f"увеличь --limit чтобы увидеть весь тред/историю", file=sys.stderr)


def cmd_upload(args):
    client = _make_client()
    channel_id = resolve_target(args.target, client)
    # thread из --thread, иначе из самой target-ссылки (если это URL с thread_ts/на родителя треда)
    thread_ts = _thread_ts(getattr(args, "thread", None))
    if not thread_ts and isinstance(args.target, str) and "slack.com/archives/" in args.target:
        thread_ts = _thread_ts(args.target)
    files = args.file if isinstance(args.file, list) else [args.file]
    kw = dict(initial_comment=args.comment or "")
    # один файл → file=; несколько → file_uploads=[...] (один пост с N вложениями,
    # один initial_comment — без дублей-сообщений в треде)
    if len(files) == 1:
        kw["file"] = files[0]
    else:
        kw["file_uploads"] = [{"file": f, "filename": os.path.basename(f)} for f in files]
    if thread_ts:
        kw["thread_ts"] = thread_ts
    resp = _call("files_upload_v2", _client=client, channel=channel_id, **kw)
    audit("upload", channel=channel_id, file=files, count=len(files))
    uploaded = resp.get("files") or ([resp["file"]] if resp.get("file") else [])
    file_ids = [f.get("id") for f in uploaded]
    permalinks = [f.get("permalink") for f in uploaded]
    emit({"file_id": file_ids if len(file_ids) != 1 else file_ids[0],
          "permalink": permalinks if len(permalinks) != 1 else (permalinks[0] if permalinks else None),
          "count": len(file_ids), "thread_ts": thread_ts}, args.json)


def cmd_me(args):
    client = _make_client()
    resp = _call("auth_test", _client=client)
    emit({"user_id": resp.get("user_id"), "user": resp.get("user"),
          "team": resp.get("team"), "url": resp.get("url")}, args.json)


def _in_filter(in_target: str, client: WebClient) -> str:
    """Строит фрагмент `in:...` для search-запроса.

    Корректно обрабатывает channel ID даже с ошибочным префиксом #/@
    (например `#C05G3S35GAJ`): снимает префикс, резолвит ID → #имя через
    conversations.info. Голый ID без имени → `in:<#ID>` (mention-синтаксис).
    Имена/@users пропускает как есть.
    """
    tgt = in_target.strip()
    bare = tgt.lstrip("#@")
    # channel ID (C/G/D...), возможно с ошибочным # или @ впереди
    if bare[:1] in ("C", "G", "D") and len(bare) >= 9 and bare[1:].isalnum():
        try:
            name = dest_of(bare, None, client).name  # '#chan' | '@user' | id
        except Exception:
            name = None
        if name and name.startswith(("#", "@")):
            return f"in:{name}"
        return f"in:<#{bare}>"
    if tgt.startswith("@"):
        return f"in:@{bare}"
    # обычное имя канала (с # или без)
    return f"in:#{bare}"


def cmd_search(args):
    """search.messages с фильтрами. Работает только с user-token (xoxc) — нам ок."""
    client = _make_client()
    domain = team_domain(client)
    q = args.query
    if args.in_target:
        q += f" {_in_filter(args.in_target, client)}"
    if args.from_user:
        user = args.from_user.lstrip("@")
        q += f" from:@{user}"
    if args.after:
        q += f" after:{args.after}"
    if args.before:
        q += f" before:{args.before}"

    resp = _call("search_messages", _client=client, query=q, count=args.limit, sort="timestamp")
    matches = resp.get("messages", {}).get("matches", [])

    if args.json:
        print(json.dumps(matches, ensure_ascii=False, indent=2))
        return
    for m in matches:
        ch = m.get("channel", {})
        ch_name = ch.get("name") or ch.get("id", "?")
        user = m.get("username") or _user_name(m.get("user"), client)
        ts = m.get("ts", "?")
        tstr = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts))) if "." in ts else ts
        text = (m.get("text") or "").replace("\n", " ")
        snippet = text[:120] + ("…" if len(text) > 120 else "")
        permalink = m.get("permalink") or make_url(domain, ch.get("id", ""), ts)
        print(f"{tstr}  #{ch_name}  @{user}")
        print(f"  {snippet}")
        print(f"  {permalink}")


def cmd_channels(args):
    """List channels; --filter substring на name."""
    client = _make_client()
    cache = _load_cache(CHANNEL_INFO_CACHE)
    # force populate if empty or --refresh
    if args.refresh or not any(c.get("name") for c in cache.values()):
        cursor = None
        while True:
            resp = _call("conversations_list", _client=client, limit=200,
                         types="public_channel,private_channel", cursor=cursor)
            for ch in resp.get("channels", []):
                cache[ch["id"]] = {
                    "name": ch.get("name"),
                    "is_channel": ch.get("is_channel"),
                    "is_group": ch.get("is_group"),
                    "is_private": ch.get("is_private"),
                }
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        _save_cache(CHANNEL_INFO_CACHE, cache)
    rows = []
    for cid, info in cache.items():
        name = info.get("name") or ""
        if args.filter and args.filter not in name:
            continue
        if not name:  # skip DMs without names
            continue
        rows.append((cid, name, "private" if info.get("is_private") else "public"))
    rows.sort(key=lambda r: r[1])
    if args.json:
        print(json.dumps([{"id": r[0], "name": r[1], "visibility": r[2]} for r in rows],
                         ensure_ascii=False, indent=2))
        return
    for cid, name, vis in rows:
        print(f"{cid}\t{vis}\t#{name}")


def cmd_users(args):
    """List users; --filter substring на name/display/real."""
    client = _make_client()
    cache = _load_cache(USER_CACHE)
    if args.refresh or not cache:
        cursor = None
        while True:
            resp = _call("users_list", _client=client, limit=200, cursor=cursor)
            for u in resp.get("members", []):
                profile = u.get("profile", {})
                cache[u["id"]] = {
                    "name": u.get("name"),
                    "display_name": profile.get("display_name"),
                    "real_name": profile.get("real_name"),
                    "deleted": u.get("deleted", False),
                }
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        _save_cache(USER_CACHE, cache)
    rows = []
    for uid, info in cache.items():
        if info.get("deleted"):
            continue
        name = info.get("name") or ""
        display = info.get("display_name") or ""
        real = info.get("real_name") or ""
        if args.filter and args.filter.lower() not in (
            name + display + real).lower():
            continue
        rows.append((uid, name, display, real))
    rows.sort(key=lambda r: r[1])
    if args.json:
        print(json.dumps([{"id": r[0], "name": r[1], "display_name": r[2], "real_name": r[3]}
                          for r in rows], ensure_ascii=False, indent=2))
        return
    for uid, name, display, real in rows:
        shown = display or real or name
        print(f"{uid}\t@{name}\t{shown}")


def cmd_files(args):
    """List files in channel/thread/DM.

    Тред НЕ разворачивается сам: для всего треда добавь --thread (файлы обычно в реплаях,
    а не в родителе). Без --thread — только сообщение из URL (или history канала/DM).
    Каждый файл привязан к своему сообщению (msg_url). --download DIR скачивает все найденные.
    """
    client = _make_client()
    channel, msgs = _messages_for_target(args.target, client, args.limit, force_thread=args.thread)

    files = []
    for m in msgs:
        mts = m.get("ts")
        mthread = m.get("thread_ts")
        for f in m.get("files", []) or []:
            files.append({
                "id": f.get("id"),
                "name": f.get("name"),
                "title": f.get("title"),
                "size": f.get("size"),
                "mode": f.get("mode"),
                "filetype": f.get("filetype"),
                "mimetype": f.get("mimetype"),
                "url_private_download": f.get("url_private_download"),
                "permalink": f.get("permalink"),
                "msg_ts": mts,
                "msg_user": m.get("user"),
                "msg_url": _msg_permalink(channel, mts, mthread),
            })

    if args.json:
        print(json.dumps(files, ensure_ascii=False, indent=2))
    else:
        if not files:
            print(f"(файлов не найдено; сообщений просмотрено: {len(msgs)}"
                  + ("" if args.thread else "; для треда добавь --thread") + ")", file=sys.stderr)
        for f in files:
            kind = f.get("mimetype", "?")
            if f.get("mode") == "snippet":
                kind = f"snippet/{kind}"
            print(f"{f['id']}\t{_human_size(f.get('size'))}\t{kind}\t"
                  f"{f.get('title') or f.get('name','?')}\t{f.get('msg_url','')}")

    dest = getattr(args, "download", None)
    if dest:
        out_dir = Path(dest)
        out_dir.mkdir(parents=True, exist_ok=True)
        import urllib.request
        saved = []
        for f in files:
            url = f.get("url_private_download") or f.get("url_private")
            if not url:
                continue
            name = f.get("name") or (f["id"] + ".bin")
            out = out_dir / f"{f['id']}_{name}"
            req = urllib.request.Request(url, headers=_slack_auth_headers())
            with urllib.request.urlopen(req) as r, out.open("wb") as fh:
                while True:
                    chunk = r.read(64 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
            saved.append(str(out))
        audit("files-download", target=args.target, count=len(saved), dest=str(out_dir))
        print(f"→ скачано {len(saved)} файлов в {out_dir}", file=sys.stderr)
        for s in saved:
            print(f"   {s}", file=sys.stderr)


def cmd_download(args):
    """Download a file by ID or permalink."""
    import urllib.request
    client = _make_client()
    if args.file.startswith("F") and len(args.file) >= 9:
        # file ID → files.info → url_private_download
        resp = _call("files_info", _client=client, file=args.file)
        f = resp["file"]
        url = f["url_private_download"]
        name = f.get("name") or args.file
    elif args.file.startswith("http"):
        url = args.file
        name = args.output or url.rsplit("/", 1)[-1].split("?")[0] or "downloaded"
    else:
        raise ValueError("expected F-id or URL")

    out = Path(args.output) if args.output else Path.cwd() / name
    if out.is_dir():
        out = out / name

    req = urllib.request.Request(url, headers=_slack_auth_headers())
    with urllib.request.urlopen(req) as r, out.open("wb") as f:
        while True:
            chunk = r.read(64 * 1024)
            if not chunk:
                break
            f.write(chunk)
    audit("download", file=args.file, path=str(out))
    emit({"path": str(out), "size": out.stat().st_size}, args.json)


def cmd_reactions(args):
    """Читает реакции на конкретное сообщение."""
    client = _make_client()
    channel_id, ts, _ = parse_url(args.url)
    resp = _call("reactions_get", _client=client, channel=channel_id, timestamp=ts, full=True)
    msg = resp.get("message", {})
    reactions = msg.get("reactions", []) or []
    if args.json:
        print(json.dumps(reactions, ensure_ascii=False, indent=2))
        return
    if not reactions:
        print("(no reactions)", file=sys.stderr)
        return
    for r in reactions:
        users = r.get("users", []) or []
        names = [f"@{_user_name(u, client)}" for u in users]
        print(f":{r['name']}:  {r.get('count', 0)}  {', '.join(names)}")


def cmd_mentions(args):
    """Упоминания текущего пользователя (search по @me)."""
    client = _make_client()
    domain = team_domain(client)
    me = _call("auth_test", _client=client)
    me_handle = me.get("user")
    q = f"@{me_handle}"
    if args.after:
        q += f" after:{args.after}"
    if args.before:
        q += f" before:{args.before}"
    if args.in_target:
        q += f" {_in_filter(args.in_target, client)}"

    resp = _call("search_messages", _client=client, query=q, count=args.limit, sort="timestamp")
    matches = resp.get("messages", {}).get("matches", [])

    if args.json:
        print(json.dumps(matches, ensure_ascii=False, indent=2))
        return
    if not matches:
        print("(no mentions)", file=sys.stderr)
        return
    for m in matches:
        ch = m.get("channel", {})
        ch_name = ch.get("name") or ch.get("id", "?")
        user = m.get("username") or _user_name(m.get("user"), client)
        ts = m.get("ts", "?")
        tstr = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts))) if "." in ts else ts
        text = (m.get("text") or "").replace("\n", " ")
        snippet = text[:150] + ("…" if len(text) > 150 else "")
        permalink = m.get("permalink") or make_url(domain, ch.get("id", ""), ts)
        print(f"{tstr}  #{ch_name}  @{user}")
        print(f"  {snippet}")
        print(f"  {permalink}")


def cmd_unread(args):
    """Unread activity: каналы/DM/треды с непрочитанными + первые превью."""
    client = _make_client()
    domain = team_domain(client)
    # users.conversations возвращает channels с unread_count для user token
    types = args.types or "public_channel,private_channel,im,mpim"
    cursor = None
    unread = []
    while True:
        resp = _call("users_conversations", _client=client, types=types, limit=200,
                     cursor=cursor, exclude_archived=True)
        for ch in resp.get("channels", []):
            n = ch.get("unread_count_display") or ch.get("unread_count") or 0
            if n > 0:
                unread.append((ch, n))
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    # Sort: каналы сначала, потом DM
    unread.sort(key=lambda x: (not (x[0].get("is_channel") or x[0].get("is_group")), -x[1]))

    if args.json:
        out = []
        for ch, n in unread:
            out.append({
                "channel_id": ch["id"],
                "name": ch.get("name"),
                "unread": n,
                "is_dm": ch.get("is_im"),
                "is_mpim": ch.get("is_mpim"),
            })
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return
    if not unread:
        print("(no unread)", file=sys.stderr)
        return
    for ch, n in unread:
        cid = ch["id"]
        if ch.get("is_im"):
            label = f"@{_user_name(ch.get('user'), client)}"
        elif ch.get("is_mpim"):
            label = ch.get("name") or cid
        else:
            label = f"#{ch.get('name') or cid}"
        print(f"{n:>3} unread  {label}  ({cid})")
        if args.preview:
            last_read = ch.get("last_read")
            try:
                hist = _call("conversations_history", _client=client,
                             channel=cid, oldest=last_read, limit=min(n, 3), inclusive=False)
                for m in reversed(hist.get("messages", [])):
                    u = _user_name(m.get("user"), client)
                    ts = m.get("ts", "?")
                    tstr = time.strftime("%H:%M", time.localtime(float(ts))) if "." in ts else ts
                    text = (m.get("text") or "").replace("\n", " ")[:100]
                    print(f"    {tstr}  @{u}: {text}")
                    print(f"       {make_url(domain, cid, ts)}")
            except Exception as e:
                print(f"    (failed to preview: {e})")


def cmd_whoami_msg(args):
    """Кто автор сообщения по URL."""
    client = _make_client()
    channel_id, ts, _ = parse_url(args.url)
    resp = _call("conversations_history", _client=client, channel=channel_id,
                 latest=ts, inclusive=True, limit=1)
    msgs = resp.get("messages", [])
    if not msgs:
        print("ERROR: message not found", file=sys.stderr)
        sys.exit(1)
    m = msgs[0]
    user_id = m.get("user") or m.get("bot_id")
    name = _user_name(user_id, client)
    emit({"user_id": user_id, "user": name, "ts": ts, "channel": channel_id}, args.json)


# ---------------------------------------------------------------------------
# Block Kit commands (table / data_table / markdown / mermaid-PNG / raw blocks)
# Builders + mermaid rendering live in send_blocks.py (sibling module).
# ---------------------------------------------------------------------------
def _thread_ts(thread):
    if not thread:
        return None
    if "slack.com/archives/" in thread:
        _, parent_ts, url_thread = parse_url(thread)
        return url_thread or parent_ts
    return thread


def _notify_if_channel(args, client, dest, urls):
    flag = args.notify if getattr(args, "notify", None) is not None else _should_notify_channels()
    if urls and dest.is_channel_like and flag:
        notify_self(client, dest, urls)


def _wait_file_shared(client, file_id, timeout=20.0):
    """Block until an uploaded file is actually shared (poll files.info), so the next
    message can't overtake the image in the thread. Returns True once confirmed."""
    import time
    if not file_id:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            info = _call("files_info", _client=client, file=file_id)
            if info.get("file", {}).get("shares"):
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def cmd_table(args):
    import send_blocks as sb
    client = _make_client()
    channel_id = resolve_target(args.target, client)
    thread_ts = _thread_ts(args.thread)
    kind = "data_table" if args.data else "table"
    block, (nrows, ncols) = sb.build_table_block(
        read_text_arg(args.src), kind, args.align, args.caption)
    if nrows > 100 or ncols > 20:
        print(f"WARN: {nrows}x{ncols} exceeds Slack table limit 100x20", file=sys.stderr)
    domain = team_domain(client)
    dest = dest_of(channel_id, thread_ts, client)
    # TASK-001 — slack read: рендерить Block Kit table/data_table в текст (сейчас (table) placeholder) + чинить --js…
    resp = _call("chat_postMessage", _client=client, channel=channel_id,
                 text=args.caption or "(table)", blocks=[block],
                 thread_ts=thread_ts, unfurl_links=False)
    ts = resp["ts"]
    url = make_url(domain, channel_id, ts, thread_ts)
    save_last(channel_id, ts, thread_ts)
    audit("table", dest=dest.kind, name=dest.name, channel=channel_id, kind=kind, url=url)
    _notify_if_channel(args, client, dest, [url])
    print(f"→ dest={dest.kind} {dest.name} ({kind} {nrows}x{ncols})", file=sys.stderr)
    emit({"ts": ts, "url": url, "kind": kind, "rows": nrows, "cols": ncols}, args.json)


def cmd_blocks(args):
    client = _make_client()
    channel_id = resolve_target(args.target, client)
    thread_ts = _thread_ts(args.thread)
    blocks = json.loads(read_text_arg(args.src))
    if isinstance(blocks, dict):
        blocks = [blocks]
    domain = team_domain(client)
    dest = dest_of(channel_id, thread_ts, client)
    resp = _call("chat_postMessage", _client=client, channel=channel_id,
                 text=args.text or "(blocks)", blocks=blocks,
                 thread_ts=thread_ts, unfurl_links=False)
    ts = resp["ts"]
    url = make_url(domain, channel_id, ts, thread_ts)
    save_last(channel_id, ts, thread_ts)
    audit("blocks", dest=dest.kind, name=dest.name, channel=channel_id, n=len(blocks), url=url)
    _notify_if_channel(args, client, dest, [url])
    print(f"→ dest={dest.kind} {dest.name} ({len(blocks)} blocks)", file=sys.stderr)
    emit({"ts": ts, "url": url, "blocks": len(blocks)}, args.json)


def cmd_md(args):
    """Post a markdown file; each ```mermaid``` fence is rendered to a PNG and posted
    inline as a separator: [md before] -> [PNG] -> [md after]. First message anchors a
    thread; the rest reply into it (use --thread to target an existing thread)."""
    import send_blocks as sb
    md = read_text_arg(args.src)
    segs = sb.split_md_mermaid(md)
    n_mermaid = sum(1 for k, _ in segs if k == "mermaid")

    if args.dry_run:
        plan = [{"kind": "md", "blocks": len(sb.chunk_markdown(c))} if k == "md"
                else {"kind": "mermaid_png"} for k, c in segs]
        emit({"segments": len(segs), "mermaid": n_mermaid, "plan": plan}, args.json)
        return

    client = _make_client()
    channel_id = resolve_target(args.target, client)
    thread_ts = _thread_ts(args.thread)   # default None → flat sequence of top-level messages
    domain = team_domain(client)
    dest = dest_of(channel_id, thread_ts, client)
    first_url, n_md, n_png = "", 0, 0

    if args.title and thread_ts is None:   # separate parent announcement; thread the md under it
        pr = _call("chat_postMessage", _client=client, channel=channel_id,
                   text=args.title[:150], blocks=[{"type": "markdown", "text": args.title}],
                   thread_ts=None, unfurl_links=False)
        thread_ts = pr["ts"]
        first_url = make_url(domain, channel_id, pr["ts"], None)

    for kind, content in segs:
        if kind == "md":
            for blk in sb.chunk_markdown(content):
                fb = next((l for l in blk["text"].splitlines() if l.strip()), "report")[:150]
                resp = _call("chat_postMessage", _client=client, channel=channel_id,
                             text=fb, blocks=[blk], thread_ts=thread_ts, unfurl_links=False)
                if not first_url:
                    first_url = make_url(domain, channel_id, resp["ts"], thread_ts)
                n_md += 1
        else:
            png = sb.render_mermaid(content, theme=args.theme, bg=args.bg)
            up = _call("files_upload_v2", _client=client, channel=channel_id,
                       file=png, thread_ts=thread_ts, initial_comment="")
            f = (up.get("files") or [up.get("file", {})])[0]
            # order guard: file share is async — wait for it before posting the next message
            _wait_file_shared(client, f.get("id"))
            n_png += 1

    save_last(channel_id, None, thread_ts)
    audit("md", dest=dest.kind, name=dest.name, channel=channel_id,
          segments=len(segs), mermaid=n_mermaid, url=first_url)
    _notify_if_channel(args, client, dest, [first_url] if first_url else [])
    print(f"→ dest={dest.kind} {dest.name}: {n_md} md + {n_png} png "
          f"({'threaded' if thread_ts else 'flat'})", file=sys.stderr)
    emit({"segments": len(segs), "md_msgs": n_md, "png": n_png, "url": first_url}, args.json)


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(prog="slack", description="LP agent-oriented Slack CLI")
    p.add_argument("--json", action="store_true", help="output raw JSON")
    sub = p.add_subparsers(dest="cmd", required=True)

    # TASK-001 — `--json` должен работать И после подкоманды (`read <url> --json`),
    # не только перед ней (`--json read <url>`). SUPPRESS → не затирает глобальный
    # флаг, когда не передан в позиции подкоманды.
    _jsonp = argparse.ArgumentParser(add_help=False)
    _jsonp.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="output raw JSON (accepted after the subcommand too)")
    _add = sub.add_parser

    def addp(name, **kw):
        return _add(name, parents=[_jsonp], **kw)

    s = addp("send", help="send message")
    s.add_argument("target", help="#channel | @user | C123 | D123 | URL | -")
    s.add_argument("text", help="text | @file | -")
    s.add_argument("--thread", help="thread_ts or URL to reply into thread")
    s.add_argument("--no-split", action="store_true")
    s.add_argument("--notify", dest="notify", action="store_true", default=None)
    s.add_argument("--no-notify", dest="notify", action="store_false")
    s.set_defaults(func=cmd_send)

    s = addp("reply", help="reply in thread (shortcut: reply <url> <text>)")
    s.add_argument("url")
    s.add_argument("text")
    s.add_argument("--no-split", action="store_true")
    s.add_argument("--notify", dest="notify", action="store_true", default=None)
    s.add_argument("--no-notify", dest="notify", action="store_false")
    s.set_defaults(func=cmd_reply)

    s = addp("edit")
    s.add_argument("url")
    s.add_argument("text")
    s.set_defaults(func=cmd_edit)

    s = addp("delete")
    s.add_argument("url")
    s.set_defaults(func=cmd_delete)

    s = addp("read", help="read channel history or thread")
    s.add_argument("target")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--thread", action="store_true",
                   help="for Slack URL: read the whole thread for parent/reply URL")
    s.set_defaults(func=cmd_read)

    s = addp("react")
    s.add_argument("url")
    s.add_argument("emoji")
    s.set_defaults(func=cmd_react)

    s = addp("unreact")
    s.add_argument("url")
    s.add_argument("emoji")
    s.set_defaults(func=cmd_unreact)

    s = addp("upload", help="upload one or more files (single post, shared --comment)")
    s.add_argument("target")
    s.add_argument("file", nargs="+", type=win_path,
                   help="один или несколько путей: `upload <target> a.png b.png …` → один пост с N вложениями")
    s.add_argument("--comment", help="initial_comment (общий для всех файлов)")
    s.add_argument("--thread", help="URL/ts треда — файл уйдёт в тред, а не в top-level")
    s.set_defaults(func=cmd_upload)

    s = addp("me", help="show my user info")
    s.set_defaults(func=cmd_me)

    s = addp("search", help="search messages")
    s.add_argument("query")
    s.add_argument("--in", dest="in_target", help="#channel / @user / C-id")
    s.add_argument("--from", dest="from_user", help="@user")
    s.add_argument("--after", help="YYYY-MM-DD or relative")
    s.add_argument("--before", help="YYYY-MM-DD")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_search)

    s = addp("channels", help="list channels")
    s.add_argument("--filter", help="substring in name")
    s.add_argument("--refresh", action="store_true", help="force re-fetch list")
    s.set_defaults(func=cmd_channels)

    s = addp("users", help="list users")
    s.add_argument("--filter", help="substring in name/display/real")
    s.add_argument("--refresh", action="store_true")
    s.set_defaults(func=cmd_users)

    s = addp("whoami-msg", help="author of a message by URL")
    s.add_argument("url")
    s.set_defaults(func=cmd_whoami_msg)

    s = addp("files", help="list files in channel/thread (для URL тред разворачивается автоматически)")
    s.add_argument("target")
    s.add_argument("--limit", type=int, default=200,
                   help="макс. сообщений (default 200 — тред целиком; для канала это history-глубина)")
    s.add_argument("--thread", action="store_true",
                   help="форсить разворот треда (для URL включено по умолчанию)")
    s.add_argument("--no-thread", action="store_true",
                   help="для URL: только сообщение из URL, без разворота треда")
    s.add_argument("--download", metavar="DIR", type=win_path,
                   help="скачать все найденные файлы (hosted) в каталог DIR (имя = <Fid>_<name>)")
    s.set_defaults(func=cmd_files)

    s = addp("download", help="download file by F-id or URL")
    s.add_argument("file", help="F-id or url_private_download")
    s.add_argument("-o", "--output", type=win_path, help="output path (file or dir)")
    s.set_defaults(func=cmd_download)

    s = addp("reactions", help="read reactions on a message")
    s.add_argument("url")
    s.set_defaults(func=cmd_reactions)

    s = addp("mentions", help="search mentions of me")
    s.add_argument("--in", dest="in_target")
    s.add_argument("--after")
    s.add_argument("--before")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_mentions)

    s = addp("unread", help="channels/DMs/threads with unread")
    s.add_argument("--types", help="public_channel,private_channel,im,mpim")
    s.add_argument("--preview", action="store_true", help="show first unread per channel")
    s.set_defaults(func=cmd_unread)

    s = addp("table", help="post table/data_table block from TSV (first row=header)")
    s.add_argument("target", help="#channel | @user | C123 | URL")
    s.add_argument("src", help="TSV: @file | - | inline")
    s.add_argument("--data", action="store_true", help="interactive data_table (filter + fullscreen)")
    s.add_argument("--caption", default="", help="caption (for data_table)")
    s.add_argument("--align", default="", help="per-column align: l,c,r")
    s.add_argument("--thread")
    s.add_argument("--notify", dest="notify", action="store_true", default=None)
    s.add_argument("--no-notify", dest="notify", action="store_false")
    s.set_defaults(func=cmd_table)

    s = addp("md", help="post .md; ```mermaid``` fences auto-render to PNG separators")
    s.add_argument("target")
    s.add_argument("src", help="markdown: @file | - | inline")
    s.add_argument("--theme", default="default", help="mermaid theme (default/neutral/dark/forest)")
    s.add_argument("--bg", default="white", help="mermaid background (white/transparent)")
    s.add_argument("--title", default="", help="post a parent announcement msg, thread the whole md under it")
    s.add_argument("--thread", help="post into existing thread (ts or URL)")
    s.add_argument("--dry-run", action="store_true", help="show segment plan; no send/render")
    s.add_argument("--notify", dest="notify", action="store_true", default=None)
    s.add_argument("--no-notify", dest="notify", action="store_false")
    s.set_defaults(func=cmd_md)

    s = addp("blocks", help="post raw Block Kit blocks (JSON array)")
    s.add_argument("target")
    s.add_argument("src", help="JSON array: @file | - | inline")
    s.add_argument("--text", default="", help="fallback text")
    s.add_argument("--thread")
    s.add_argument("--notify", dest="notify", action="store_true", default=None)
    s.add_argument("--no-notify", dest="notify", action="store_false")
    s.set_defaults(func=cmd_blocks)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except SlackApiError as e:
        print(f"ERROR: Slack API: {e.response.get('error')}", file=sys.stderr)
        if args.json:
            print(json.dumps(dict(e.response.data), indent=2))
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
