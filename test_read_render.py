#!/usr/bin/env python3
"""Tests for TASK-001 (Block Kit table render in `read` + `--json` after the
subcommand) and TASK-002 (multi-file upload argparse). No network — pure
parsing/rendering. Run: `python test_read_render.py` (exit 0 = pass).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import slack_cli as s  # noqa: E402

# --- TASK-001: table/data_table block → text -------------------------------

_TABLE = {
    "type": "table",
    "rows": [
        [{"type": "raw_text", "text": "Client"},
         {"type": "raw_text", "text": "State"},
         {"type": "raw_text", "text": "Result"}],
        [{"type": "raw_number", "text": "37085796"},
         {"type": "raw_number", "text": "2"},
         {"type": "raw_text", "text": "401 CLIENT_DELETED"}],
    ],
}
_DATA_TABLE = {**_TABLE, "type": "data_table", "caption": "QA states"}


def test_render_table_aligns_and_has_header_separator():
    out = s._render_table_block(_TABLE)
    lines = out.splitlines()
    assert lines[0] == "Client   | State | Result"
    assert set(lines[1]) <= {"-", "+"}          # header separator row
    assert lines[2] == "37085796 | 2     | 401 CLIENT_DELETED"


def test_render_data_table_prepends_caption():
    out = s._render_table_block(_DATA_TABLE)
    assert out.startswith("[QA states]\n")


def test_render_table_no_caption_prefix_for_plain_table():
    assert not s._render_table_block(_TABLE).startswith("[")


def test_cell_text_variants():
    assert s._cell_text({"type": "raw_text", "text": "x"}) == "x"
    assert s._cell_text({"type": "raw_number", "text": "42"}) == "42"
    rich = {"type": "rich_text", "elements": [
        {"type": "rich_text_section", "elements": [{"type": "text", "text": "hi "},
                                                    {"type": "emoji", "name": "tada"}]}]}
    assert s._cell_text(rich) == "hi :tada:"


def test_rt_to_str_link_and_list():
    # TASK-003: label БЕЗ url прятал, куда ссылка ведёт — читающему агенту это
    # важнее краткости, поэтому теперь mrkdwn-форма `<url|label>`
    link = {"type": "link", "url": "https://x", "text": "X"}
    assert s._rt_to_str(link) == "<https://x|X>"
    assert s._rt_to_str({"type": "link", "url": "https://x"}) == "https://x"
    lst = {"type": "rich_text_list", "elements": [
        {"type": "rich_text_section", "elements": [{"type": "text", "text": "a"}]},
        {"type": "rich_text_section", "elements": [{"type": "text", "text": "b"}]}]}
    assert s._rt_to_str(lst) == "• a\n• b"


# --- TASK-001: `--json` accepted after the subcommand ----------------------

def test_json_flag_after_subcommand():
    p = s.build_parser()
    # was the bug: `read <url> --json` → "unrecognized arguments: --json" (rc=2)
    assert p.parse_args(["read", "URL", "--json"]).json is True
    assert p.parse_args(["--json", "read", "URL"]).json is True
    assert p.parse_args(["read", "URL"]).json is False


# --- TASK-002: multi-file upload argparse ----------------------------------

def test_upload_accepts_multiple_files():
    p = s.build_parser()
    a = p.parse_args(["upload", "#chan", "a.png", "b.png", "c.png"])
    assert a.file == ["a.png", "b.png", "c.png"]


def test_upload_single_file_still_list():
    p = s.build_parser()
    assert p.parse_args(["upload", "#chan", "only.png"]).file == ["only.png"]


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
