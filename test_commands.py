#!/usr/bin/env python3
"""Offline tests for the message/channel-ops commands added on top of the base CLI:
forward, members, pin/unpin, join/leave, --schedule + scheduled, stats, wait, listen.

No network / tokens: every command takes an injected `_client` (the same seam the
CLI uses for token-retry) — here a FakeClient that records calls and returns canned
responses. Same standalone style as test_win_path.py / test_read_render.py:
`python test_commands.py` (exit 0 = pass, 1 = fail).

All on-disk caches/state are redirected to a temp dir so tests never touch the real
~/.cache/slack-cli and stay deterministic.
"""
import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import slack_cli as s  # noqa: E402

# --- redirect all disk caches/state to a throwaway dir ----------------------
_TMP = Path(tempfile.mkdtemp(prefix="slack_cli_test_"))
s.CACHE_DIR = _TMP
s.AUDIT_LOG = _TMP / "audit.jsonl"
s.LAST_STATE = _TMP / "last.json"
s.CHANNEL_INFO_CACHE = _TMP / "channel-info.json"
s.SELF_DM_CACHE = _TMP / "self-dm.json"
s.USER_CACHE = _TMP / "users.json"

WS = "testws.slack.com"
CHAN = "C0CHAN0001"
DEST = "C0DEST0001"


class FakeClient:
    """Records (method, kwargs) and returns canned responses.

    responses[name] may be a dict (returned as-is) or a callable(**kwargs)->dict
    (for pagination / ts-dependent replies). Missing → {"ok": True}.
    """

    def __init__(self, responses=None):
        self.calls = []
        self._responses = responses or {}

    def __getattr__(self, name):
        def method(**kwargs):
            self.calls.append((name, kwargs))
            r = self._responses.get(name)
            if callable(r):
                r = r(**kwargs)
            return r if r is not None else {"ok": True}
        return method

    def last(self, name):
        for m, kw in reversed(self.calls):
            if m == name:
                return kw
        return None

    def count(self, name):
        return sum(1 for m, _ in self.calls if m == name)


def _args(*argv):
    return s.build_parser().parse_args(list(argv))


def _capture(func, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        func(*a, **kw)
    return buf.getvalue()


# common canned responses so dest_of / team_domain work under a fake client
_CHAN_INFO = {"channel": {"id": DEST, "name": "dest", "is_channel": True,
                          "is_private": False, "is_im": False, "is_mpim": False}}
_TEAM = {"team": {"domain": "testws"}}


# --- argparse registration --------------------------------------------------

# --- contacts / invite (owner request 2026-10-01) ----------------------------
# "a quick list of all contacts — with whom there is and is not a conversation" and
# "add someone (e.g. Dr. House) to a channel".

_USERS = {"members": [
    {"id": "U0000000A1", "name": "alice", "profile": {"real_name": "Alice A"}},
    {"id": "U0000000B2", "name": "bob", "profile": {"real_name": "Bob B"}},
    {"id": "U0B2YLD53QU", "name": "dr_house", "is_bot": True, "profile": {"real_name": "Dr. House"}},
    {"id": "U0000000C3", "name": "gone", "deleted": True, "profile": {}},
], "response_metadata": {}}
_IMS = {"channels": [{"id": "D0000000A1", "is_im": True, "user": "U0000000A1"}],
        "response_metadata": {}}


def _fresh_user_cache():
    if s.USER_CACHE.exists():
        s.USER_CACHE.unlink()


def test_contacts_marks_who_has_a_dm():
    _fresh_user_cache()
    fake = FakeClient({"users_list": _USERS, "conversations_list": _IMS})
    out = _capture(s.cmd_contacts, _args("contacts", "--refresh", "--json"), client=fake)
    rows = {r["id"]: r for r in json.loads(out)}
    assert rows["U0000000A1"]["dm"] == "D0000000A1"
    assert rows["U0000000B2"]["dm"] is None
    assert "U0B2YLD53QU" not in rows, "bots hidden by default"
    assert "U0000000C3" not in rows, "deleted users hidden"
    assert fake.last("conversations_list")["types"] == "im"


def test_contacts_without_dm_and_bots_flags():
    _fresh_user_cache()
    fake = FakeClient({"users_list": _USERS, "conversations_list": _IMS})
    out = _capture(s.cmd_contacts, _args("contacts", "--refresh", "--without-dm", "--bots", "--json"),
                   client=fake)
    ids = {r["id"] for r in json.loads(out)}
    assert ids == {"U0000000B2", "U0B2YLD53QU"}


def test_invite_resolves_names_and_bot_ids():
    _fresh_user_cache()
    fake = FakeClient({"users_list": _USERS, "conversations_invite": {"ok": True}})
    _capture(s.cmd_invite, _args("invite", CHAN, "@bob", "U0B2YLD53QU"), client=fake)
    call = fake.last("conversations_invite")
    assert call["channel"] == CHAN
    assert call["users"] == "U0000000B2,U0B2YLD53QU"


# --- thread subscriptions: subscribe / subscriptions / unsubscribe / watch -----
# Owner request 2026-10-01: "subscribe to a thread and get notifications from it".
# Agent pattern: `slack watch` in the background exits 0 on the first new replies —
# the exit is the notification; the seen-mark advances, so the next run continues.

SUB_URL = f"https://{WS}/archives/{CHAN}/p1700000100000000"
SUB_URL2 = f"https://{WS}/archives/{CHAN}/p1700000500000000"
ME = "U0000000ME"


def _fresh_subs():
    p = s._subs_path()
    if p.exists():
        p.unlink()


def _thread(*msgs):
    return lambda **kw: {"messages": [m for m in msgs if m["ts"] > kw.get("oldest", "0")
                                      or "oldest" not in kw]}


def test_subscribe_stores_thread_with_latest_reply_as_seen():
    _fresh_subs()
    fake = FakeClient({"conversations_replies": _thread(
        {"ts": "1700000100.000000", "user": "U1"}, {"ts": "1700000150.000000", "user": "U2"})})
    _capture(s.cmd_subscribe, _args("subscribe", SUB_URL, "--label", "PLAT-4731"), client=fake)
    subs = s._load_subs()
    assert len(subs) == 1
    sub = next(iter(subs.values()))
    assert sub["channel"] == CHAN and sub["thread_ts"] == "1700000100.000000"
    assert sub["last_ts"] == "1700000150.000000" and sub["label"] == "PLAT-4731"


def test_subscribe_rejects_a_plain_channel():
    _fresh_subs()
    try:
        _capture(s.cmd_subscribe, _args("subscribe", CHAN), client=FakeClient())
        assert False, "a channel is not a thread"
    except SystemExit as e:
        assert e.code == 2


def test_watch_reports_new_replies_and_advances_seen_mark():
    _fresh_subs()
    s._save_subs({f"{CHAN}:1700000100.000000": {
        "channel": CHAN, "thread_ts": "1700000100.000000", "url": SUB_URL,
        "label": "A", "last_ts": "1700000150.000000"}})
    fake = FakeClient({"auth_test": {"user_id": ME}, "conversations_replies": _thread(
        {"ts": "1700000150.000000", "user": "U2", "text": "old"},
        {"ts": "1700000200.000000", "user": "U3", "text": "fresh reply"})})
    out = _capture(s.cmd_watch, _args("watch", "--timeout", "0"), client=fake)
    assert "fresh reply" in out and "old" not in out and "A" in out
    assert next(iter(s._load_subs().values()))["last_ts"] == "1700000200.000000"


def test_watch_ignores_own_messages_by_default():
    _fresh_subs()
    s._save_subs({f"{CHAN}:1700000100.000000": {
        "channel": CHAN, "thread_ts": "1700000100.000000", "url": SUB_URL,
        "label": "A", "last_ts": "1700000150.000000"}})
    fake = FakeClient({"auth_test": {"user_id": ME}, "conversations_replies": _thread(
        {"ts": "1700000200.000000", "user": ME, "text": "my own call"})})
    try:
        _capture(s.cmd_watch, _args("watch", "--timeout", "0"), client=fake)
        assert False, "own message must not count as a notification"
    except SystemExit as e:
        assert e.code == 3
    # own message still advances the seen mark (it was seen)
    assert next(iter(s._load_subs().values()))["last_ts"] == "1700000200.000000"


def test_watch_without_subscriptions_is_a_usage_error():
    _fresh_subs()
    try:
        _capture(s.cmd_watch, _args("watch", "--timeout", "0"), client=FakeClient())
        assert False
    except SystemExit as e:
        assert e.code == 2


def test_unsubscribe_and_list():
    _fresh_subs()
    s._save_subs({f"{CHAN}:1700000100.000000": {"channel": CHAN, "thread_ts": "1700000100.000000",
                  "url": SUB_URL, "label": "A", "last_ts": "1"},
                  f"{CHAN}:1700000500.000000": {"channel": CHAN, "thread_ts": "1700000500.000000",
                  "url": SUB_URL2, "label": "B", "last_ts": "1"}})
    out = _capture(s.cmd_subscriptions, _args("subscriptions"), client=FakeClient())
    assert "A" in out and "B" in out
    _capture(s.cmd_unsubscribe, _args("unsubscribe", SUB_URL), client=FakeClient())
    assert [v["label"] for v in s._load_subs().values()] == ["B"]


def test_all_new_commands_registered():
    p = s.build_parser()
    assert p.parse_args(["forward", "URL", "#d"]).func is s.cmd_forward
    assert p.parse_args(["members", CHAN]).func is s.cmd_members
    assert p.parse_args(["pin", "URL"]).func is s.cmd_pin
    assert p.parse_args(["unpin", "URL"]).func is s.cmd_unpin
    assert p.parse_args(["join", CHAN]).func is s.cmd_join
    assert p.parse_args(["leave", CHAN]).func is s.cmd_leave
    assert p.parse_args(["scheduled"]).func is s.cmd_scheduled
    assert p.parse_args(["stats", CHAN]).func is s.cmd_stats
    assert p.parse_args(["contacts"]).func is s.cmd_contacts
    assert p.parse_args(["subscribe", "URL"]).func is s.cmd_subscribe
    assert p.parse_args(["subscriptions"]).func is s.cmd_subscriptions
    assert p.parse_args(["unsubscribe", "URL"]).func is s.cmd_unsubscribe
    assert p.parse_args(["watch"]).func is s.cmd_watch
    assert p.parse_args(["invite", CHAN, "@bob"]).func is s.cmd_invite
    assert p.parse_args(["wait", CHAN]).func is s.cmd_wait
    assert p.parse_args(["listen", CHAN]).func is s.cmd_listen


def test_schedule_flag_on_send_and_reply():
    p = s.build_parser()
    assert p.parse_args(["send", CHAN, "hi", "--schedule", "2026-07-20T09:00"]).schedule \
        == "2026-07-20T09:00"
    url = f"https://{WS}/archives/{CHAN}/p1700000000000000"
    assert p.parse_args(["reply", url, "hi", "--schedule", "1700000000"]).schedule \
        == "1700000000"
    assert p.parse_args(["send", CHAN, "hi"]).schedule is None


# --- iso_to_epoch -----------------------------------------------------------

def test_iso_to_epoch_bare_unix():
    assert s.iso_to_epoch("1700000000") == 1700000000


def test_iso_to_epoch_utc_offset_is_absolute():
    # 2026-07-20T09:00:00+00:00 is a fixed instant regardless of local tz
    assert s.iso_to_epoch("2026-07-20T09:00:00+00:00") == 1784538000
    assert s.iso_to_epoch("2026-07-20T09:00:00Z") == 1784538000


def test_iso_to_epoch_naive_roundtrips_local():
    import time as _t
    ep = s.iso_to_epoch("2026-07-20T09:00:00")
    assert _t.strftime("%Y-%m-%d %H:%M", _t.localtime(ep)) == "2026-07-20 09:00"


# --- forward ----------------------------------------------------------------

def test_forward_uses_permalink_and_reposts_with_unfurl():
    src_url = f"https://{WS}/archives/C0SRC00001/p1700000000000123"
    fake = FakeClient({
        "chat_getPermalink": {"permalink": f"https://{WS}/archives/C0SRC00001/p1700000000000123"},
        "conversations_info": _CHAN_INFO,
        "team_info": _TEAM,
        "chat_postMessage": {"ts": "1700000009.000100"},
    })
    args = _args("forward", src_url, DEST, "--comment", "fyi", "--no-notify")
    out = _capture(s.cmd_forward, args, client=fake)
    # getPermalink resolved channel+ts from the source URL
    gp = fake.last("chat_getPermalink")
    assert gp == {"channel": "C0SRC00001", "message_ts": "1700000000.000123"}
    # repost carries the comment + permalink and turns unfurl ON (for the preview)
    pm = fake.last("chat_postMessage")
    assert pm["channel"] == DEST
    assert pm["text"] == f"fyi\nhttps://{WS}/archives/C0SRC00001/p1700000000000123"
    assert pm["unfurl_links"] is True
    assert "url=" in out


def test_forward_target_plus_ts_requires_ts():
    fake = FakeClient()
    args = _args("forward", DEST, DEST)  # src is a bare target, no --ts
    try:
        s.cmd_forward(args, client=fake)
        assert False, "expected ValueError when src is a target without --ts"
    except ValueError as e:
        assert "--ts" in str(e)


# --- members ----------------------------------------------------------------

def test_members_paginates_and_lists_ids():
    pages = {None: {"members": ["U111", "U222"],
                    "response_metadata": {"next_cursor": "c2"}},
             "c2": {"members": ["U333"], "response_metadata": {"next_cursor": ""}}}
    fake = FakeClient({"conversations_members": lambda **kw: pages[kw.get("cursor")]})
    args = _args("members", CHAN)
    out = _capture(s.cmd_members, args, client=fake)
    assert fake.last("conversations_members")["channel"] == CHAN
    assert fake.count("conversations_members") == 2  # both pages fetched
    for uid in ("U111", "U222", "U333"):
        assert uid in out


def test_members_limit_truncates():
    fake = FakeClient({"conversations_members":
                       {"members": ["U1", "U2", "U3", "U4"], "response_metadata": {}}})
    args = _args("members", CHAN, "--limit", "2")
    out = _capture(s.cmd_members, args, client=fake)
    lines = [l for l in out.splitlines() if l.strip()]
    assert len(lines) == 2


# --- pin / unpin ------------------------------------------------------------

def test_pin_calls_pins_add_with_channel_and_ts():
    fake = FakeClient()
    url = f"https://{WS}/archives/{CHAN}/p1700000000000123"
    s.cmd_pin(_args("pin", url), client=fake)
    assert fake.last("pins_add") == {"channel": CHAN, "timestamp": "1700000000.000123"}


def test_unpin_calls_pins_remove():
    fake = FakeClient()
    url = f"https://{WS}/archives/{CHAN}/p1700000000000123"
    s.cmd_unpin(_args("unpin", url), client=fake)
    assert fake.last("pins_remove") == {"channel": CHAN, "timestamp": "1700000000.000123"}


# --- join / leave -----------------------------------------------------------

def test_join_calls_conversations_join():
    fake = FakeClient({"conversations_join": {"channel": {"id": CHAN, "name": "eng"}}})
    s.cmd_join(_args("join", CHAN), client=fake)
    assert fake.last("conversations_join") == {"channel": CHAN}


def test_leave_calls_conversations_leave():
    fake = FakeClient()
    s.cmd_leave(_args("leave", CHAN), client=fake)
    assert fake.last("conversations_leave") == {"channel": CHAN}


def test_interactive_elements_parse_message_actions_and_values():
    blocks = [{"type": "actions", "block_id": "controls", "elements": [
        {"type": "button", "action_id": "go", "text": {"type": "plain_text", "text": "Go"}, "value": "v1"},
        {"type": "static_select", "action_id": "mode", "options": [
            {"text": {"type": "plain_text", "text": "Deep"}, "value": "deeper"}]},
        {"type": "overflow", "action_id": "more", "options": []},
        {"type": "datepicker", "action_id": "date"},
        {"type": "timepicker", "action_id": "time"},
        {"type": "checkboxes", "action_id": "checks", "options": []},
        {"type": "radio_buttons", "action_id": "radio", "options": []},
    ]}, {"type": "section", "block_id": "accessory", "accessory": {
        "type": "users_select", "action_id": "user"}}]
    rows = s._interactive_elements(blocks)
    assert [r["action_id"] for r in rows] == ["go", "mode", "more", "date", "time", "checks", "radio", "user"]
    assert rows[0]["block_id"] == "controls"
    assert rows[0]["text"] == "Go" and rows[0]["value"] == "v1"
    assert rows[1]["options"][0]["value"] == "deeper"


def test_view_schema_and_state_are_keyed_by_block_then_action():
    view = {"id": "V1", "callback_id": "generic_form", "type": "modal",
            "title": {"text": "Form"}, "submit": {"text": "Submit"}, "blocks": [
        {"type": "input", "block_id": "text", "label": {"text": "Text"}, "element":
         {"type": "plain_text_input", "action_id": "value"}},
        {"type": "input", "block_id": "mode", "optional": True, "label": {"text": "Mode"}, "element":
         {"type": "static_select", "action_id": "value", "options": [
             {"text": {"text": "Deep"}, "value": "deeper"}]}},
        {"type": "input", "block_id": "checks", "optional": True, "element":
         {"type": "checkboxes", "action_id": "value", "options": [
             {"text": {"text": "A"}, "value": "a"}, {"text": {"text": "B"}, "value": "b"}]}},
        {"type": "input", "block_id": "when", "optional": True, "element":
         {"type": "datepicker", "action_id": "value"}},
    ]}
    schema = s._view_schema(view)
    assert schema["id"] == "V1" and schema["callback_id"] == "generic_form"
    assert schema["inputs"][0]["required"] is True
    assert schema["inputs"][1]["options"][0]["value"] == "deeper"
    state = s._view_state(view, {"text": "hello", "mode": "deeper", "checks": "a,b", "when": "2026-10-29"})
    assert state["text"]["value"]["value"] == "hello"
    assert state["mode"]["value"]["selected_option"]["value"] == "deeper"
    assert [o["value"] for o in state["checks"]["value"]["selected_options"]] == ["a", "b"]
    assert state["when"]["value"]["selected_date"] == "2026-10-29"


def test_view_state_requires_mandatory_input_and_rejects_unknown_block():
    view = {"blocks": [{"type": "input", "block_id": "required", "element":
                        {"type": "plain_text_input", "action_id": "value"}}]}
    try:
        s._view_state(view, {})
        assert False, "missing required field accepted"
    except s.InteractionError as exc:
        assert exc.code == "missing_required_form"
    try:
        s._view_state(view, {"other": "x"})
        assert False, "unknown block accepted"
    except s.InteractionError as exc:
        assert exc.code == "unknown_form_block"


def test_view_state_accepts_dynamic_external_select_choice():
    view = {"blocks": [{"type": "input", "block_id": "person", "element": {
        "type": "external_select", "action_id": "value", "options": []}}]}
    state = s._view_state(view, {"person": "U_DYNAMIC"})
    assert state["person"]["value"]["selected_option"] == {
        "text": {"type": "plain_text", "text": "U_DYNAMIC"}, "value": "U_DYNAMIC"}


def test_view_state_accepts_dynamic_multi_external_select_choices():
    view = {"blocks": [{"type": "input", "block_id": "people", "element": {
        "type": "multi_external_select", "action_id": "value", "options": []}}]}
    state = s._view_state(view, {"people": "U_DYNAMIC_1,U_DYNAMIC_2"})
    assert state["people"]["value"]["selected_options"] == [
        {"text": {"type": "plain_text", "text": "U_DYNAMIC_1"}, "value": "U_DYNAMIC_1"},
        {"text": {"type": "plain_text", "text": "U_DYNAMIC_2"}, "value": "U_DYNAMIC_2"}]


def test_view_state_still_rejects_unknown_static_select_choice():
    view = {"blocks": [{"type": "input", "block_id": "mode", "element": {
        "type": "static_select", "action_id": "value", "options": [
            {"text": {"type": "plain_text", "text": "Known"}, "value": "known"}]}}]}
    try:
        s._view_state(view, {"mode": "unknown"})
        assert False, "unknown static option accepted"
    except s.InteractionError as exc:
        assert exc.code == "invalid_form_value"


def _assert_click_dry_run_dynamic_external_select(etype, raw, expected_key, expected_values):
    url = f"https://{WS}/archives/{DEST}/p1700000000000000"
    old_load_env = s._load_env
    s._load_env = lambda: {"SLACK_XOXC": "xoxc-test"}
    try:
        msg = {"ts": "1700000000.000000", "blocks": [{"type": "actions", "block_id": "b",
            "elements": [{"type": etype, "action_id": "choose", "options": []}]}]}
        fake = FakeClient({"conversations_history": {"messages": [msg]}})
        args = _args("click", url, "--action", "choose", "--value", raw, "--dry-run", "--json")
        payload = json.loads(_capture(s.cmd_click, args, client=fake))["payload"]
        action = json.loads(payload["actions"])[0]
        selected = action[expected_key]
        selected = [selected] if expected_key == "selected_option" else selected
        assert [option["value"] for option in selected] == expected_values
        assert all(option["text"]["type"] == "plain_text" for option in selected)
        assert fake.count("api_call") == 0
    finally:
        s._load_env = old_load_env


def test_click_dry_run_accepts_dynamic_external_select_choice():
    _assert_click_dry_run_dynamic_external_select(
        "external_select", "U_DYNAMIC", "selected_option", ["U_DYNAMIC"])


def test_click_dry_run_accepts_dynamic_multi_external_select_choices():
    _assert_click_dry_run_dynamic_external_select(
        "multi_external_select", "U_DYNAMIC_1,U_DYNAMIC_2", "selected_options",
        ["U_DYNAMIC_1", "U_DYNAMIC_2"])


def test_click_dry_run_still_rejects_unknown_static_select_choice():
    url = f"https://{WS}/archives/{DEST}/p1700000000000000"
    msg = {"ts": "1700000000.000000", "blocks": [{"type": "actions", "block_id": "b",
        "elements": [{"type": "static_select", "action_id": "choose", "options": [
            {"text": {"type": "plain_text", "text": "Known"}, "value": "known"}]}]}]}
    fake = FakeClient({"conversations_history": {"messages": [msg]}})
    args = _args("click", url, "--action", "choose", "--value", "unknown", "--dry-run", "--json")
    try:
        s.cmd_click(args, client=fake)
        assert False, "unknown static option accepted"
    except s.InteractionError as exc:
        assert exc.code == "invalid_action_value"


def test_click_dry_run_is_read_only_and_describes_action():
    url = f"https://{WS}/archives/{DEST}/p1700000000000000"
    msg = {"ts": "1700000000.000000", "blocks": [{"type": "actions", "block_id": "b",
            "elements": [{"type": "button", "action_id": "go", "text": {"type": "plain_text", "text": "Go"}}]}]}
    fake = FakeClient({"conversations_history": {"messages": [msg]}})
    args = _args("click", url, "--action", "go", "--dry-run", "--json")
    out = _capture(s.cmd_click, args, client=fake)
    payload = json.loads(out)
    assert payload["method"] == "blocks.actions"
    assert json.loads(payload["payload"]["actions"])[0]["action_id"] == "go"
    assert payload["payload"]["client_token"] == "<redacted>"
    assert fake.count("api_call") == 0


class FakeListener:
    """Подменяет _ViewListener: отдаёт записанное событие view_opened, подставив наш client_token."""

    def __init__(self, event=None):
        self.event, self.closed = event, False

    def wait_view(self, client_token, timeout):
        return dict(self.event, client_token=client_token) if self.event else None

    def close(self):
        self.closed = True


def _real_view_opened():
    """Захват 2026-10-29: RTM `view_opened` на клик house_continue (модалку НЕ сабмитили)."""
    path = Path(__file__).parent / "fixtures" / "view_opened_house_continue.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _click_with_listener(listener, *argv, fake=None):
    url = f"https://{WS}/archives/{DEST}/p1700000000000000"
    msg = {"ts": "1700000000.000000", "app_id": "A000000001", "bot_id": "B000000001", "blocks": [{
        "type": "actions", "block_id": "c", "elements": [{
            "type": "button", "action_id": "go", "text": {"type": "plain_text", "text": "Go"}}]}]}
    fake = fake or FakeClient({"conversations_history": {"messages": [msg]},
                               "api_call": lambda api_method, **kw: {"ok": True}})
    opener, load_env = s._open_view_listener, s._load_env
    s._open_view_listener = lambda client, timeout: listener
    s._load_env = lambda: {"SLACK_XOXC": "xoxc-secret"}
    try:
        return fake, _capture(s.cmd_click, _args("click", url, "--action", "go", *argv, "--json"), client=fake)
    finally:
        s._open_view_listener, s._load_env = opener, load_env


def test_click_returns_modal_schema_from_real_view_opened_event():
    listener = FakeListener(_real_view_opened())
    fake, out = _click_with_listener(listener)
    result = json.loads(out)
    assert result["submitted"] is False
    view = result["view"]
    assert view["id"] == "V0CD3JXPPC5" and view["callback_id"] == "house_continue_form"
    assert view["title"] == "Продолжить разбор" and view["submit"] == "Запустить"
    assert [(i["block_id"], i["type"], i["required"]) for i in view["inputs"]] == [
        ("text", "plain_text_input", True), ("files", "file_input", False)]
    assert [kw["api_method"] for m, kw in fake.calls if m == "api_call"] == ["blocks.actions"]
    assert listener.closed


def test_click_form_plans_views_submit_but_sends_only_with_submit_flag():
    ev = _real_view_opened()
    fake, out = _click_with_listener(FakeListener(ev), "--form", "text=hello")
    result = json.loads(out)
    assert result["submitted"] is False and "response" not in result
    plan = result["planned_submit"]["payload"]
    assert plan["view_id"] == "V0CD3JXPPC5" and plan["hash"] == ev["view"]["hash"]
    assert [kw["api_method"] for m, kw in fake.calls if m == "api_call"] == ["blocks.actions"]

    fake, out = _click_with_listener(FakeListener(ev), "--form", "text=hello", "--submit")
    calls = [kw for m, kw in fake.calls if m == "api_call"]
    assert [c["api_method"] for c in calls] == ["blocks.actions", "views.submit"]
    assert calls[1]["params"]["view_id"] == "V0CD3JXPPC5"
    assert calls[1]["params"]["hash"] == ev["view"]["hash"]
    state = json.loads(calls[1]["params"]["state"])
    assert state["values"]["text"]["v"] == {"type": "plain_text_input", "value": "hello"}
    assert json.loads(out)["submitted"] is True


def test_click_waits_for_view_only_for_own_client_token():
    class FakeSocket:
        def __init__(self, events):
            self.events = [json.dumps(e) for e in events]
        def settimeout(self, _): pass
        def recv(self):
            if not self.events:
                raise TimeoutError
            return self.events.pop(0)
    foreign = dict(_real_view_opened(), client_token="web-other")
    mine = dict(_real_view_opened(), client_token="web-mine")
    listener = s._ViewListener.__new__(s._ViewListener)
    listener._ws = FakeSocket([{"type": "message"}, foreign, mine])
    listener._timeout_exc = TimeoutError
    listener._closed_exc = ConnectionError
    assert listener.wait_view("web-mine", 5)["client_token"] == "web-mine"


def test_click_form_without_modal_fails_with_view_timeout():
    try:
        _click_with_listener(FakeListener(None), "--form", "text=hello", "--timeout", "0")
        assert False, "form submitted without modal"
    except s.InteractionError as exc:
        assert exc.code == "view_timeout"


def test_click_uses_bot_id_as_service_id_and_generated_client_token():
    """Замер 2026-10-29 на сообщении бота: app_id → invalid_service_id, bot_id → ok;
    client_token — сгенерированный id клиента, а не xoxc-токен."""
    fake, _ = _click_with_listener(FakeListener(None), "--timeout", "0")
    params = [kw for m, kw in fake.calls if m == "api_call"][0]["params"]
    assert params["service_id"] == "B000000001", params
    assert params["client_token"].startswith("web-") and "xoxc" not in params["client_token"], params


# --- send --schedule --------------------------------------------------------

def test_send_schedule_uses_scheduleMessage_with_post_at():
    fake = FakeClient({
        "conversations_info": {"channel": {"id": CHAN, "name": "eng", "is_channel": True}},
        "team_info": _TEAM,
        "chat_scheduleMessage": {"scheduled_message_id": "Q12345"},
    })
    args = _args("send", CHAN, "later!", "--schedule", "1700000000")
    out = _capture(s.cmd_send, args, client=fake)
    sm = fake.last("chat_scheduleMessage")
    assert sm["channel"] == CHAN
    assert sm["post_at"] == 1700000000
    assert sm["text"] == "later!"
    # scheduling must NOT post immediately
    assert fake.count("chat_postMessage") == 0
    assert "scheduled_message_id=Q12345" in out


# --- scheduled (list / cancel) ---------------------------------------------

def test_scheduled_list_calls_list_and_prints_rows():
    fake = FakeClient({"chat_scheduledMessages_list": {"scheduled_messages": [
        {"id": "Q1", "post_at": 1700000000, "channel_id": CHAN, "text": "hi"}]}})
    args = _args("scheduled", CHAN)
    out = _capture(s.cmd_scheduled, args, client=fake)
    assert fake.last("chat_scheduledMessages_list")["channel"] == CHAN
    assert "Q1" in out


def test_scheduled_cancel_calls_delete():
    fake = FakeClient()
    args = _args("scheduled", CHAN, "--cancel", "Q1")
    s.cmd_scheduled(args, client=fake)
    dm = fake.last("chat_deleteScheduledMessage")
    assert dm == {"channel": CHAN, "scheduled_message_id": "Q1"}


def test_scheduled_cancel_without_target_errors():
    fake = FakeClient()
    args = _args("scheduled", "--cancel", "Q1")  # no target
    try:
        s.cmd_scheduled(args, client=fake)
        assert False, "expected ValueError without a target channel"
    except ValueError as e:
        assert "target" in str(e)


# --- stats ------------------------------------------------------------------

def test_stats_aggregates_senders_days_and_shares():
    msgs = [
        {"ts": "1700000000.000001", "user": "U1", "files": [{}]},          # 2023-11-14 (UTC)
        {"ts": "1700000060.000002", "user": "U1", "reactions": [{}]},
        {"ts": "1700000120.000003", "user": "U2"},
    ]
    fake = FakeClient({"conversations_history": {"messages": msgs, "response_metadata": {}}})
    args = _args("stats", CHAN, "--limit", "100", "--json")
    out = _capture(s.cmd_stats, args, client=fake)
    data = json.loads(out)
    assert data["total"] == 3
    top = {d["user_id"]: d["count"] for d in data["top_senders"]}
    assert top["U1"] == 2 and top["U2"] == 1
    assert data["with_files"] == 1 and data["with_reactions"] == 1
    assert data["files_pct"] == round(100 / 3, 1)


# --- wait -------------------------------------------------------------------

def test_wait_returns_on_new_message():
    new = {"ts": "1700000200.000000", "user": "U9", "text": "ping"}
    fake = FakeClient({"conversations_history": {"messages": [new]}})
    args = _args("wait", CHAN, "--after-ts", "1700000100.000000", "--timeout", "5")
    out = _capture(s.cmd_wait, args, client=fake)
    assert "ping" in out
    # baseline supplied via --after-ts → no extra history call for baseline
    assert fake.last("conversations_history")["oldest"] == "1700000100.000000"


def test_wait_times_out_with_exit_3():
    # only stale messages (<= baseline) → no new → timeout
    fake = FakeClient({"conversations_history":
                       {"messages": [{"ts": "1700000100.000000", "user": "U0"}]}})
    args = _args("wait", CHAN, "--after-ts", "1700000100.000000", "--timeout", "0")
    try:
        _capture(s.cmd_wait, args, client=fake)
        assert False, "expected SystemExit(3) on timeout"
    except SystemExit as e:
        assert e.code == 3


# --- wait in a thread: replies, --from, --while-contains --------------------
# Case 2026-10-01: Dr. House answers in a thread and EDITS its one message in place
# (`:processing:` -> final). `wait` polled only conversations.history (top level) and
# fired on any new message, so "the bot has finished" was not expressible.

THREAD_URL = f"https://{WS}/archives/{CHAN}/p1700000100000000"


def test_wait_thread_url_polls_replies_not_history():
    reply = {"ts": "1700000200.000000", "user": "U9", "text": "done"}
    fake = FakeClient({"conversations_replies": {"messages": [reply]}})
    args = _args("wait", THREAD_URL, "--after-ts", "1700000150.000000", "--timeout", "5")
    out = _capture(s.cmd_wait, args, client=fake)
    assert "done" in out
    assert fake.count("conversations_history") == 0
    assert fake.last("conversations_replies")["ts"] == "1700000100.000000"


def test_wait_from_ignores_other_authors():
    other = {"ts": "1700000200.000000", "user": "U1", "text": "noise"}
    fake = FakeClient({"conversations_replies": {"messages": [other]}})
    args = _args("wait", THREAD_URL, "--after-ts", "1700000150.000000",
                 "--from", "U9", "--timeout", "0")
    try:
        _capture(s.cmd_wait, args, client=fake)
        assert False, "expected SystemExit(3): only another author replied"
    except SystemExit as e:
        assert e.code == 3


def test_wait_while_contains_waits_until_marker_leaves_edited_message():
    states = iter([":processing: analysing", ":processing: still", "result ready"])

    def replies(**_):
        return {"messages": [{"ts": "1700000200.000000", "user": "U9", "text": next(states)}]}

    fake = FakeClient({"conversations_replies": replies})
    args = _args("wait", THREAD_URL, "--after-ts", "1700000150.000000", "--from", "U9",
                 "--while-contains", ":processing:", "--interval", "0", "--timeout", "30")
    out = _capture(s.cmd_wait, args, client=fake)
    assert "result ready" in out
    assert fake.count("conversations_replies") == 3


def test_wait_while_contains_checks_blocks_not_only_text():
    msg = {"ts": "1700000200.000000", "user": "U9", "text": "",
           "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": ":processing: x"}}]}
    fake = FakeClient({"conversations_replies": {"messages": [msg]}})
    args = _args("wait", THREAD_URL, "--after-ts", "1700000150.000000", "--from", "U9",
                 "--while-contains", ":processing:", "--interval", "0", "--timeout", "0")
    try:
        _capture(s.cmd_wait, args, client=fake)
        assert False, "marker is in blocks: must keep waiting and time out"
    except SystemExit as e:
        assert e.code == 3


# --- listen -----------------------------------------------------------------

def test_listen_emits_ndjson_for_new_messages():
    new = {"ts": "1700000200.000000", "user": "U9", "text": "hello"}
    fake = FakeClient({"conversations_history": {"messages": [new]}})
    args = _args("listen", CHAN, "--after-ts", "1700000100.000000", "--interval", "0")
    args._max_iterations = 1  # test hook: one poll then stop (unbounded in prod)
    out = _capture(s.cmd_listen, args, client=fake)
    lines = [l for l in out.splitlines() if l.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])  # each new message is one NDJSON object
    assert rec["ts"] == "1700000200.000000"
    assert rec["text"] == "hello"
    assert rec["channel"] == CHAN


# --- a message URL as target means "into that thread" for every write command ------
# Regression: `md`/`table`/`blocks` had no URL->thread branch, so a thread URL silently
# degraded to "just the channel" and the post landed top-level with exit 0. Measured
# 2026-09-03 on a real report: six replies became six channel messages.

_THREAD_URL = f"https://{WS}/archives/{CHAN}/p1788271903629669"
_THREAD_TS = "1788271903.629669"


def test_url_target_resolves_to_its_thread():
    fake = FakeClient({"conversations_info": _CHAN_INFO, "team_info": _TEAM})
    channel_id, thread_ts = s.resolve_target_and_thread(_THREAD_URL, None, fake)
    assert channel_id == CHAN, channel_id
    assert thread_ts == _THREAD_TS, thread_ts


def test_explicit_thread_flag_wins_over_the_target_url():
    fake = FakeClient({"conversations_info": _CHAN_INFO, "team_info": _TEAM})
    other = f"https://{WS}/archives/{CHAN}/p1700000000000123"
    _, thread_ts = s.resolve_target_and_thread(other, _THREAD_URL, fake)
    assert thread_ts == _THREAD_TS, thread_ts


def test_plain_channel_target_stays_top_level():
    fake = FakeClient({"conversations_info": _CHAN_INFO, "team_info": _TEAM})
    _, thread_ts = s.resolve_target_and_thread(CHAN, None, fake)
    assert thread_ts is None, thread_ts


def _post_with_url_target(cmd_name, argv):
    """Runs a write command whose client is built internally, against a FakeClient."""
    fake = FakeClient({"conversations_info": _CHAN_INFO, "team_info": _TEAM,
                       "chat_postMessage": {"ts": "1788433644.134379"}})
    original = s._make_client
    s._make_client = lambda *a, **kw: fake
    try:
        _capture(getattr(s, "cmd_" + cmd_name), _args(*argv))
    finally:
        s._make_client = original
    return fake


def test_table_with_url_target_posts_into_the_thread():
    tsv = chr(9).join(("a", "b")) + chr(10) + chr(9).join(("c", "d"))
    fake = _post_with_url_target("table", ("table", _THREAD_URL, tsv, "--no-notify"))
    assert fake.last("chat_postMessage")["thread_ts"] == _THREAD_TS


def test_blocks_with_url_target_posts_into_the_thread():
    payload = '[{"type": "markdown", "text": "x"}]'
    fake = _post_with_url_target("blocks", ("blocks", _THREAD_URL, payload, "--no-notify"))
    assert fake.last("chat_postMessage")["thread_ts"] == _THREAD_TS


def test_md_with_url_target_posts_into_the_thread():
    fake = _post_with_url_target("md", ("md", _THREAD_URL, "**report**", "--no-notify"))
    assert fake.last("chat_postMessage")["thread_ts"] == _THREAD_TS


def test_buttons_on_thread_reply_url_reads_that_reply_not_channel_latest():
    """URL ответа в треде (?thread_ts=) → именно этот reply, а не последнее сообщение канала."""
    reply = {"ts": "1793279706.302369", "thread_ts": "1788599118.790599", "bot_id": "B1",
             "blocks": [{"type": "actions", "block_id": "ctl",
                         "elements": [{"type": "button", "action_id": "steer",
                                       "text": {"type": "plain_text", "text": "Go"}, "value": "v"}]}]}
    fake = FakeClient({
        "conversations_history": {"messages": [{"ts": "1793273857.202359", "text": "other"}]},
        "conversations_replies": {"messages": [{"ts": "1788599118.790599"}, reply]},
    })
    url = ("https://loyaltyplant.slack.com/archives/C09NKNZM97E/p1793279706302369"
           "?thread_ts=1788599118.790599&cid=C09NKNZM97E")
    out = json.loads(_capture(s.cmd_buttons, _args("buttons", url, "--json"), client=fake))
    assert out["ts"] == "1793279706.302369", out
    assert [e["action_id"] for e in out["elements"]] == ["steer"], out


def test_buttons_on_unknown_message_is_not_found_not_another_message():
    fake = FakeClient({
        "conversations_history": {"messages": [{"ts": "1793273857.202359", "text": "other"}]},
        "conversations_replies": {"messages": [{"ts": "1788599118.790599"}]},
    })
    url = ("https://loyaltyplant.slack.com/archives/C09NKNZM97E/p1793279706302369"
           "?thread_ts=1788599118.790599&cid=C09NKNZM97E")
    try:
        _capture(s.cmd_buttons, _args("buttons", url, "--json"), client=fake)
    except s.InteractionError as e:
        assert "not_found" in str(e) or getattr(e, "code", "") == "message_not_found", e
    else:
        raise AssertionError("expected message_not_found")


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
