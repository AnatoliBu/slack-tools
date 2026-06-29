#!/usr/bin/env python3
"""Tests for slack_cli.win_path / win_path_at — MSYS/WSL/tmp → Windows path
normalization (BUG-001).

Guards the recurring '/c/.. or /tmp/.. path breaks Windows-Python' class of bugs
in download -o, upload <file>, files --download and @file args. No pytest dep —
runs as `python test_win_path.py` (exit 0 = pass, 1 = fail).

Mirrors ~/.claude/scripts/tests/test_lp_pathutil.py, plus the /tmp mount that
slack_cli additionally handles.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from slack_cli import win_path, win_path_at, read_text_arg  # noqa: E402

TMP = tempfile.gettempdir()


def test_msys_drive_prefix_rewritten():
    assert win_path("/c/Users/dev/foo.json") == "C:/Users/dev/foo.json"
    assert win_path("/d/data/x") == "D:/data/x"


def test_wsl_mnt_prefix_rewritten():
    assert win_path("/mnt/c/Users/x") == "C:/Users/x"
    assert win_path("/mnt/d/y") == "D:/y"


def test_bare_drive_root():
    assert win_path("/c") == "C:/"
    assert win_path("/d/") == "D:/"


def test_tmp_mount_mapped_to_tempdir():
    # Git-for-Windows maps /tmp to %TEMP%; verified == tempfile.gettempdir()
    assert win_path("/tmp") == TMP
    assert win_path("/tmp/font-issue/x.png") == os.path.join(TMP, "font-issue", "x.png")


def test_native_paths_unchanged():
    assert win_path("C:/already/native") == "C:/already/native"
    assert win_path("C:\\win\\style") == "C:\\win\\style"
    assert win_path("relative/path") == "relative/path"
    assert win_path("./rel") == "./rel"


def test_passthrough_sentinels():
    assert win_path("-") == "-"
    assert win_path("") == ""
    assert win_path(None) is None


def test_idempotent():
    once = win_path("/c/Users/x")
    assert win_path(once) == once
    tmp_once = win_path("/tmp/x")
    assert win_path(tmp_once) == tmp_once


def test_other_single_segment_mount_left_alone():
    # /usr is not a drive and not /tmp → left as-is (we can't resolve it safely)
    assert win_path("/usr/bin/bash") == "/usr/bin/bash"


def test_win_path_at_handles_at_prefix():
    assert win_path_at("@/c/Users/x") == "@C:/Users/x"
    assert win_path_at("@/tmp/x") == "@" + os.path.join(TMP, "x")
    assert win_path_at("/c/Users/x") == "C:/Users/x"
    assert win_path_at("@-") == "@-"
    assert win_path_at("@") == "@"


def test_read_text_arg_reads_msys_at_file():
    # write a file under %TEMP% and reference it via the MSYS /tmp form
    fname = "slack_cli_bug001_probe.txt"
    target = Path(TMP) / fname
    target.write_text("payload-001", encoding="utf-8")
    try:
        # @/tmp/<f> must resolve to %TEMP%/<f> and be read (not returned literally)
        assert read_text_arg(f"@/tmp/{fname}") == "payload-001"
        # inline text that is not an @file is returned verbatim
        assert read_text_arg("just a message") == "just a message"
    finally:
        target.unlink(missing_ok=True)


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
