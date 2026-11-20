"""Regression tests for slack_ask.py defects 1..4 (2026-11-13)."""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import types

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.modules.setdefault("slack_sdk", types.SimpleNamespace(WebClient=object))

import slack_ask as sa  # noqa: E402

ask_owner = sa._ask_owner()

BOT, OWNER = "UBOT", "UOWNER"
TG_OWNER = 122920894
THREAD = "https://loyaltyplant.slack.com/archives/DSELF/p1791900000000001"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Workspace:
    def __init__(self) -> None:
        self.messages: dict[str, list[dict]] = {}
        self.seq = 0
        self.chat_updates: list[tuple[str, str, str, list]] = []

    def post(self, user: str, channel: str, text: str, thread_ts: str | None = None, blocks: list | None = None) -> dict:
        self.seq += 1
        ts = f"1791900000.{self.seq:06d}"
        msg = {"ts": ts, "user": user, "text": text, "blocks": blocks or [], "reactions": []}
        if thread_ts:
            msg["thread_ts"] = thread_ts
            parent = self.find(channel, thread_ts)
            parent["reply_count"] = parent.get("reply_count", 0) + 1
            parent["latest_reply"] = ts
        self.messages.setdefault(channel, []).append(msg)
        return msg

    def update(self, channel: str, ts: str, text: str, blocks: list | None = None) -> None:
        self.chat_updates.append((channel, ts, text, blocks or []))
        msg = self.find(channel, ts)
        msg["text"], msg["blocks"] = text, blocks or []

    def find(self, channel: str, ts: str) -> dict:
        return next(m for m in self.messages[channel] if m["ts"] == ts)

    def react(self, channel: str, ts: str, name: str, user: str) -> None:
        msg = self.find(channel, ts)
        entry = next((r for r in msg["reactions"] if r["name"] == name), None)
        if entry is None:
            msg["reactions"].append({"name": name, "users": [user], "count": 1})
        elif user not in entry["users"]:
            entry["users"].append(user)

    def top(self, channel: str) -> list[dict]:
        return [m for m in self.messages.get(channel, []) if "thread_ts" not in m]

    def thread(self, channel: str, ts: str) -> list[dict]:
        return [m for m in self.messages.get(channel, []) if m["ts"] == ts or m.get("thread_ts") == ts]


class FakeSlackError(Exception):
    def __init__(self, code: str = "invalid_auth", status: int = 401) -> None:
        super().__init__(code)
        self.response = {"error": code, "status_code": status, "headers": {}}

    @property
    def status_code(self) -> int:
        return self.response["status_code"]

    @property
    def headers(self) -> dict:
        return self.response["headers"]


class FakeClient:
    def __init__(self, ws: Workspace, user: str) -> None:
        self.ws, self.user = ws, user
        self.history_calls = 0
        self.history_errors: list[Exception] = []
        self.auth_error: Exception | None = None

    def auth_test(self) -> dict:
        if self.auth_error:
            raise self.auth_error
        return {"user_id": self.user}

    def conversations_open(self, users: str) -> dict:
        return {"channel": {"id": "DBOT" if self.user == BOT else "DSELF"}}

    def chat_postMessage(self, channel: str, text: str = "", thread_ts: str | None = None, blocks: list | None = None, **_) -> dict:
        return {"ts": self.ws.post(self.user, channel, text, thread_ts, blocks)["ts"]}

    def chat_update(self, channel: str, ts: str, text: str = "", blocks: list | None = None, **_) -> None:
        self.ws.update(channel, ts, text, blocks)

    def chat_getPermalink(self, channel: str, message_ts: str) -> dict:
        return {"permalink": f"https://loyaltyplant.slack.com/archives/{channel}/p{message_ts.replace('.', '')}"}

    def reactions_add(self, channel: str, timestamp: str, name: str) -> None:
        self.ws.react(channel, timestamp, name, self.user)

    def conversations_history(self, channel: str, oldest: str, inclusive: bool = True, limit: int = 200, cursor: str | None = None) -> dict:
        self.history_calls += 1
        if self.history_errors:
            error = self.history_errors.pop(0) if len(self.history_errors) > 1 else self.history_errors[0]
            raise error
        msgs = [m for m in self.ws.top(channel) if float(m["ts"]) >= float(oldest)]
        return json.loads(json.dumps({"messages": msgs[::-1], "response_metadata": {"next_cursor": ""}}))

    def conversations_replies(self, channel: str, ts: str, limit: int = 200) -> dict:
        return json.loads(json.dumps({"messages": self.ws.thread(channel, ts)}))


class FakeTelegram:
    def __init__(self, clock: Clock) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.updates: list[dict] = []
        self.next_id = 100
        self.next_update = 1
        self.clock = clock
        self.transient_edit_failures = 0

    def __call__(self, method: str, **params):
        self.calls.append((method, params))
        if method in ("editMessageText", "editMessageReplyMarkup") and self.transient_edit_failures > 0:
            self.transient_edit_failures -= 1
            raise OSError("[Errno 104] Connection reset by peer")
        if method == "getUpdates":
            ready = [u for u in self.updates if u["update_id"] >= params.get("offset", 0)]
            if not ready:
                self.clock.now += params.get("timeout", 0) or 1
            return ready
        if method == "sendMessage":
            self.next_id += 1
            return {"message_id": self.next_id}
        return True

    def tap(self, data: str, user: int = TG_OWNER) -> None:
        self.updates.append({
            "update_id": self.next_update,
            "callback_query": {"id": f"cb{self.next_update}", "from": {"id": user}, "data": data},
        })
        self.next_update += 1


SYSTEM_CALLS: list[list[str]] = []


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    """No test touches the real machine (2026-11-20 incident: tests typed fixture «owner decisions» into the live
    orchestrator session via real `tmux send-keys`): HOME and ~/.claude in tmp, PATH stubs for tmux/claude that exit
    97, no inherited session id, and the real Runner refuses tmux/claude/ps — only a fake `ctx.runner` may answer."""
    home = tmp_path / "sandbox-home"
    stubs = tmp_path / "stub-bin"
    stubs.mkdir()
    for tool in ("tmux", "claude"):
        (stubs / tool).write_text("#!/bin/sh\nexit 97\n")
        (stubs / tool).chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(sa, "CLAUDE_HOME", home / ".claude")
    SYSTEM_CALLS.clear()

    def refuse(kind):
        def guard(self, argv, *args, **kwargs):
            SYSTEM_CALLS.append(list(argv))
            if kind == "run" or pathlib.Path(argv[0]).name in ("tmux", "claude", "ps"):
                raise AssertionError(f"real system call through the production Runner: {argv[:4]}")
            return real_start(self, argv, *args, **kwargs)
        return guard

    real_start = sa.Runner.start
    monkeypatch.setattr(sa.Runner, "run", refuse("run"))
    monkeypatch.setattr(sa.Runner, "start", refuse("start"))
    yield
    stray = [c for c in SYSTEM_CALLS if c]
    SYSTEM_CALLS.clear()
    assert not stray or getattr(sa, "_GUARD_SELFTEST", False), f"production code reached the system seam: {stray[:3]}"


def test_sandbox_guard_refuses_real_tmux_claude_and_stubs_exit_97(tmp_path):
    """The guard: the production Runner may not call tmux/claude/ps, and a stray subprocess reaches only the stubs."""
    import subprocess
    with pytest.raises(AssertionError):
        sa.Runner().run(["tmux", "send-keys", "-t", "%1", "-l", "x"])
    with pytest.raises(AssertionError):
        sa.Runner().start(["claude", "--bg", "--resume", "s"], tmp_path / "l", tmp_path / "e")
    SYSTEM_CALLS.clear()  # the two refusals above are the point of this test
    assert subprocess.run(["tmux", "list-panes"]).returncode == 97
    assert subprocess.run(["claude", "--version"]).returncode == 97
    assert os.environ["HOME"].endswith("sandbox-home") and "CLAUDE_CODE_SESSION_ID" not in os.environ


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(sa, "BATCH_HOME", tmp_path / "home")
    monkeypatch.setattr(ask_owner, "CONSUMER_LOCK", tmp_path / "consumer.json")
    monkeypatch.setenv("OWNER_CHANNELS_CONFIG", str(tmp_path / "missing-channels.json"))
    clock = Clock()
    ws = Workspace()
    tg = FakeTelegram(clock)

    def ctx(tg_on: bool = True, usr: FakeClient | None = None):
        u = FakeClient(ws, OWNER) if usr is None else usr
        return sa.Ctx(FakeClient(ws, BOT), u, tg if tg_on else None, TG_OWNER, owner=OWNER,
                      origin="rdsh2", clock=clock, sleep=clock.sleep, runner=FakeRunner())

    return types.SimpleNamespace(ws=ws, tg=tg, clock=clock, ctx=ctx, home=tmp_path / "home", tmp_path=tmp_path)


def test_defect1_cross_channel_close_marks_both_channels_and_escalation_creates_no_duplicate_card(env):
    """Defect 1:
    - telegram-first escalation to Slack must not send a second Telegram card (which orphaned the first card with buttons);
    - answering in Slack must edit the Telegram card text with the outcome AND remove its reply_markup (retrying transient resets);
    - answering in Telegram must update the Slack root card via chat_update with the outcome.
    """
    spec = {
        "title": "Проверка закрытия карточек",
        "channels": {"mode": "telegram-first", "escalate_after": 60},
        "approve_only": True,
        "items": [
            {"id": "TASK-101", "title": "Ответим в Slack", "payload": "Первый вопрос", "yesno": True},
            {"id": "TASK-102", "title": "Ответим в Telegram", "payload": "Второй вопрос", "yesno": True},
        ],
    }
    c = env.ctx()
    st = sa.batch_send(c, spec, "b-cross")
    tg_card_1 = st["items"][0]["rounds"][0]["telegram"]["card_id"]
    tg_card_2 = st["items"][1]["rounds"][0]["telegram"]["card_id"]

    # Escalate after 60s to Slack: must NOT create duplicate Telegram cards
    env.clock.now += 61
    st = sa.load_batch("b-cross")
    sa.send_escalations(c, st)
    sa.save_batch(st)
    assert st["items"][0]["rounds"][0]["telegram"]["card_id"] == tg_card_1
    assert st["items"][1]["rounds"][0]["telegram"]["card_id"] == tg_card_2

    # Answer item 1 in Slack (with one transient Telegram reset to prove retry on card close)
    slack_ts_1 = st["items"][0]["rounds"][0]["slack"]["ts"]
    slack_ts_2 = st["items"][1]["rounds"][0]["slack"]["ts"]
    env.tg.transient_edit_failures = 1
    env.ws.react("DBOT", slack_ts_1, "white_check_mark", OWNER)

    # Answer item 2 in Telegram
    env.tg.tap(f"sa|{st['key']}|2|1|r")
    assert sa.batch_watch(c, names=["b-cross"], timeout=20, slack_poll=0, tg_poll=5) == 0

    # Item 1 (answered in Slack): Telegram card must have editMessageText with outcome AND empty inline_keyboard
    tg_text_edits_1 = [p for m, p in env.tg.calls if m == "editMessageText" and p.get("message_id") == tg_card_1]
    assert tg_text_edits_1, "Telegram card text was not updated when answered in Slack"
    assert "Принято" in tg_text_edits_1[-1]["text"]
    tg_markup_clears_1 = [
        p for m, p in env.tg.calls
        if m in ("editMessageReplyMarkup", "editMessageText")
        and p.get("message_id") == tg_card_1
        and p.get("reply_markup") == {"inline_keyboard": []}
    ]
    assert tg_markup_clears_1, "Telegram card buttons were not cleared when answered in Slack"

    # Item 2 (answered in Telegram): Slack root card must be updated via chat_update with outcome
    slack_updates_2 = [(ch, ts, txt, blks) for ch, ts, txt, blks in env.ws.chat_updates if ts == slack_ts_2]
    assert slack_updates_2, "Slack root card was not updated via chat_update when answered in Telegram"
    updated_block_text = json.dumps(slack_updates_2[-1][3], ensure_ascii=False)
    assert "не ок" in updated_block_text or "Отклонено" in updated_block_text or "Принято" in updated_block_text


def test_defect2_default_card_timeout_is_8h_and_cli_wait_timeout_keeps_card_open_for_late_answer(env, monkeypatch, capsys):
    """Defect 2:
    - Default card lifetime is at least 8 hours (28800 s);
    - Expiry of the waiting CLI process (--timeout) returns exit 3 WITHOUT expiring the card itself;
    - A late answer (e.g. 1 hour later) is still recorded in state/journal and accepted by batch wait & verify.
    """
    monkeypatch.setenv("SLACK_ASK_MIN_TIMEOUT", "0")
    monkeypatch.setattr(sa, "make_ctx", lambda args, telegram=True: env.ctx(tg_on=False))

    code = sa.main(["PLAT-4888: опубликовать вердикт в Jira?", "--yesno", "--channels", "slack-only", "--timeout", "5", "--json"])
    assert code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["answer"] == "timeout"

    [batch_name] = sa.list_batches()
    st = sa.load_batch(batch_name)
    item = st["items"][0]
    assert item["timeout"] >= 8 * 3600, f"Expected card TTL >= 28800s (8h), got {item['timeout']}"
    assert item["status"] == "pending", f"CLI wait timeout must not expire the card, got status={item['status']}"

    # 1 hour later (3600s), the owner answers ✅ in Slack
    env.clock.now += 3600
    monkeypatch.setattr(sa.time, "time", lambda: env.clock.now)
    env.ws.react("DBOT", item["rounds"][0]["slack"]["ts"], "white_check_mark", OWNER)
    assert sa.batch_watch(env.ctx(tg_on=False), names=[batch_name], timeout=10, slack_poll=0, tg_poll=5) == 0

    st_after = sa.load_batch(batch_name)
    assert st_after["items"][0]["status"] == "done"
    assert sa.batch_wait(batch_name, None, timeout=5, poll=0) == 0
    capsys.readouterr()
    assert sa.verify([out["permalink"], "--key", "PLAT-4888", "--owner", OWNER, "--require", "Jira"]) == 0


def test_defect3_copy_thread_reply_read_after_token_refresh_and_loud_exit_when_token_truly_dead(env, monkeypatch, capsys):
    """Defect 3:
    - Part A: If watch previously lost ctx.usr on an expired token, and .env is later refreshed before/after a new
      batch is sent with copy_ts, watch reloads ctx.usr and reads the owner's reply in the copy thread (DSELF).
    - Part B: If the owner's token is truly dead on a copy batch, watch notifies Telegram and waiting process exits non-zero (7).
    """
    # Part A: watch had lost ctx.usr earlier, then token was refreshed on disk
    sender_ctx = env.ctx()
    st = sa.batch_send(sender_ctx, {
        "title": "PLAT-4888 · Jira RTR?",
        "channels": {"mode": "slack-only", "escalate_after": 60},
        "approve_only": True,
        "items": [{"id": "PLAT-4888", "title": "Jira: публикую вердикт?", "payload": "Текст", "yesno": True}],
    }, "b-copy")
    copy_ts = st["items"][0]["rounds"][0]["slack"]["copy_ts"]

    watch_ctx = env.ctx()
    watch_ctx.usr = None
    watch_ctx.owner_auth_reload_attempted = True
    watch_ctx.reload_owner_client = lambda: FakeClient(env.ws, OWNER)

    # Owner replies in the copy thread
    reply_text = "вот тут проблема: ищи в самой задаче описание поведения и по нему дай вердикт"
    env.ws.post(OWNER, "DSELF", reply_text, thread_ts=copy_ts)

    assert sa.batch_watch(watch_ctx, names=["b-copy"], timeout=15, slack_poll=0, tg_poll=5) == 0
    updated = sa.load_batch("b-copy")["items"][0]
    assert updated["status"] != "pending", "Owner's reply in copy thread was ignored; item stayed pending"

    # Part B: When token is genuinely dead, waiting process must fail loudly (non-zero exit + Telegram notice)
    st2 = sa.batch_send(env.ctx(), {
        "title": "PLAT-4889 · Dead token test",
        "channels": {"mode": "slack-only", "escalate_after": 60},
        "approve_only": True,
        "items": [{"id": "PLAT-4889", "title": "Вопрос", "payload": "Текст", "yesno": True}],
    }, "b-dead-auth")
    dead_ctx = env.ctx()
    err = FakeSlackError(code="invalid_auth", status=401)
    dead_ctx.usr._client.history_errors = [err]
    bad = FakeClient(env.ws, OWNER)
    bad.auth_error = err
    bad.history_errors = [err]
    notices: list[int] = []
    dead_ctx.reload_owner_client = lambda: bad
    dead_ctx.auth_notifier = lambda: notices.append(1)

    states = {"b-dead-auth": st2}
    sa.poll_slack_batches(dead_ctx, states)
    sa.save_batch(states["b-dead-auth"])
    assert notices == [1]
    wait_code = sa.batch_wait("b-dead-auth", None, timeout=5, poll=0)
    assert wait_code != 0 and wait_code != 3, f"Expected loud non-zero exit (not timeout 3) on dead owner token, got {wait_code}"


def test_defect4_telegram_first_button_answer_has_verifiable_permalink_and_verify_accepts_batch_id(env, monkeypatch, capsys):
    """Defect 4:
    - With --channels telegram-first, answering by button in Telegram before escalation still produces a non-null
      verifiable permalink in slack-ask output;
    - slack-ask verify accepts both the returned permalink and the batch ID.
    """
    monkeypatch.setenv("SLACK_ASK_MIN_TIMEOUT", "0")
    monkeypatch.setattr(sa.time, "time", lambda: env.clock.now)
    monkeypatch.setattr(sa, "make_ctx", lambda args, telegram=True: env.ctx(tg_on=True))

    real_send = sa.batch_send

    def send_and_tap_tg(ctx, spec_, name):
        st = real_send(ctx, spec_, name)
        env.tg.tap(f"sa|{st['key']}|1|1|a", user=TG_OWNER)
        return st

    monkeypatch.setattr(sa, "batch_send", send_and_tap_tg)
    code = sa.main([
        "PLAT-4888: опубликовать вердикт в Jira?",
        "--yesno",
        "--channels",
        "telegram-first",
        "--timeout",
        "10",
        "--json",
    ])
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result.get("permalink"), f"Expected non-null permalink on telegram-first answer, got {result}"
    assert sa.verify([result["permalink"], "--key", "PLAT-4888", "--owner", OWNER, "--require", "Jira"]) == 0
    capsys.readouterr()
    [batch_name] = sa.list_batches()
    assert sa.verify([batch_name, "--key", "PLAT-4888", "--owner", OWNER, "--require", "Jira"]) == 0


def test_copy_thread_reply_written_before_escalation_is_not_swallowed_by_the_round_notice(env):
    """2026-11-18 TASK-1319: the owner answered in the copy thread 72 s after a slack-first card; the 60 s escalation
    re-sent the round, posted «раунд 1 готов» into the copy thread from the owner's token and set copy_latest to that
    notice, so the poll that followed in the same cycle saw latest_reply == copy_latest and never read the answer."""
    c = env.ctx()
    st = sa.batch_send(c, {
        "title": "TASK-1319 · касса",
        "channels": {"mode": "slack-first", "escalate_after": 60},
        "approve_only": True,
        "items": [{"id": "TASK-1319", "title": "Как закрываем TC-358?", "payload": "Текст",
                   "options": ["принять по unit", "попросить Qatar"]}],
    }, "b-esc-race")
    copy_ts = st["items"][0]["rounds"][0]["slack"]["copy_ts"]
    sa.poll_slack_batches(c, {"b-esc-race": st})  # a poll before the answer: nothing new
    reply = "перебирай рекурсивно ищи подходящее уверен что нет подходящих касс в контурах тест6 и тест3?"
    env.ws.post(OWNER, "DSELF", reply, thread_ts=copy_ts)
    env.clock.now += 61
    sa.send_escalations(c, st)  # same cycle order as batch_watch: escalation, then the Slack poll
    sa.poll_slack_batches(c, {"b-esc-race": st})
    item = st["items"][0]
    texts = json.dumps(st.get("to_agent", []), ensure_ascii=False) + json.dumps(item, ensure_ascii=False)
    assert reply in texts, "the owner's copy-thread answer was swallowed by the escalation notice"


QUESTION = "Причем тут пересинк на проде? Какой вердикт на тесте e2e? Синк ломается если меню не снести или что?"


def _incident_spec(batch_id_title="RTR-B1G1"):
    return {
        "title": "B1G1 — что дальше",
        "channels": {"mode": "slack-first", "escalate_after": 0},
        "approve_only": True,
        "items": [{"id": batch_id_title, "title": "Переводим в RTR?", "payload": "Текст",
                   "options": ["🚀 RTR всем трём", "🚀 RTR только 3480 + 3384", "⏸ Пока не переводить"]}],
    }


def test_copy_thread_reply_is_read_without_intermediate_agent_reply(env):
    """2026-11-19 rtr-b1g1: send → poll → owner writes in the copy thread → poll must see it (no `batch reply` between)."""
    c = env.ctx()
    st = sa.batch_send(c, _incident_spec(), "b-copy-direct")
    copy_ts = st["items"][0]["rounds"][0]["slack"]["copy_ts"]
    sa.poll_slack_batches(c, {"b-copy-direct": st})
    env.ws.post(OWNER, "DSELF", "Почему не третий вариант?!", thread_ts=copy_ts)
    sa.poll_slack_batches(c, {"b-copy-direct": st})
    assert "Почему не третий вариант?!" in json.dumps(st["items"][0], ensure_ascii=False) + json.dumps(st.get("to_agent", []), ensure_ascii=False)
    assert st["items"][0]["rounds"][-1]["slack"]["copy_latest"]


def test_question_in_copy_thread_is_not_a_choice(env):
    """2026-11-19 rtr-b1g1: a question to the agent written in the copy thread was recorded as choice=0 with the
    question as choice_label and closed the item; it must go to the agent and leave the item open."""
    c = env.ctx()
    st = sa.batch_send(c, _incident_spec(), "b-copy-question")
    copy_ts = st["items"][0]["rounds"][0]["slack"]["copy_ts"]
    env.ws.post(OWNER, "DSELF", QUESTION, thread_ts=copy_ts)
    sa.poll_slack_batches(c, {"b-copy-question": st})
    item = st["items"][0]
    assert item["status"] in sa.OPEN, item["status"]
    assert item.get("choice") is None and not item.get("choice_label")
    assert [n["text"] for n in st["to_agent"]] == [QUESTION]
    assert st["to_agent"][0]["via"] == "slack:thread-question"


def test_free_text_without_question_in_copy_thread_is_still_own_option(env):
    c = env.ctx()
    st = sa.batch_send(c, _incident_spec(), "b-copy-own")
    copy_ts = st["items"][0]["rounds"][0]["slack"]["copy_ts"]
    env.ws.post(OWNER, "DSELF", "Переводим только после проверки на проде", thread_ts=copy_ts)
    sa.poll_slack_batches(c, {"b-copy-own": st})
    assert st["items"][0]["choice"] == 0


def test_question_with_option_number_in_copy_thread_is_still_a_choice(env):
    c = env.ctx()
    st = sa.batch_send(c, _incident_spec(), "b-copy-num")
    copy_ts = st["items"][0]["rounds"][0]["slack"]["copy_ts"]
    env.ws.post(OWNER, "DSELF", "2, ок?", thread_ts=copy_ts)
    sa.poll_slack_batches(c, {"b-copy-num": st})
    assert st["items"][0]["choice"] == 2


@pytest.mark.parametrize("age", [0, 599, 600, 3599, 3600, 7200, 86400])
def test_slack_poll_cadence_never_exceeds_a_minute(age):
    """2026-11-19 rtr-b1g1: the cadence grew to 600 s for cards older than an hour — an owner answer waited up to 10 min."""
    assert sa.slack_cadence(age, 20) <= 60
    assert sa.slack_cadence(age, 0) == 0


# --- batch edit: the sent card is redrawn in place (owner 2026-11-19) -----------------------------------------------

EDIT_SPEC = {
    "title": "edit", "approve_only": True,
    "items": [{"id": "TASK-201", "title": "выбор", "detail": "старая деталь", "payload": "Текст вопроса",
               "options": ["A", "B"]}],
}


def _sent_for_edit(env, name="b-edit"):
    c = env.ctx()
    st = sa.batch_send(c, json.loads(json.dumps(EDIT_SPEC)), name)
    rnd = st["items"][0]["rounds"][0]
    return c, st["key"], rnd["slack"]["ts"], rnd["telegram"]["card_id"]


def _api_calls(env) -> int:
    return len(env.tg.calls) + len(env.ws.chat_updates) + sum(len(v) for v in env.ws.messages.values())


def test_edit_detail_redraws_both_cards_in_place_and_adds_one_thread_line(env):
    c, _key, ts, card = _sent_for_edit(env)
    roots, calls0, updates0 = len(env.ws.top("DBOT")), len(env.tg.calls), len(env.ws.chat_updates)
    thread0 = len(env.ws.thread("DBOT", ts))

    sa.batch_edit(c, sa.load_batch("b-edit"), "TASK-201", {"detail": "новая деталь"})

    mine = [u for u in env.ws.chat_updates[updates0:] if u[1] == ts]
    assert len(mine) == 1 and mine[0][0] == "DBOT"
    assert "новая деталь" in json.dumps(mine[0][3], ensure_ascii=False)
    assert len(env.ws.top("DBOT")) == roots
    added = env.ws.thread("DBOT", ts)[thread0:]
    assert len(added) == 1 and added[0]["text"].startswith("✏️ карточка обновлена")
    edits = [p for m, p in env.tg.calls[calls0:] if m == "editMessageText"]
    assert len(edits) == 1 and edits[0]["message_id"] == card
    assert "новая деталь" in edits[0]["text"]
    buttons = [b["text"] for row in edits[0]["reply_markup"]["inline_keyboard"] for b in row]
    assert any("A" in b for b in buttons) and any("B" in b for b in buttons)
    sends = [p for m, p in env.tg.calls[calls0:] if m == "sendMessage"]
    assert all(p.get("reply_to_message_id") == card for p in sends), "a Telegram notice must be a reply, not a root"
    st = sa.load_batch("b-edit")
    assert st["items"][0]["detail"] == "новая деталь" and st["items"][0]["revision"] == 1
    assert "ред. 1" in sa.batch_show(st)


def test_edit_does_not_interrupt_wait_and_owner_choice_still_counts(env):
    c, key, _ts, _card = _sent_for_edit(env)
    sa.batch_edit(c, sa.load_batch("b-edit"), "TASK-201", {"detail": "новая деталь", "payload": "Новый текст"})
    assert sa.load_batch("b-edit")["items"][0]["status"] == "pending"
    env.tg.tap(f"sa|{key}|1|1|c2")
    assert sa.batch_watch(c, names=["b-edit"], timeout=20, slack_poll=0, tg_poll=5) == 0
    item = sa.load_batch("b-edit")["items"][0]
    assert item["status"] == "done" and item["choice"] == 2 and item["choice_label"] == "B"
    assert sa.batch_wait("b-edit", None, timeout=5, poll=0, clock=env.clock, sleep=env.clock.sleep) == 0


def test_edit_of_decided_item_exits_2_without_api_calls(env, monkeypatch, capsys):
    c, key, _ts, _card = _sent_for_edit(env)
    env.tg.tap(f"sa|{key}|1|1|c1")
    sa.batch_watch(c, names=["b-edit"], timeout=20, slack_poll=0, tg_poll=5)
    assert sa.load_batch("b-edit")["items"][0]["status"] == "done"
    monkeypatch.setattr(sa, "make_ctx", lambda args, telegram=True: env.ctx())
    before = _api_calls(env)
    assert sa.batch_cli(["edit", "--batch", "b-edit", "--item", "TASK-201", "--detail", "поздно"]) == 2
    assert "уже решена" in capsys.readouterr().err
    assert _api_calls(env) == before


def test_edit_that_changes_options_ignores_stale_number_reaction(env):
    c, _key, ts, _card = _sent_for_edit(env)
    env.ws.react("DBOT", ts, "two", OWNER)  # «B» — not yet polled
    sa.batch_edit(c, sa.load_batch("b-edit"), "TASK-201", {"options": ["A", "новый вариант"]})
    note = env.ws.thread("DBOT", ts)[-1]["text"]
    assert "✏️ карточка обновлена" in note and "2" in note and "реакци" in note
    states = {"b-edit": sa.load_batch("b-edit")}
    sa.poll_slack_batches(c, states)
    sa.save_batch(states["b-edit"])
    assert sa.load_batch("b-edit")["items"][0]["status"] == "pending", "the old reaction must not choose the new option"
    # a fresh reaction after toggling off/on counts as before
    env.ws.find("DBOT", ts)["reactions"] = [r for r in env.ws.find("DBOT", ts)["reactions"] if r["name"] != "two"]
    states = {"b-edit": sa.load_batch("b-edit")}
    sa.poll_slack_batches(c, states)
    sa.save_batch(states["b-edit"])
    env.ws.react("DBOT", ts, "two", OWNER)
    states = {"b-edit": sa.load_batch("b-edit")}
    sa.poll_slack_batches(c, states)
    sa.save_batch(states["b-edit"])
    item = sa.load_batch("b-edit")["items"][0]
    assert item["status"] == "done" and item["choice_label"] == "новый вариант"


@pytest.mark.parametrize("changes", [{"options": ["один"]}, {"options": ["a", ""]}, {"payload_file": "/nonexistent/p.md"},
                                     {}, {"options": ["a"] * 10}])
def test_edit_with_invalid_spec_is_refused_without_api_calls(env, changes):
    c, _key, _ts, _card = _sent_for_edit(env)
    before = _api_calls(env)
    with pytest.raises(ValueError):
        sa.batch_edit(c, sa.load_batch("b-edit"), "TASK-201", changes)
    assert _api_calls(env) == before


def test_edit_cli_spec_file_invalid_exits_2_before_any_client(env, monkeypatch, tmp_path, capsys):
    _sent_for_edit(env)
    spec = tmp_path / "e.json"
    spec.write_text(json.dumps({"options": ["only-one"]}), encoding="utf-8")

    def boom(args, telegram=True):
        raise AssertionError("no client may be built for a refused edit")

    monkeypatch.setattr(sa, "make_ctx", boom)
    assert sa.batch_cli(["edit", "--batch", "b-edit", "--item", "TASK-201", "--spec", str(spec)]) == 2
    assert "options" in capsys.readouterr().err


def test_edit_payload_updates_snapshot_sha_and_text_in_place(env):
    c, _key, ts, card = _sent_for_edit(env)
    calls0 = len(env.tg.calls)
    sa.batch_edit(c, sa.load_batch("b-edit"), "TASK-201", {"payload": "Совсем другой текст"})
    item = sa.load_batch("b-edit")["items"][0]
    assert item["sha256"] == sa.sha256("Совсем другой текст") == item["rounds"][-1]["sha256"]
    assert pathlib.Path(item["rounds"][-1]["snapshot"]).read_text(encoding="utf-8") == "Совсем другой текст"
    root = env.ws.find("DBOT", ts)
    assert "Совсем другой текст" in json.dumps(root["blocks"], ensure_ascii=False)
    body = [p for m, p in env.tg.calls[calls0:] if m == "editMessageText" and p["message_id"] != card]
    assert body and "Совсем другой текст" in body[0]["text"]


def test_edit_shrinking_long_payload_marks_old_continuations_replaced_without_new_messages(env):
    c = env.ctx()
    spec = json.loads(json.dumps(EDIT_SPEC))
    spec["items"][0]["payload"] = "длинная строка текста\n" * 600
    st = sa.batch_send(c, spec, "b-long")
    ts = st["items"][0]["rounds"][0]["slack"]["ts"]
    parts = st["items"][0]["rounds"][0]["slack"]["body_parts"]
    tg_body = st["items"][0]["rounds"][0]["telegram"]["body_ids"]
    assert len(parts) >= 2 and len(tg_body) >= 2
    posts0, calls0 = sum(len(v) for v in env.ws.messages.values()), len(env.tg.calls)
    sa.batch_edit(c, sa.load_batch("b-long"), "TASK-201", {"payload": "коротко"})
    assert sum(len(v) for v in env.ws.messages.values()) == posts0 + 1  # only the «✏️» line
    for part_ts in parts:
        assert "заменена" in env.ws.find("DBOT", part_ts)["text"]
    edited = {p["message_id"]: p["text"] for m, p in env.tg.calls[calls0:] if m == "editMessageText"}
    assert "коротко" in edited[tg_body[0]] and all("заменена" in edited[mid] for mid in tg_body[1:])
    item = sa.load_batch("b-long")["items"][0]
    assert item["rounds"][0]["slack"]["body_parts"] == [] and item["rounds"][0]["telegram"]["body_ids"] == tg_body[:1]


# --- batch edit: branches the first tests did not pin (acceptance r1) ---------------------------------------------

def _reactions_remove(self, channel: str, timestamp: str, name: str) -> None:
    msg = self.ws.find(channel, timestamp)
    msg["reactions"] = [r for r in msg["reactions"] if r["name"] != name]


def _bot_presets(env, ts) -> set[str]:
    return {r["name"] for r in env.ws.find("DBOT", ts)["reactions"] if BOT in r["users"]}


def test_edit_options_change_keeps_preset_reactions_in_step_with_new_options(env, monkeypatch):
    monkeypatch.setattr(FakeClient, "reactions_remove", _reactions_remove, raising=False)
    c, _key, ts, _card = _sent_for_edit(env)
    assert _bot_presets(env, ts) == {"one", "two", "x"}
    sa.batch_edit(c, sa.load_batch("b-edit"), "TASK-201", {"options": ["A", "B", "C", "D"]})
    assert _bot_presets(env, ts) == {"one", "two", "three", "four", "x"}
    sa.batch_edit(c, sa.load_batch("b-edit"), "TASK-201", {"options": ["A", "B"]})
    assert _bot_presets(env, ts) == {"one", "two", "x"}


def test_edit_is_not_a_decision_last_status_and_show_stay(env):
    c, _key, _ts, _card = _sent_for_edit(env)
    before = sa.load_batch("b-edit")["items"][0]
    assert before["last"]["decision"] == "sent"
    sa.batch_edit(c, sa.load_batch("b-edit"), "TASK-201", {"detail": "новая деталь"})
    st = sa.load_batch("b-edit")
    assert st["items"][0]["last"] == before["last"] and st["items"][0]["status"] == "pending"
    assert "sent" in sa.batch_show(st) and "edited" not in sa.batch_show(st)
    journal = [json.loads(ln) for ln in (sa.batch_dir("b-edit") / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
    assert journal[-1]["decision"] == "edited" and journal[-1]["revision"] == 1


def test_edit_slack_failure_is_exit_1_pending_redraw_and_repeat_finishes(env, monkeypatch, capsys):
    c, _key, ts, _card = _sent_for_edit(env)
    monkeypatch.setattr(sa, "make_ctx", lambda args, telegram=True: c)
    real = Workspace.update

    def broken(self, *a, **k):
        raise FakeSlackError("ratelimited", 429)

    monkeypatch.setattr(Workspace, "update", broken)
    argv = ["edit", "--batch", "b-edit", "--item", "TASK-201", "--detail", "новая деталь"]
    assert sa.batch_cli(argv) == 1
    err = capsys.readouterr().err
    assert "repeat the same edit" in err
    st = sa.load_batch("b-edit")
    assert st["items"][0]["redraw_pending"] is True and st["items"][0]["detail"] == "новая деталь"
    assert "перерисовка не завершена" in sa.batch_show(st)
    monkeypatch.setattr(Workspace, "update", real)
    assert sa.batch_cli(argv) == 0  # the same edit: values equal, but the pending redraw lets it through
    st = sa.load_batch("b-edit")
    assert "redraw_pending" not in st["items"][0]
    assert "перерисовка не завершена" not in sa.batch_show(st)
    assert "новая деталь" in json.dumps(env.ws.find("DBOT", ts)["blocks"], ensure_ascii=False)


def test_edit_telegram_failure_is_reported_and_keeps_redraw_pending(env):
    c, _key, _ts, _card = _sent_for_edit(env)
    real = env.tg.__class__.__call__

    def failing(self, method, **params):
        if method == "sendMessage":
            raise RuntimeError("telegram down")
        return real(self, method, **params)

    env.tg.__class__.__call__ = failing
    try:
        with pytest.raises(RuntimeError, match="Telegram"):
            sa.batch_edit(c, sa.load_batch("b-edit"), "TASK-201", {"detail": "новая деталь"})
    finally:
        env.tg.__class__.__call__ = real
    assert sa.load_batch("b-edit")["items"][0]["redraw_pending"] is True
    sa.batch_edit(c, sa.load_batch("b-edit"), "TASK-201", {"detail": "новая деталь"})
    assert "redraw_pending" not in sa.load_batch("b-edit")["items"][0]


def _multi_sent(env):
    c = env.ctx()
    spec = {"title": "m", "channels": "telegram-only", "approve_only": True,
            "recipients": [{"chat_id": 777, "name": "Вася"}],
            "items": [{"id": "TASK-301", "title": "выбор", "detail": "деталь", "payload": "Текст", "options": ["A", "B"]}]}
    st = sa.batch_send(c, spec, "b-multi")
    return c, st["key"], st["items"][0]["rounds"][0]["telegram"]["chats"]


def test_edit_multi_recipient_answered_keeps_answered_keyboard_and_options_change_is_refused(env):
    c, key, chats = _multi_sent(env)
    assert set(chats) == {str(TG_OWNER), "777"}
    env.tg.tap(f"sa|{key}|1|1|c1", user=777)
    sa.batch_watch(c, names=["b-multi"], timeout=10, slack_poll=0, tg_poll=5)
    item = sa.load_batch("b-multi")["items"][0]
    assert item["status"] == "pending" and "777" in item["answers"]
    calls0 = len(env.tg.calls)
    with pytest.raises(ValueError, match="кто-то из получателей уже ответил"):
        sa.batch_edit(c, sa.load_batch("b-multi"), "TASK-301", {"options": ["X", "Y"]})
    assert len(env.tg.calls) == calls0
    sa.batch_edit(c, sa.load_batch("b-multi"), "TASK-301", {"detail": "новая деталь"})
    edits = {p["chat_id"]: p for m, p in env.tg.calls[calls0:] if m == "editMessageText"}
    assert set(edits) == {TG_OWNER, 777}
    texts = lambda p: [b["text"] for row in p["reply_markup"]["inline_keyboard"] for b in row]  # noqa: E731
    assert sa.CHANGE_ANSWER in texts(edits[777]) and not any("A" in t for t in texts(edits[777]))
    assert any("A" in t for t in texts(edits[TG_OWNER]))
    # the «✏️» notice: a reply to each recipient's own card, remembered for reply-binding
    sends = [p for m, p in env.tg.calls[calls0:] if m == "sendMessage"]
    assert {p["chat_id"]: p["reply_to_message_id"] for p in sends} == \
        {int(c_): ids["card_id"] for c_, ids in chats.items()}
    after = sa.load_batch("b-multi")["items"][0]["rounds"][-1]["telegram"]["chats"]
    for c_, ids in after.items():
        assert any(p["chat_id"] == int(c_) for p in sends) and len(ids["notice_ids"]) >= 1


def test_edit_telegram_notice_is_remembered_as_notice_of_the_card(env):
    c, _key, _ts, card = _sent_for_edit(env)
    calls0 = len(env.tg.calls)
    sa.batch_edit(c, sa.load_batch("b-edit"), "TASK-201", {"detail": "новая деталь"})
    [send] = [p for m, p in env.tg.calls[calls0:] if m == "sendMessage"]
    assert send["reply_to_message_id"] == card and send["text"].startswith("TASK-201 · ✏️ карточка обновлена")
    notice_ids = sa.load_batch("b-edit")["items"][0]["rounds"][-1]["telegram"]["notice_ids"]
    assert len(notice_ids) == 1


def _long_sent(env, name="b-legacy"):
    c = env.ctx()
    spec = json.loads(json.dumps(EDIT_SPEC))
    spec["items"][0]["payload"] = "длинная строка текста\n" * 600
    return c, sa.batch_send(c, spec, name)


def _strip_body_markers(name, round_=None):
    st = sa.load_batch(name)
    item = st["items"][0]
    rnd = item["rounds"][-1]
    for key in ("body_parts", "body_start"):
        rnd["slack"].pop(key, None)
    rnd["telegram"].pop("body_ids", None)
    if round_:
        item["round"] = round_
    sa.save_batch(st)


def test_edit_legacy_state_round_1_rewrites_every_text_part(env):
    c, st = _long_sent(env)
    slack = st["items"][0]["rounds"][0]["slack"]
    parts, tg_parts_ = list(slack["body_parts"]), list(st["items"][0]["rounds"][0]["telegram"]["body_ids"])
    _strip_body_markers("b-legacy")
    calls0 = len(env.tg.calls)
    sa.batch_edit(c, sa.load_batch("b-legacy"), "TASK-201", {"payload": "коротко"})
    for ts in parts:
        assert "заменена" in env.ws.find("DBOT", ts)["text"]
    edited = {p["message_id"]: p["text"] for m, p in env.tg.calls[calls0:] if m == "editMessageText"}
    assert "коротко" in edited[tg_parts_[0]] and all("заменена" in edited[mid] for mid in tg_parts_[1:])


def test_edit_legacy_state_later_round_redraws_only_the_card(env):
    c, st = _long_sent(env, "b-legacy2")
    slack = st["items"][0]["rounds"][0]["slack"]
    parts, tg_body = list(slack["body_parts"]), list(st["items"][0]["rounds"][0]["telegram"]["body_ids"])
    ts, card = slack["ts"], st["items"][0]["rounds"][0]["telegram"]["card_id"]
    _strip_body_markers("b-legacy2", round_=2)
    posts0, calls0, updates0 = sum(len(v) for v in env.ws.messages.values()), len(env.tg.calls), len(env.ws.chat_updates)
    sa.batch_edit(c, sa.load_batch("b-legacy2"), "TASK-201", {"payload": "коротко"})
    assert [u[1] for u in env.ws.chat_updates[updates0:]].count(ts) == 1
    assert not any(u[1] in parts for u in env.ws.chat_updates[updates0:])
    assert sum(len(v) for v in env.ws.messages.values()) == posts0 + 1  # only the «✏️» line
    edited = [p["message_id"] for m, p in env.tg.calls[calls0:] if m == "editMessageText"]
    assert edited == [card] and not set(edited) & set(tg_body)


def test_edit_retry_after_failed_redraw_rewrites_the_text_parts_too(env, monkeypatch):
    c, st = _long_sent(env, "b-retry")
    parts = list(st["items"][0]["rounds"][0]["slack"]["body_parts"])
    real = Workspace.update
    monkeypatch.setattr(Workspace, "update", lambda self, *a, **k: (_ for _ in ()).throw(FakeSlackError("ratelimited", 429)))
    with pytest.raises(RuntimeError, match="Slack"):
        sa.batch_edit(c, sa.load_batch("b-retry"), "TASK-201", {"payload": "коротко"})
    assert all("заменена" not in env.ws.find("DBOT", ts)["text"] for ts in parts)
    monkeypatch.setattr(Workspace, "update", real)
    sa.batch_edit(c, sa.load_batch("b-retry"), "TASK-201", {"payload": "коротко"})
    assert all("заменена" in env.ws.find("DBOT", ts)["text"] for ts in parts)


# --- delivery to a live agent session (2026-11-20) --------------------------------------------------------

class FakeRunner:
    """The system seam: records every `run` (tmux, ps) and `start` (claude --bg --resume), answers from tables."""

    def __init__(self, panes: str = "", parents: dict | None = None, start_result=(0, "")) -> None:
        self.panes, self.parents, self.start_result = panes, parents or {}, start_result
        self.runs: list[list[str]] = []
        self.starts: list[dict] = []
        self.tmux_fail = False

    def run(self, argv, timeout=10):
        self.runs.append(list(argv))
        if argv[0] == "tmux" and argv[1] == "list-panes":
            return 0, self.panes
        if argv[0] == "tmux":
            return (1, "no server") if self.tmux_fail else (0, "")
        if argv[0] == "ps":
            return 0, f"{self.parents.get(int(argv[-1]), 1)}\n"
        return 127, ""

    def start(self, argv, log, exit_file, cwd=None):
        handle = {"pid": 1, "log": str(log), "exit_file": str(exit_file), "argv": argv, "cwd": cwd}
        self.starts.append(handle)
        return handle

    def poll(self, handle):
        return self.start_result


SESSION = "988096f9-0000-0000-0000-000000000000"


@pytest.fixture
def agent(env, monkeypatch):
    """A batch whose agent session lives on this host; ~/.claude is a tmp dir."""
    home = env.tmp_path / "claude"
    (home / "sessions").mkdir(parents=True)
    monkeypatch.setattr(sa, "CLAUDE_HOME", home)
    monkeypatch.setattr(sa.socket, "gethostname", lambda: "rdsh2")
    alive = {"pids": set()}
    monkeypatch.setattr(sa, "_pid_alive", lambda pid: pid in alive["pids"])

    def session_file(pid: int, cwd: str | None = "/work/project", live: bool = True) -> None:
        data = {"pid": pid, "sessionId": SESSION}
        if cwd:
            data["cwd"] = cwd
        (home / "sessions" / f"{pid}.json").write_text(json.dumps(data), encoding="utf-8")
        if live:
            alive["pids"].add(pid)

    def make(name: str, runner: FakeRunner, options=("да", "нет")):
        c = env.ctx()
        c.runner = runner
        st = sa.batch_send(c, {"title": "T", "channels": {"mode": "slack-first", "escalate_after": 0},
                               "approve_only": True,
                               "items": [{"id": "TASK-1", "title": "Что делаем?", "payload": "Текст",
                                          "options": list(options)}]}, name)
        st["origin"] = {"session_id": SESSION, "cwd": "/home/user", "host": "rdsh2"}
        sa.save_batch(st)
        return c, st

    return types.SimpleNamespace(home=home, session_file=session_file, make=make)


def _ask(c, st, text="уточни, пожалуйста"):
    item = st["items"][0]
    return sa.to_agent(c, st, item, text, "telegram:to_agent")


def test_delivery_wait_alive_goes_to_wait(env, agent):
    runner = FakeRunner()
    c, st = agent.make("d-wait", runner)
    sa.wait_heartbeat("d-wait", env.clock())
    _ask(c, st)
    assert st["to_agent"][-1]["delivered"] == "wait"
    assert runner.runs == [] and runner.starts == []


def test_delivery_live_process_gets_tmux_send_keys(env, agent):
    agent.session_file(4242)
    runner = FakeRunner(panes="100 %1\n200 %7\n", parents={4242: 150, 150: 200})
    c, st = agent.make("d-tmux", runner)
    _ask(c, st, "сделай вариант 2")
    assert st["to_agent"][-1]["delivered"] == "tmux:%7"
    sends = [r for r in runner.runs if r[:2] == ["tmux", "send-keys"]]
    assert sends[0][:5] == ["tmux", "send-keys", "-t", "%7", "-l"] and "сделай вариант 2" in sends[0][5]
    assert sends[1] == ["tmux", "send-keys", "-t", "%7", "Enter"]
    assert runner.starts == []  # no copy of a live session


def test_delivery_no_process_resumes_in_project_cwd_from_sessions_file(env, agent):
    agent.session_file(4243, cwd="/work/project", live=False)
    runner = FakeRunner()
    c, st = agent.make("d-resume", runner)
    _ask(c, st)
    assert st["to_agent"][-1]["delivered"] == "resume"
    assert runner.starts[0]["cwd"] == "/work/project"  # not origin.cwd=/home/user
    assert runner.starts[0]["argv"][1:4] == ["--bg", "--resume", SESSION]


def test_delivery_no_process_cwd_from_transcript(env, agent):
    proj = agent.home / "projects" / "-work-project"
    proj.mkdir(parents=True)
    (proj / f"{SESSION}.jsonl").write_text('{"type":"summary"}\n{"type":"user","cwd":"/work/from-transcript"}\n',
                                          encoding="utf-8")
    runner = FakeRunner()
    c, st = agent.make("d-transcript", runner)
    _ask(c, st)
    assert runner.starts[0]["cwd"] == "/work/from-transcript"


def test_delivery_resume_exit_1_is_failed_and_owner_is_told(env, agent):
    agent.session_file(4244, live=False)
    runner = FakeRunner(start_result=(1, "Workspace not trusted"))
    c, st = agent.make("d-fail", runner)
    _ask(c, st)
    sa.poll_slack(c, st)
    note = st["to_agent"][-1]
    assert note["delivered"].startswith("failed:") and "Workspace not trusted" in note["delivered"]
    told = [p["text"] for m, p in env.tg.calls if m == "sendMessage" and "не доставил агенту" in p.get("text", "")]
    assert len(told) == 1 and "Workspace not trusted" in told[0]


def test_delivery_resume_copy_of_live_session_is_failed(env, agent):
    agent.session_file(4245, live=False)
    runner = FakeRunner(start_result=(0, "note: session 988096f9 is open in another Claude Code process, so this started a copy"))
    c, st = agent.make("d-copy", runner)
    _ask(c, st)
    sa.poll_slack(c, st)
    assert st["to_agent"][-1]["delivered"].startswith("failed:")


def test_delivery_tmux_error_is_failed(env, agent):
    agent.session_file(4246)
    runner = FakeRunner(panes="300 %2\n", parents={4246: 300})
    runner.tmux_fail = True
    c, st = agent.make("d-tmuxfail", runner)
    # list-panes succeeds in the fake; make send-keys fail
    reply = _ask(c, st)
    assert st["to_agent"][-1]["delivered"].startswith("failed:") and "не доставил агенту" in reply


# --- thread after decision (B) ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["cancelled", "done"])
def test_owner_reply_in_thread_of_closed_item_goes_to_agent(env, agent, status):
    runner = FakeRunner()
    c, st = agent.make(f"b-{status}", runner)
    item = st["items"][0]
    copy_ts = item["rounds"][0]["slack"]["copy_ts"]
    item["status"] = status
    sa.journal(st, item, status, via="test")
    env.ws.post(OWNER, "DSELF", "эта задача не относится к эпику комбо", thread_ts=copy_ts)
    sa.poll_slack_batches(c, {st["name"]: st})
    assert [n["via"] for n in st["to_agent"]] == ["slack:after_decision"]
    assert st["to_agent"][0]["text"] == "после решения: эта задача не относится к эпику комбо"
    assert item["status"] == status and item.get("choice") is None


def test_thread_of_closed_item_is_not_polled_after_72h(env, agent, monkeypatch):
    c, st = agent.make("b-late", FakeRunner())
    item = st["items"][0]
    copy_ts = item["rounds"][0]["slack"]["copy_ts"]
    item["status"] = "cancelled"
    sa.journal(st, item, "cancelled", via="test")
    real = sa.time.time()
    c.clock = lambda: real + 73 * 3600
    env.ws.post(OWNER, "DSELF", "поздно", thread_ts=copy_ts)
    assert sa.slack_groups(c, {st["name"]: st}) == {}
    sa.poll_slack_batches(c, {st["name"]: st})
    assert st.get("to_agent") in (None, [])
    c.clock = lambda: real + 71 * 3600
    assert sa.slack_groups(c, {st["name"]: st}) != {}


# --- question anywhere (C) and thread decision delivery (D) ------------------------------------------------

def test_question_mark_in_the_middle_goes_to_agent(env, agent):
    c, st = agent.make("c-mid", FakeRunner())
    copy_ts = st["items"][0]["rounds"][0]["slack"]["copy_ts"]
    text = "не понял почему нельзя почистить меню…? или не поломаное? можно нового партнера или новую точку просто"
    env.ws.post(OWNER, "DSELF", text, thread_ts=copy_ts)
    sa.poll_slack_batches(c, {st["name"]: st})
    item = st["items"][0]
    assert item["status"] in sa.OPEN and item.get("choice") is None
    assert [n["via"] for n in st["to_agent"]] == ["slack:thread-question"]


def test_choice_token_with_question_mark_is_still_a_choice(env, agent):
    c, st = agent.make("c-two", FakeRunner(), options=("да", "нет"))
    copy_ts = st["items"][0]["rounds"][0]["slack"]["copy_ts"]
    env.ws.post(OWNER, "DSELF", "2, но почему так дорого?", thread_ts=copy_ts)
    sa.poll_slack_batches(c, {st["name"]: st})
    assert st["items"][0]["choice"] == 2


def test_thread_decision_is_delivered_when_wait_is_not_alive(env, agent):
    agent.session_file(4247, cwd="/work/project", live=False)
    runner = FakeRunner()
    c, st = agent.make("d-decision", runner)
    copy_ts = st["items"][0]["rounds"][0]["slack"]["copy_ts"]
    env.ws.post(OWNER, "DSELF", "2", thread_ts=copy_ts)
    sa.poll_slack_batches(c, {st["name"]: st})
    assert st["items"][0]["choice"] == 2
    [rec] = st["deliveries"]
    assert rec["delivered"] == "resume" and "решение владельца по TASK-1: нет" in rec["text"]
    assert runner.starts[0]["cwd"] == "/work/project"


def test_thread_decision_not_delivered_when_wait_alive(env, agent):
    runner = FakeRunner()
    c, st = agent.make("d-decision-wait", runner)
    sa.wait_heartbeat("d-decision-wait", env.clock())
    copy_ts = st["items"][0]["rounds"][0]["slack"]["copy_ts"]
    env.ws.post(OWNER, "DSELF", "1", thread_ts=copy_ts)
    sa.poll_slack_batches(c, {st["name"]: st})
    assert st["items"][0]["choice"] == 1 and not st.get("deliveries") and runner.starts == []


# --- acceptance r1: the remaining branches --------------------------------------------------------------

def test_live_process_outside_tmux_is_failed_not_resumed_and_owner_is_told(env, agent):
    agent.session_file(5001)
    runner = FakeRunner(panes="900 %9\n", parents={5001: 1})  # no ancestor of the process is a pane
    c, st = agent.make("r1-notmux", runner)
    reply = _ask(c, st)
    assert st["to_agent"][-1]["delivered"].startswith("failed:") and "не в tmux" in st["to_agent"][-1]["delivered"]
    assert runner.starts == [] and not [r for r in runner.runs if r[:2] == ["tmux", "send-keys"]]
    assert "не доставил агенту" in reply
    assert any(m == "sendMessage" and "не доставил агенту" in p.get("text", "") for m, p in env.tg.calls)


def test_no_process_and_no_cwd_is_failed(env, agent):
    agent.session_file(5002, cwd=None, live=False)  # sessions file without cwd, no transcript
    runner = FakeRunner()
    c, st = agent.make("r1-nocwd", runner)
    _ask(c, st)
    assert st["to_agent"][-1]["delivered"].startswith("failed:каталог проекта")
    assert runner.starts == []


def test_resume_start_oserror_is_failed(env, agent):
    agent.session_file(5003, live=False)

    class Boom(FakeRunner):
        def start(self, argv, log, exit_file, cwd=None):
            raise OSError("cwd gone")

    c, st = agent.make("r1-oserror", Boom())
    _ask(c, st)
    assert st["to_agent"][-1]["delivered"] == "failed:resume не запустился: cwd gone"


def test_thread_decision_of_session_on_another_host_is_not_delivered(env, agent):
    agent.session_file(5004, live=False)
    runner = FakeRunner()
    c, st = agent.make("r1-host", runner)
    st["origin"]["host"] = "other-host"
    copy_ts = st["items"][0]["rounds"][0]["slack"]["copy_ts"]
    env.ws.post(OWNER, "DSELF", "2", thread_ts=copy_ts)
    sa.poll_slack_batches(c, {st["name"]: st})
    assert st["items"][0]["choice"] == 2
    assert not st.get("deliveries") and runner.starts == [] and runner.runs == []


def test_thread_decision_delivery_failure_tells_the_owner(env, agent):
    agent.session_file(5005, cwd=None, live=False)  # nowhere to resume
    c, st = agent.make("r1-decfail", FakeRunner())
    copy_ts = st["items"][0]["rounds"][0]["slack"]["copy_ts"]
    env.ws.post(OWNER, "DSELF", "1", thread_ts=copy_ts)
    sa.poll_slack_batches(c, {st["name"]: st})
    [rec] = st["deliveries"]
    assert rec["delivered"].startswith("failed:")
    told = [p["text"] for m, p in env.tg.calls if m == "sendMessage" and "не доставил агенту" in p.get("text", "")]
    assert len(told) == 1


def test_in_after_window_takes_only_terminal_items(env, agent):
    c, st = agent.make("r1-window", FakeRunner())
    item = st["items"][0]
    sa.journal(st, item, "pending", via="test")  # a fresh `last` on an open item
    assert item["status"] in sa.OPEN and not sa.in_after_window(c, item)
    item["status"] = "expired"
    assert sa.in_after_window(c, item)


def test_open_item_thread_is_read_once_per_poll(env, agent):
    c, st = agent.make("r1-once", FakeRunner())
    item = st["items"][0]
    sa.journal(st, item, "pending", via="test")
    calls = []
    real = c.usr.conversations_replies
    c.usr.conversations_replies = lambda **kw: (calls.append(kw), real(**kw))[1]
    copy_ts = item["rounds"][0]["slack"]["copy_ts"]
    env.ws.post(OWNER, "DSELF", "что именно?", thread_ts=copy_ts)
    sa.poll_slack(c, st)
    assert len(calls) == 1


def test_after_decision_flag_is_saved_and_duplicate_does_not_mark_foreign_note(env, agent):
    c, st = agent.make("r1-flag", FakeRunner())
    item = st["items"][0]
    sa.to_agent(c, st, item, "после решения: да", "slack:after_decision", after_decision=True)
    assert sa.load_batch("r1-flag")["to_agent"][-1]["after_decision"] is True
    sa.to_agent(c, st, item, "ответ", "telegram:to_agent")
    sa.to_agent(c, st, item, "после решения: да", "slack:after_decision", after_decision=True)  # duplicate
    assert "after_decision" not in st["to_agent"][1] and len(st["to_agent"]) == 2
