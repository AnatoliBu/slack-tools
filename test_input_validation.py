#!/usr/bin/env python3
"""Offline tests for local input validation of the Block Kit commands
(`slack table` / `slack blocks` / `slack md`) — incl. mrkdwn-vs-Markdown.

Why this file exists: Slack answers EVERY malformed Block Kit payload with the
single word `invalid_blocks`, and the CLI used to drop the actual reason
(`response_metadata.messages`) unless `--json` was passed — so "uneven rows",
"empty cell", ">20 columns", "unread @file" and "broken JSON" all looked the
same and cost blind sends to tell apart. Everything decidable from the input is
now decided locally (exit 2, nothing sent); these tests pin each rule and its
message.

No network / tokens. Same standalone style as test_commands.py:
`python test_input_validation.py` (exit 0 = pass, 1 = fail).
"""
import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import send_blocks as sb  # noqa: E402
import slack_cli as s  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="slack_tblinput_"))
s.CACHE_DIR = _TMP
s.AUDIT_LOG = _TMP / "audit.jsonl"
s.LAST_STATE = _TMP / "last.json"


def _err(fn, *a, **kw):
    """Run fn expecting an sb.InputError; return it."""
    try:
        fn(*a, **kw)
    except sb.InputError as e:
        return e
    raise AssertionError(f"expected InputError from {getattr(fn, '__name__', fn)}")


# --- the reported scenario: printf '%s' does not expand \t ------------------

def test_printf_literal_tab_rows_is_uneven_and_names_printf():
    # exactly what `printf 'ID\tP\tЧто\n%s\n%s\n' 'BUG-121\tP1\t...' ...` produces:
    # header split by real TABs, data rows carrying backslash-t as text
    tsv = ("ID\tP\tЧто\n"
           "BUG-121\\tP1\\tплатёж PENDING\n"
           "BUG-119\\tP1\\tтокен в логах\n")
    e = _err(sb.validate_tsv, tsv)
    msg = str(e)
    assert e.code == "UNEVEN_ROWS", e.code
    assert "row 2=1" in msg and "row 3=1" in msg, msg          # which rows, how many cols
    assert "header (row 1) has 3" in msg, msg                   # what was expected
    assert "rows 2, 3:" in msg, msg                             # one hint per cause, grouped
    assert "printf" in msg and "%b" in msg, msg                 # the hypothesis + fix
    assert "uneven_table_rows_not_allowed" in msg, msg          # what Slack would have said


def test_all_rows_literal_tab_is_its_own_code():
    tsv = "ID\\tP\nBUG-121\\tP1\nBUG-119\\tP1\n"
    e = _err(sb.validate_tsv, tsv)
    assert e.code == "LITERAL_TAB", e.code
    assert "printf" in str(e) and "cat -A" in str(e)


def test_row_with_commas_says_tsv_needs_tabs():
    e = _err(sb.validate_tsv, "ID\tP\tЧто\nBUG-121,P1,платёж\n")
    assert e.code == "UNEVEN_ROWS"
    assert "no TAB at all but ',' present" in str(e), str(e)


def test_blank_line_is_named_as_blank_row():
    e = _err(sb.validate_tsv, "ID\tP\na\tb\n\nc\td\n")
    assert e.code == "UNEVEN_ROWS"
    assert "row 3: blank line" in str(e), str(e)


# --- empty / header-only input ---------------------------------------------

def test_empty_input():
    e = _err(sb.validate_tsv, "")
    assert e.code == "EMPTY_INPUT"
    assert "nothing to post" in str(e)


def test_whitespace_only_input_is_empty():
    assert _err(sb.validate_tsv, "  \n\n").code == "EMPTY_INPUT"


def test_header_only_input():
    e = _err(sb.validate_tsv, "ID\tP\tЧто\n")
    assert e.code == "SINGLE_ROW"
    assert "only 1 row (3 column(s))" in str(e), str(e)


def test_rows1_cols1_warns_about_unread_at_file():
    e = _err(sb.validate_tsv, "@rows.tsv", src="@rows.tsv")
    assert e.code == "SINGLE_ROW"
    assert "rows=1 cols=1" in str(e) and "@file" in str(e), str(e)
    assert "cat f.tsv | slack table" in str(e), str(e)


# --- empty cells (live-measured 2026-08-07) --------------------------------

def test_empty_header_cell_rejected_with_column_number():
    tsv = "ID\tP\tЧто\t\nBUG-121\tP1\tплатёж\t🟢\n"
    e = _err(sb.validate_tsv, tsv)
    assert e.code == "EMPTY_CELL"
    assert "row 1 col 4" in str(e), str(e)
    assert "give column 4 a name" in str(e), str(e)


def test_empty_body_cell_also_rejected():
    # measured: Slack rejects a zero-length cell in ANY row, not only the header
    e = _err(sb.validate_tsv, "ID\tP\tЧто\nBUG-121\t\tплатёж\n")
    assert e.code == "EMPTY_CELL"
    assert "row 2 col 2" in str(e), str(e)
    assert "must be more than 0 characters" in str(e), str(e)


def test_trailing_tab_on_every_row_is_empty_cell_with_hint():
    e = _err(sb.validate_tsv, "ID\tP\t\na\tb\t\n")
    assert e.code == "EMPTY_CELL"
    assert "row 1 ends with a TAB" in str(e), str(e)


def test_trailing_tab_on_one_row_is_uneven_with_hint():
    e = _err(sb.validate_tsv, "ID\tP\na\tb\t\n")
    assert e.code == "UNEVEN_ROWS"
    assert "row 2: trailing TAB" in str(e), str(e)


def test_space_only_cell_is_accepted():
    # measured 2026-08-07: " " renders fine; only "" is rejected — do not false-flag it
    rows = sb.validate_tsv("ID\tP\t \nBUG-121\tP1\t \n")
    assert rows[0][2] == " "


# --- Block Kit 100x20 limits ------------------------------------------------

def test_too_many_rows():
    tsv = "\n".join(f"a{i}\tb{i}" for i in range(101))
    e = _err(sb.validate_tsv, tsv)
    assert e.code == "LIMIT_EXCEEDED"
    assert "101x2" in str(e) and "100 rows x 20 columns" in str(e), str(e)
    assert "head -n 100" in str(e), str(e)


def test_too_many_cols():
    row = "\t".join(f"c{i}" for i in range(21))
    e = _err(sb.validate_tsv, row + "\n" + row + "\n")
    assert e.code == "LIMIT_EXCEEDED"
    assert "2x21" in str(e), str(e)
    assert "21 columns > 20" in str(e), str(e)


def test_exactly_at_limit_is_allowed():
    row = "\t".join(f"c{i}" for i in range(20))
    rows = sb.validate_tsv("\n".join([row] * 100))
    assert len(rows) == 100 and len(rows[0]) == 20


# --- happy path: builder output unchanged -----------------------------------

def test_valid_table_block_shape_and_align_unchanged():
    blk, (nrows, ncols) = sb.build_table_block("ID\tP\tЧто\nBUG-121\tP1\tплатёж\n",
                                               "table", "l,c,l", "")
    assert (nrows, ncols) == (2, 3)
    assert blk["type"] == "table"
    assert blk["rows"][0][0] == {"type": "raw_text", "text": "ID"}
    assert blk["column_settings"] == [{"align": "left"}, {"align": "center"},
                                      {"align": "left"}]


def test_data_table_keeps_caption_and_drops_column_settings():
    blk, _ = sb.build_table_block("a\tb\nc\td\n", "data_table", "l,c", "Cap")
    assert blk["caption"] == "Cap"
    assert "column_settings" not in blk


# --- raw blocks JSON --------------------------------------------------------

def test_blocks_invalid_json_reports_parser_detail_and_preview():
    e = _err(sb.parse_blocks_json, '[{"type": "markdown", "text": "hi"')
    assert e.code == "INVALID_JSON"
    assert "parser said:" in str(e) and "line 1" in str(e), str(e)
    assert "input started with:" in str(e), str(e)


def test_blocks_unread_at_file_is_named():
    e = _err(sb.parse_blocks_json, "@blocks.json", src="@blocks.json")
    assert e.code == "INVALID_JSON"
    assert "the file was NOT read" in str(e), str(e)


def test_blocks_block_without_type():
    e = _err(sb.parse_blocks_json, '[{"text": "hi"}]')
    assert e.code == "BLOCK_NO_TYPE"
    assert "blocks[0]" in str(e) and "keys present: text" in str(e), str(e)


def test_blocks_non_object_element():
    e = _err(sb.parse_blocks_json, '["markdown"]')
    assert e.code == "BLOCK_NOT_OBJECT"
    assert "blocks[0] is str" in str(e), str(e)


def test_blocks_single_object_is_wrapped():
    assert sb.parse_blocks_json('{"type": "markdown", "text": "hi"}')[0]["type"] == "markdown"


def test_blocks_empty_array():
    assert _err(sb.parse_blocks_json, "[]").code == "EMPTY_INPUT"


# --- CLI wiring: strict @file, exit 2, no network ---------------------------

def test_read_text_arg_strict_fails_loud_on_unread_file():
    e = _err(s.read_text_arg_strict, "@/nope/missing.tsv", "table")
    assert e.code == "FILE_NOT_READ"
    assert "was not read as a file" in str(e)
    assert "tried:" in str(e) and "cat <file> | slack table" in str(e), str(e)


def test_read_text_arg_strict_passes_through_stdin_and_inline():
    assert s.read_text_arg_strict("a\tb", "table") == "a\tb"


def test_read_text_arg_strict_reads_existing_file():
    f = _TMP / "rows.tsv"
    f.write_text("a\tb\nc\td\n", encoding="utf-8")
    assert s.read_text_arg_strict(f"@{f}", "table").startswith("a\tb")


def test_main_exits_2_on_bad_table_input_without_touching_network():
    argv = sys.argv
    sys.argv = ["slack", "table", "#chan", "ID\tP\tЧто\nBUG-121\\tP1\\tплатёж"]
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            s.main()
        raise AssertionError("expected SystemExit(2)")
    except SystemExit as e:
        assert e.code == 2, e.code
    finally:
        sys.argv = argv
    out = err.getvalue()
    assert "uneven table rows" in out and "printf" in out, out


def test_main_json_mode_emits_machine_readable_code():
    argv = sys.argv
    sys.argv = ["slack", "table", "#chan", "solo-line", "--json"]
    buf, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            s.main()
        raise AssertionError("expected SystemExit(2)")
    except SystemExit as e:
        assert e.code == 2
    finally:
        sys.argv = argv
    rec = json.loads(buf.getvalue())
    assert rec["ok"] is False and rec["error"] == "SINGLE_ROW"
    assert rec["hints"]


# --- mrkdwn (*bold*) vs Markdown (**bold**) in a `markdown` block -----------
# Live-measured 2026-08-07: a markdown block with `*single* vs **double**` comes
# back from Slack as style.italic / style.bold respectively — mrkdwn bold is
# silently downgraded to italic, exit 0, no error.

def test_mrkdwn_spans_detected():
    spans = sb.mrkdwn_bold_spans("*Кейс* — про *BUG-121* и всё")
    assert spans == ["*Кейс*", "*BUG-121*"], spans


def test_markdown_bold_is_not_flagged():
    assert sb.mrkdwn_bold_spans("**Кейс** — про **BUG-121**") == []
    assert sb.has_markdown_bold("**bold**") is True


def test_bullets_and_code_are_not_flagged():
    txt = "* bullet one\n* bullet two\n`a * b * c`\n```\nx = *p;\n```\n"
    assert sb.mrkdwn_bold_spans(txt) == []


def test_warning_fires_only_when_no_real_markdown_bold():
    assert sb.mrkdwn_warning("*Кейс* и *Корень*") is not None
    # mixed source: the author clearly knows Markdown → no nagging
    assert sb.mrkdwn_warning("**Кейс** и *курсив*") is None
    assert sb.mrkdwn_warning("plain text") is None


def test_warning_text_names_the_rule_and_the_fix():
    w = sb.mrkdwn_warning("*Кейс* *BUG-121* *Корень* *Ещё*")
    assert "4 single-star span(s)" in w and "`*Кейс*`" in w
    assert "(+1 more)" in w
    assert "ITALIC, not bold" in w and "bold = **x**" in w
    assert "--mrkdwn" in w
    assert "write **x** for bold" in sb.mrkdwn_warning("*x*", convert_flag=None)


def test_conversion_leaves_code_bullets_and_existing_bold_alone():
    src = "*Кейс*: см. `*ptr*` и\n```\nchar *p = *q;\n```\n* bullet\n**already**\n"
    out = sb.mrkdwn_to_markdown(src)
    assert out.startswith("**Кейс**:")
    assert "`*ptr*`" in out                    # inline code untouched
    assert "char *p = *q;" in out              # fenced code untouched
    assert "\n* bullet\n" in out               # list bullet untouched
    assert "**already**" in out and "****already****" not in out


def test_conversion_is_idempotent():
    once = sb.mrkdwn_to_markdown("*x* and *y*")
    assert once == "**x** and **y**"
    assert sb.mrkdwn_to_markdown(once) == once


def test_md_command_has_mrkdwn_flag():
    p = s.build_parser()
    assert p.parse_args(["md", "#c", "@r.md", "--mrkdwn"]).mrkdwn is True
    assert p.parse_args(["md", "#c", "@r.md"]).mrkdwn is False


def test_md_dry_run_warns_before_anything_is_sent():
    argv = sys.argv
    sys.argv = ["slack", "md", "#chan", "*Кейс*\n\ntext", "--dry-run"]
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            s.main()
    finally:
        sys.argv = argv
    assert "WARN:" in err.getvalue() and "ITALIC, not bold" in err.getvalue()


# --- Slack API error is no longer collapsed to one word ---------------------

class _Resp:
    def __init__(self, data):
        self.data = data

    def get(self, k, default=None):
        return self.data.get(k, default)


def test_api_error_details_surfaces_response_metadata_messages():
    resp = _Resp({"ok": False, "error": "invalid_blocks", "response_metadata": {
        "messages": ["[ERROR] uneven_table_rows_not_allowed [json-pointer:/blocks/0/]"]}})
    lines = s.api_error_details(resp)
    assert lines == ["[ERROR] uneven_table_rows_not_allowed [json-pointer:/blocks/0/]"]


def test_api_error_details_surfaces_needed_provided_and_warnings():
    resp = _Resp({"ok": False, "error": "missing_scope", "needed": "chat:write",
                  "provided": "identify", "warning": "superfluous_charset",
                  "response_metadata": {"warnings": ["superfluous_charset"]}})
    lines = s.api_error_details(resp)
    assert "warning: superfluous_charset" in lines
    assert "needed: chat:write" in lines and "provided: identify" in lines


def test_api_error_details_never_raises_on_junk():
    assert s.api_error_details(object()) == []


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
