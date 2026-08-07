#!/usr/bin/env python3
"""TASK-003 — `slack read` разворачивает `blocks`, а `--json` кладёт РОВНО один документ.

Два измеренных дефекта чтения (07.08.2026, тред C08FVRMDTUN/p1786098551116109):

1. сообщение, чьё тело живёт в `blocks` (rich_text + table), печаталось ОДНОЙ первой
   строкой из `text`-фоллбэка. Цена — ложные выводы читающего агента: «тред обрывается
   на висячем заголовке» (заголовок не висячий — под ним 2400 символов) и «сломан bold,
   в тексте литеральные `**`» (в блоках настоящий `style.bold`; `**` есть только в
   фоллбэке, который Slack не показывает);
2. `--json` мог отдать несколько JSON-документов подряд → `json.loads` падал с
   `Extra data: line N column M`, и это читалось как «CLI сломан».

Сеть НЕ трогаем: клиент и выборка сообщений подменяются заглушками.
Запуск: `python -m pytest test_read_blocks.py` или `python test_read_blocks.py`.
"""
import argparse
import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import slack_cli as s  # noqa: E402

# ---------------------------------------------------------------------------
# Фикстуры — форма реальных сообщений из треда (структура снята с payload Slack)
# ---------------------------------------------------------------------------
_RICH_INLINE = {
    "type": "rich_text",
    "elements": [{
        "type": "rich_text_section",
        "elements": [
            {"type": "text", "text": "ПОРЯДОК ПОЧИНКИ", "style": {"bold": True}},
            {"type": "text", "text": "\n\n"},
            {"type": "emoji", "name": "hammer_and_wrench", "unicode": "1f6e0-fe0f"},
            {"type": "text", "text": " смотри "},
            {"type": "text", "text": "GatewayResourceConfig", "style": {"code": True}},
            {"type": "text", "text": " в "},
            {"type": "link", "url": "https://gitlab/mr/4", "text": "MR !4"},
            {"type": "text", "text": " — "},
            {"type": "text", "text": "срочно", "style": {"italic": True}},
            {"type": "text", "text": " и "},
            {"type": "text", "text": "не откладывать", "style": {"strike": True}},
        ],
    }],
}

_RICH_LIST = {
    "type": "rich_text",
    "elements": [
        {"type": "rich_text_list", "style": "bullet", "indent": 0, "elements": [
            {"type": "rich_text_section", "elements": [
                {"type": "text", "text": "PaymentExternalDeclineApiTest",
                 "style": {"code": True}},
                {"type": "text", "text": " → исчерпанный общий ресурс"},
            ]},
            {"type": "rich_text_section", "elements": [
                {"type": "text", "text": "пять красных вечернего прогона"}]},
        ]},
        {"type": "rich_text_list", "style": "ordered", "indent": 1, "offset": 0,
         "elements": [
             {"type": "rich_text_section", "elements": [
                 {"type": "text", "text": "снять REQUIRES_NEW"}]},
             {"type": "rich_text_section", "elements": [
                 {"type": "text", "text": "провести лимиты"}]},
         ]},
    ],
}

_RICH_PRE = {
    "type": "rich_text",
    "elements": [
        {"type": "rich_text_section", "elements": [
            {"type": "text", "text": "лог:", "style": {"bold": True}},
            {"type": "text", "text": "\n"}]},
        {"type": "rich_text_preformatted", "elements": [
            {"type": "text", "text": "1  BUG-119   маскировать bind-параметры\n"
                                     "2  BUG-121   снять REQUIRES_NEW"}]},
    ],
}

_RICH_QUOTE = {
    "type": "rich_text",
    "elements": [{"type": "rich_text_quote", "elements": [
        {"type": "text", "text": "рестарт НЕ лечит\nзаклинило снова через 14 с"}]}],
}

_TABLE = {
    "type": "table",
    "rows": [
        [{"type": "raw_text", "text": "ID"},
         {"type": "raw_text", "text": "P"},
         {"type": "raw_text", "text": "Что сломано"}],
        [{"type": "raw_text", "text": "BUG-121"},
         {"type": "raw_text", "text": "P1"},
         {"type": "raw_text", "text": "платёж навсегда PENDING"}],
    ],
}
# ячейка может быть и rich_text, а не только raw_text — наивный обход
# «искать только type == text» давал ПУСТЫЕ ячейки на реальном payload
_TABLE_RICH_CELLS = {
    "type": "table",
    "rows": [
        [{"type": "raw_text", "text": "Источник"},
         {"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [
             {"type": "text", "text": "Улика", "style": {"bold": True}}]}]}],
        [{"type": "raw_number", "text": "593"},
         {"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [
             {"type": "text", "text": "EXPIRED"}, {"type": "text", "text": " = 0"}]}]}],
    ],
}


def _msg(text="", blocks=None, ts="1786100740.088839"):
    m = {"ts": ts, "user": "U1", "text": text}
    if blocks is not None:
        m["blocks"] = blocks
    return m


# ---------------------------------------------------------------------------
# rich_text → текст
# ---------------------------------------------------------------------------
def test_inline_styles_become_mrkdwn():
    out = s._render_block(_RICH_INLINE)
    assert "*ПОРЯДОК ПОЧИНКИ*" in out                     # bold
    assert "`GatewayResourceConfig`" in out               # code
    assert "_срочно_" in out                              # italic
    assert "~не откладывать~" in out                      # strike
    assert ":hammer_and_wrench:" in out                   # emoji
    assert "<https://gitlab/mr/4|MR !4>" in out           # link


def test_style_markers_do_not_swallow_outer_whitespace():
    el = {"type": "text", "text": "  жирное  ", "style": {"bold": True}}
    assert s._rt_to_str(el) == "  *жирное*  "


def test_whitespace_only_text_is_not_wrapped():
    el = {"type": "text", "text": " ", "style": {"bold": True}}
    assert s._rt_to_str(el) == " "


def test_bold_plus_code_nests_code_inside():
    el = {"type": "text", "text": "x", "style": {"bold": True, "code": True}}
    assert s._rt_to_str(el) == "*`x`*"


def test_link_without_label_is_bare_url():
    assert s._rt_to_str({"type": "link", "url": "https://x"}) == "https://x"


def test_mention_elements():
    assert s._rt_to_str({"type": "user", "user_id": "U123"}) == "@U123"
    assert s._rt_to_str({"type": "channel", "channel_id": "C123"}) == "#C123"
    assert s._rt_to_str({"type": "broadcast", "range": "here"}) == "@here"


def test_bullet_and_ordered_lists():
    out = s._render_block(_RICH_LIST)
    assert "• `PaymentExternalDeclineApiTest` → исчерпанный общий ресурс" in out
    assert "• пять красных вечернего прогона" in out
    assert "    1. снять REQUIRES_NEW" in out             # ordered + indent=1
    assert "    2. провести лимиты" in out


def test_preformatted_is_fenced_and_keeps_raw_text():
    out = s._render_block(_RICH_PRE)
    assert out.startswith("*лог:*")
    assert "```\n1  BUG-119   маскировать bind-параметры" in out
    assert out.rstrip().endswith("```")


def test_preformatted_does_not_apply_styles():
    block = {"type": "rich_text", "elements": [
        {"type": "rich_text_preformatted", "elements": [
            {"type": "text", "text": "plain", "style": {"bold": True}}]}]}
    assert "*plain*" not in s._render_block(block)


def test_quote_prefixes_every_line():
    assert s._render_block(_RICH_QUOTE) == "> рестарт НЕ лечит\n> заклинило снова через 14 с"


# ---------------------------------------------------------------------------
# table — ячейки не должны быть пустыми (регресс наивного обхода)
# ---------------------------------------------------------------------------
def test_table_cells_are_not_empty():
    out = s._render_block(_TABLE)
    assert "BUG-121" in out and "платёж навсегда PENDING" in out
    assert out.splitlines()[0].startswith("ID      | P  | Что сломано")


def test_table_rich_text_cells_render():
    out = s._render_block(_TABLE_RICH_CELLS)
    assert "*Улика*" in out
    assert "593" in out and "EXPIRED = 0" in out
    assert "|  |" not in out  # ни одной пустой ячейки


# ---------------------------------------------------------------------------
# другие типы блоков
# ---------------------------------------------------------------------------
def test_section_header_markdown_context_divider():
    assert s._render_block({"type": "section",
                            "text": {"type": "mrkdwn", "text": "*hi*"}}) == "*hi*"
    assert s._render_block({"type": "header",
                            "text": {"type": "plain_text", "text": "H"}}) == "H"
    assert s._render_block({"type": "markdown", "text": "**md**"}) == "**md**"
    assert s._render_block({"type": "context", "elements": [
        {"type": "mrkdwn", "text": "a"}, {"type": "mrkdwn", "text": "b"}]}) == "a b"
    assert s._render_block({"type": "divider"}) == "---"


def test_render_blocks_drops_empty_and_keeps_type():
    got = s._render_blocks([{"type": "actions", "elements": []}, _TABLE])
    assert [t for t, _ in got] == ["table"]


# ---------------------------------------------------------------------------
# cmd_read — blocks побеждают fallback-текст
# ---------------------------------------------------------------------------
def _read_output(msgs, as_json=False, limit=60):
    """cmd_read без сети: клиент и выборка сообщений заглушены. → (stdout, stderr)."""
    orig = (s._make_client, s._messages_for_target, s._user_name, s._JSON_EMITTED)
    s._make_client = lambda *a, **k: object()
    s._messages_for_target = lambda *a, **k: ("C1", msgs)
    s._user_name = lambda *a, **k: "a.buchnev"
    s._JSON_EMITTED = False
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            s.cmd_read(argparse.Namespace(target="URL", limit=limit,
                                          thread=True, json=as_json))
    finally:
        s._make_client, s._messages_for_target, s._user_name, s._JSON_EMITTED = orig
    return out.getvalue(), err.getvalue()


# TASK-003 — TASK-003: read: разворачивать blocks вместо text-фоллбэка + ровно один JSON-документ на stdout
def test_blocks_win_over_text_fallback():
    """Главный дефект: `text` — фоллбэк, Slack его не показывает; печатать его
    ВМЕСТО тела = врать читающему («тред обрывается на висячем заголовке»)."""
    fallback = "**ПОРЯДОК ПОЧИНКИ**"
    out, _ = _read_output([_msg(fallback, [_RICH_INLINE])])
    assert fallback not in out                    # литеральные ** не попали в вывод
    assert "*ПОРЯДОК ПОЧИНКИ*" in out             # развёрнут настоящий style.bold
    assert "`GatewayResourceConfig`" in out       # тело блока, которого не было в fallback


def test_body_is_much_longer_than_fallback():
    fallback = "**ПОРЯДОК ПОЧИНКИ**"
    out, _ = _read_output([_msg(fallback, [_RICH_INLINE, _RICH_PRE, _RICH_LIST])])
    body = out.split("\n", 1)[1]                  # без строки-заголовка `--- @user ...`
    assert len(body) > 4 * len(fallback)


def test_text_only_message_still_printed():
    out, _ = _read_output([_msg("просто текст без блоков")])
    assert "просто текст без блоков" in out


def test_message_without_text_and_blocks_keeps_placeholder_line():
    out, _ = _read_output([_msg("")])
    assert out.splitlines()[1] == "  "


def test_table_placeholder_text_dropped_but_table_kept():
    out, _ = _read_output([_msg("(table)", [_TABLE])])
    assert "(table)" not in out
    assert "BUG-121 | P1 | платёж навсегда PENDING" in out


def test_table_caption_from_text_survives():
    """`slack table --caption X` кладёт подпись ТОЛЬКО в `text` (обычный `table`
    не носит caption) — снести её вместе с фоллбэком значило бы потерять данные."""
    out, _ = _read_output([_msg("Сводка по багам", [_TABLE])])
    assert "Сводка по багам" in out


def test_data_table_caption_not_duplicated():
    dt = {**_TABLE, "type": "data_table", "caption": "QA states"}
    out, _ = _read_output([_msg("QA states", [dt])])
    assert out.count("QA states") == 1


def test_limit_warning_still_emitted_to_stderr():
    _, err = _read_output([_msg("x") for _ in range(3)], limit=3)
    assert "[warn]" in err and "--limit 3" in err


def test_file_attachment_label_still_printed():
    m = _msg("", [_RICH_INLINE])
    m["files"] = [{"id": "F123", "title": "t.log", "pretty_type": "Plain Text",
                   "size": 2048}]
    out, _ = _read_output([m])
    assert "[file F123]" in out and "slack download F123" in out


# ---------------------------------------------------------------------------
# --json — ровно один документ на stdout
# TASK-003 — TASK-003: read: разворачивать blocks вместо text-фоллбэка + ровно один JSON-документ на stdout
# ---------------------------------------------------------------------------
def test_json_is_exactly_one_document():
    out, err = _read_output([_msg("a", [_RICH_INLINE]), _msg("b", [_TABLE])],
                            as_json=True)
    parsed = json.loads(out)                      # упал бы с `Extra data`, будь их два
    assert len(parsed) == 2
    assert _count_json_docs(out) == 1
    assert err == ""                              # диагностика не смешивается с JSON


def test_json_mode_prints_nothing_but_json():
    out, _ = _read_output([_msg("x") for _ in range(5)], as_json=True, limit=5)
    assert out.lstrip().startswith("[") and out.rstrip().endswith("]")


def test_json_drops_none_messages():
    out, _ = _read_output([_msg("a"), None], as_json=True)
    assert len(json.loads(out)) == 1


def _count_json_docs(raw: str) -> int:
    dec, i, n = json.JSONDecoder(), 0, 0
    while i < len(raw):
        while i < len(raw) and raw[i] in " \r\n\t":
            i += 1
        if i >= len(raw):
            break
        _, i = dec.raw_decode(raw, i)
        n += 1
    return n


def test_error_json_goes_to_stderr_once_a_document_was_emitted():
    """Второй документ на stdout — ровно то, что ломало `json.loads` у вызывающего."""
    orig = s._JSON_EMITTED
    out, err = io.StringIO(), io.StringIO()
    try:
        s._JSON_EMITTED = True
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            s._emit_error_json({"ok": False})
    finally:
        s._JSON_EMITTED = orig
    assert out.getvalue() == ""
    assert json.loads(err.getvalue())["ok"] is False


def test_error_json_goes_to_stdout_when_nothing_emitted_yet():
    orig = s._JSON_EMITTED
    out = io.StringIO()
    try:
        s._JSON_EMITTED = False
        with contextlib.redirect_stdout(out):
            s._emit_error_json({"ok": False})
    finally:
        s._JSON_EMITTED = orig
    assert json.loads(out.getvalue())["ok"] is False
# TASK-003 — TASK-003: read: разворачивать blocks вместо text-фоллбэка + ровно один JSON-документ на stdout


def test_token_refresh_subprocess_never_writes_to_our_stdout():
    """extract_tokens.py печатает ~30 строк прогресса; по умолчанию subprocess
    наследует наш stdout → они приземлялись ПЕРЕД json-ом."""
    code = ("import sys, slack_cli as s;"
            "s._run_quiet([sys.executable, '-c', "
            "\"print('progress to stdout'); import sys; print('to stderr', file=sys.stderr)\"]);"
            "print('[]')")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          cwd=str(Path(__file__).resolve().parent))
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == []          # на stdout ТОЛЬКО документ
    assert "progress to stdout" in done.stderr
    assert "to stderr" in done.stderr


def _run():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
