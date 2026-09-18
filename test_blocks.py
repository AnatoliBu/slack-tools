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

# --- note-cli: YAML снимается, шапка НЕ сочиняется --------------------------------------------
#
# Заметка реестра постится в Slack КАК ЕСТЬ. Снимается только YAML (он для гейтов: schema_version,
# fingerprint, body_hash, sessions, aliases — 24 строки против трёх полезных, замер на BUG-219).
# Заметная строка «пин · BUG-NNN · P1 · server» живёт В ФАЙЛЕ, первой строкой тела, и тул её не
# трогает — решение владельца 2026-09-18: «надо в изначальном файле чтобы было, а слак тулс просто
# постил».

HEADER_LINE = "📌 · BUG-219 · P2 · server"


def test_yaml_is_stripped_and_body_keeps_its_own_header():
    md = ("---@id: BUG-219@schema_version: 2@fingerprint: 17f36119@body_hash: 152c2b1d@---@@"
          + HEADER_LINE + "@validation-service отдаёт сырой ключ@@## Описание@тело@").replace("@", chr(10))
    out, note_id = sb.note_to_post(md)
    assert note_id == "BUG-219"
    assert out.splitlines()[0] == HEADER_LINE          # взято ИЗ ФАЙЛА, не собрано тулом
    for gate_field in ("fingerprint", "body_hash", "schema_version"):
        assert gate_field not in out


def test_the_tool_invents_no_header():
    """Нет шапки в файле — тул её не добавляет: пост начинается тем же, чем тело заметки."""
    md = ("---@id: BUG-230@severity: P2@side: server@title: 'BUG-230: заголовок'@---@@"
          "## Описание@тело@").replace("@", chr(10))
    out, _ = sb.note_to_post(md)
    assert out.startswith("## Описание")
    assert "BUG-230" not in out and "P2" not in out


def test_not_a_note_is_returned_unchanged():
    md = ("# Отчёт@@текст@").replace("@", chr(10))
    assert sb.note_to_post(md) == (md, None)


def test_yaml_without_note_id_is_left_alone():
    """Frontmatter бывает не только у заметок (jekyll и прочее) — без `id` не трогаем."""
    md = ("---@layout: post@title: Hello@---@@тело@").replace("@", chr(10))
    assert sb.note_to_post(md) == (md, None)


# --- chunk_markdown: разрез не рвёт code fence -------------------------------------------------

def _texts(blocks):
    return [b["text"] for b in blocks]


def test_split_inside_code_fence_closes_and_reopens():
    """Длинный ```-блок на границе chunk'а: хвост закрыт, продолжение открыто заново с языком."""
    body = "\n".join(f"line {i} " + "x" * 40 for i in range(40))
    md = "интро\n\n```python\n" + body + "\n```\n"
    blocks = _texts(sb.chunk_markdown(md, lim=500))
    assert len(blocks) > 1
    for t in blocks:
        assert t.count("```") % 2 == 0, t
    assert blocks[0].rstrip().endswith("```")
    for t in blocks[1:]:
        assert t.startswith("```python")


def test_no_fence_split_unchanged():
    md = "\n".join(f"строка {i}" for i in range(50))
    blocks = _texts(sb.chunk_markdown(md, lim=200))
    assert "```" not in "".join(blocks)
    assert "\n".join(blocks).replace("\n", "") == md.replace("\n", "")


def test_closed_fence_is_not_reopened():
    """Fence, закрывшийся до границы, не должен тащиться в следующий chunk."""
    md = "```\ncode\n```\n" + "\n".join(f"текст {i}" for i in range(60))
    blocks = _texts(sb.chunk_markdown(md, lim=120))
    assert blocks[0].count("```") == 2
    assert all("```" not in t for t in blocks[1:])
