"""Regression tests for slack_ask.py defects 1..4 (2026-11-13)."""
from __future__ import annotations

import importlib.util
import json
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
                      origin="rdsh2", clock=clock, sleep=clock.sleep)

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
