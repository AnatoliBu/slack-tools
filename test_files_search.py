#!/usr/bin/env python3
"""Offline tests (2026-10-20): legacy attachments in read/search, `search --files`,
`download` of a foreign URL with fallback to the same file posted in Slack.

Same standalone style as test_commands.py: `python test_files_search.py` (exit 0 = pass).
"""
import contextlib
import io
import sys
import tempfile
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import slack_cli as s  # noqa: E402
from test_commands import FakeClient, _args, _TEAM  # noqa: E402  (redirects caches too)

_JENKINS = {
    "ts": "1792490340.122399", "bot_id": "B04NM4G3TT5", "text": "",
    "attachments": [{
        "fallback": "KMM TestFlight - #644 Success\nPartnerId: 3330",
        "fields": [{"title": "", "value": "KMM TestFlight - #644 Success\nPartnerId: 3330\n"
                    ":android: <https://droid.lphub.net/jenkins_cloud/production/3330/11301401/"
                    "result/3330_11301401.apk>", "short": False}],
    }],
}

_F_NEW = {"id": "F0C896G1A9L", "name": "3373_11301400 (1).apk", "filetype": "apk",
          "size": 127029727, "timestamp": 1791676050, "user": "U04DSELCH5J",
          "url_private_download": "https://files.slack.com/files-pri/T/F0C896G1A9L/x.apk"}
_F_OLD = {**_F_NEW, "id": "F0C88ANPQ2Y", "name": "3373_11301300.apk", "timestamp": 1791566000}
_F_PNG = {**_F_NEW, "id": "F0PNG", "name": "3373_11301400.png", "filetype": "png",
          "timestamp": 1791999999}

_SHARES = {"file": {"shares": {"public": {"C09FZGT4AQP": [
    {"ts": "1791676050.240489", "thread_ts": "1791566991.045119",
     "channel_name": "3373_kfc_uzbekistan"}]}}}}
_REPLY = {"messages": [{"ts": "1791566991.045119", "text": "parent"},
                       {"ts": "1791676050.240489", "text": "для тестов"}]}


def _fake(matches):
    return FakeClient({
        "team_info": _TEAM,
        "users_info": {"user": {"name": "a.solovyov"}},
        "search_files": {"files": {"matches": matches, "paging": {"pages": 1}}},
        "files_info": _SHARES,
        "conversations_replies": _REPLY,
    })


def _out(func, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        func(*a, **kw)
    return buf.getvalue()


def test_attachments_render_field_value_once():
    out = s._render_attachments(_JENKINS["attachments"])
    assert len(out) == 1
    assert "3330_11301401.apk" in out[0]
    assert out[0].count("KMM TestFlight") == 1, out  # fallback не дублирует поле


def test_attachments_fallback_only_when_nothing_else():
    assert s._render_attachments([{"fallback": "only"}]) == ["only"]
    assert s._render_attachments([{"title": "T", "title_link": "https://x"}]) == ["<https://x|T>"]
    assert s._render_attachments(None) == []


def test_search_files_filters_type_sorts_newest_and_reads_share_context():
    fake = _fake([_F_OLD, _F_PNG, _F_NEW])
    out = _out(s.cmd_search, _args("search", "--files", "--type", "apk", "--limit", "5"), client=fake)
    lines = out.splitlines()
    assert "F0C896G1A9L" in lines[0] and "#3373_kfc_uzbekistan" in lines[0], out
    assert "для тестов" in out
    assert "F0PNG" not in out
    assert out.index("F0C896G1A9L") < out.index("F0C88ANPQ2Y")
    assert fake.last("search_files")["query"] == "apk"  # без query ищет по типу


def test_search_files_no_context_skips_files_info():
    fake = _fake([_F_NEW])
    _out(s.cmd_search, _args("search", "3373", "--files", "--no-context"), client=fake)
    assert fake.count("files_info") == 0


def test_search_messages_snippet_uses_attachments_when_text_empty():
    fake = FakeClient({"team_info": _TEAM, "search_messages": {"messages": {"matches": [
        {**_JENKINS, "channel": {"id": "C7385T8AW", "name": "app_builds"},
         "username": "jenkins", "permalink": "https://x"}]}}})
    out = _out(s.cmd_search, _args("search", "KMM"), client=fake)
    assert "KMM TestFlight - #644" in out, out


def test_file_stem_and_slack_host():
    assert s._file_stem("3373_11301400 (1).apk") == "3373_11301400"
    assert s._file_stem("https://h/a/3330_11301401.apk?x=1") == "3330_11301401"
    assert s._is_slack_host("https://files.slack.com/files-pri/x")
    assert not s._is_slack_host("https://droid.lphub.net/jenkins_cloud/x.apk")
    assert not s._is_slack_host("https://slack.com.evil.net/x")


def _patched_download(record, fail_hosts):
    def fake(url, out, headers):
        record.append((url, dict(headers)))
        if any(h in url for h in fail_hosts):
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
        Path(out).write_bytes(b"PK")
    return fake


def test_download_foreign_url_sends_no_slack_cookie_and_falls_back_to_slack():
    calls, orig = [], s._download_url
    s._download_url = _patched_download(calls, ["droid.lphub.net"])
    try:
        d = tempfile.mkdtemp()
        fake = _fake([_F_OLD, _F_NEW])
        out = _out(s.cmd_download, _args("download", "https://droid.lphub.net/jenkins_cloud/"
                                         "production/3373/11301400/result/3373_11301400.apk",
                                         "-o", d), client=fake)
    finally:
        s._download_url = orig
    assert calls[0][1] == {}, "Slack-cookie не должен уходить на чужой хост"
    assert calls[1][0] == _F_NEW["url_private_download"]
    assert "Cookie" in calls[1][1] or "Authorization" in calls[1][1]
    assert "slack_file=F0C896G1A9L" in out, out
    assert (Path(d) / "3373_11301400.apk").exists()


def test_download_foreign_url_without_slack_copy_fails_loud():
    calls, orig = [], s._download_url
    s._download_url = _patched_download(calls, ["droid.lphub.net"])
    try:
        fake = _fake([_F_OLD])
        try:
            s.cmd_download(_args("download", "https://droid.lphub.net/x/3330_11301401.apk",
                                 "-o", tempfile.mkdtemp()), client=fake)
            raise AssertionError("ожидалась ошибка")
        except RuntimeError as e:
            assert "3330_11301401" in str(e)
    finally:
        s._download_url = orig


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
