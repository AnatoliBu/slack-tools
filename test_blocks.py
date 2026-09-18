#!/usr/bin/env python3
"""
test_blocks.py - probe which Block Kit blocks render with our USER token (xoxc/xoxd).

Sends each block type as a separate DM to self, reports ok/error per type.
Self-DM channel = own user_id (Slack opens IM automatically).

Env: SLACK_XOXC, SLACK_XOXD (auto-loaded from .env)
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    rc = getattr(_s, "reconfigure", None)
    if rc:
        try:
            rc(encoding="utf-8")
        except OSError:
            pass

ENV_FILE = Path(__file__).resolve().parent / ".env"


def load_env():
    if os.environ.get("SLACK_XOXC") and os.environ.get("SLACK_XOXD"):
        return
    if ENV_FILE.exists():
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    for k in ("SLACK_XOXC", "SLACK_XOXD"):
        if not os.environ.get(k):
            sys.exit(f"ERROR: {k} not set")


def post(data):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage", data=body,
        headers={
            "Authorization": f"Bearer {os.environ['SLACK_XOXC']}",
            "Cookie": f"d={os.environ['SLACK_XOXD']}",
            "Content-Type": "application/json; charset=utf-8",
        }, method="POST",
    )
    return json.loads(urllib.request.urlopen(req).read())


def self_id():
    """Own user id — $SLACK_SELF_ID or resolved via auth.test (DM-to-self target)."""
    if os.environ.get("SLACK_SELF_ID"):
        return os.environ["SLACK_SELF_ID"]
    req = urllib.request.Request(
        "https://slack.com/api/auth.test", data=b"",
        headers={
            "Authorization": f"Bearer {os.environ['SLACK_XOXC']}",
            "Cookie": f"d={os.environ['SLACK_XOXD']}",
        }, method="POST",
    )
    return json.loads(urllib.request.urlopen(req).read())["user_id"]


# (name, fallback_text, block)
CASES = [
    ("table", "Block test: table", {
        "type": "table",
        "column_settings": [{}, {"align": "center"}, {}],
        "rows": [
            [{"type": "raw_text", "text": "Client"},
             {"type": "raw_text", "text": "SpecialState"},
             {"type": "raw_text", "text": "Result"}],
            [{"type": "raw_number", "text": "37085796"},
             {"type": "raw_number", "text": "2"},
             {"type": "raw_text", "text": "401 CLIENT_DELETED"}],
            [{"type": "raw_number", "text": "50709247"},
             {"type": "raw_number", "text": "6"},
             {"type": "raw_text", "text": "OK"}],
        ],
    }),
    ("markdown", "Block test: markdown", {
        "type": "markdown",
        "text": "**markdown block** — bold, _italic_, `code`, [link](https://slack.com)",
    }),
    ("alert", "Block test: alert", {
        "type": "alert",
        "text": {"type": "mrkdwn", "text": "Alert block (info level)"},
        "level": "info",
    }),
    ("card", "Block test: card", {
        "type": "card",
        "title": {"type": "plain_text", "text": "Sample Card"},
    }),
    ("carousel", "Block test: carousel", {
        "type": "carousel",
        "elements": [
            {"type": "card", "block_id": "c1",
             "title": {"type": "mrkdwn", "text": "Card 1"}},
            {"type": "card", "block_id": "c2",
             "title": {"type": "mrkdwn", "text": "Card 2"}},
        ],
    }),
    ("data_table", "Block test: data_table", {
        "type": "data_table",
        "caption": "Sample data_table",
        "rows": [
            [{"type": "raw_text", "text": "H1"}, {"type": "raw_text", "text": "H2"}],
            [{"type": "raw_text", "text": "a"}, {"type": "raw_text", "text": "b"}],
        ],
    }),
]


def main():
    load_env()
    self_dm = self_id()
    results = []
    for name, fallback, block in CASES:
        try:
            r = post({"channel": self_dm, "text": fallback, "blocks": [block]})
            if r.get("ok"):
                results.append((name, "OK", r.get("ts", "")))
            else:
                err = r.get("error", "?")
                meta = r.get("response_metadata", {}).get("messages", [])
                results.append((name, f"ERR:{err}", " | ".join(meta)[:200]))
        except Exception as e:
            results.append((name, "EXC", str(e)[:200]))
        time.sleep(0.6)

    print("\n=== RESULTS (user-token xoxc/xoxd) ===")
    for name, status, detail in results:
        print(f"{name:12} {status:18} {detail}")


if __name__ == "__main__":
    main()


import send_blocks as sb

# --- note-cli frontmatter -> компактная шапка -----------------------------------------------
#
# Заметка реестра постится в Slack КАК ЕСТЬ (`slack md @_docs/bugs/BUG-NNN-*.md`), но её YAML-шапка
# адресована гейтам: schema_version, fingerprint, body_hash, sessions, aliases. Замер 2026-09-18 на
# BUG-219: 24 строки frontmatter против трёх полезных, и сам дефект уезжал ниже первого экрана.

def test_note_frontmatter_folds_into_one_header_line():
    md = (
        "---\n"
        "id: BUG-219\n"
        "schema_version: 2\n"
        "status: OPEN\n"
        "severity: P2\n"
        "title: 'BUG-219: validation-service отдаёт сырой ключ'\n"
        "fingerprint: 17f36119\n"
        "body_hash: 152c2b1d\n"
        "side: server\n"
        "---\n"
        "\n## Описание\nтело\n"
    )
    out, note_id = sb.note_to_post(md)
    assert note_id == "BUG-219"
    assert out.splitlines()[0] == "*BUG-219 · P2 · server*"
    assert out.splitlines()[1] == "validation-service отдаёт сырой ключ"   # префикс ID не дублируется
    assert "fingerprint" not in out and "body_hash" not in out and "schema_version" not in out
    assert "## Описание" in out and "тело" in out


def test_not_a_note_is_returned_unchanged():
    """Обычный markdown-отчёт трогать нельзя — у него нет id и шапку выдумывать не из чего."""
    md = "# Отчёт\n\nтекст\n"
    assert sb.note_to_post(md) == (md, None)


def test_yaml_without_note_id_is_left_alone():
    """Frontmatter бывает не только у заметок (jekyll и прочее) — без `id` не трогаем."""
    md = "---\nlayout: post\ntitle: Hello\n---\n\nтело\n"
    assert sb.note_to_post(md) == (md, None)


def test_undetermined_side_is_not_printed():
    """`side: undetermined` — честное «не доказано», в шапку поста ему нечего добавить."""
    md = "---\nid: TASK-469\nstatus: TODO\nseverity: P2\nside: undetermined\ntitle: 'TASK-469: заголовок'\n---\n\nтело\n"
    out, _ = sb.note_to_post(md)
    assert out.splitlines()[0] == "*TASK-469 · P2 · TODO*"


def test_open_status_is_not_noise_in_the_header():
    """OPEN — состояние по умолчанию; печатать его в каждой шапке значит печатать шум."""
    md = "---\nid: BUG-230\nstatus: OPEN\nseverity: P2\nside: server\ntitle: 'BUG-230: заголовок'\n---\n\nтело\n"
    out, _ = sb.note_to_post(md)
    assert out.splitlines()[0] == "*BUG-230 · P2 · server*"
