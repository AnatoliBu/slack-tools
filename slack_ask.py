#!/usr/bin/env python3
"""slack-ask — единый batch-движок вопросов и решений владельца.

Протокол:
  * вопрос приходит от бота (bender) в личку владельцу — так гарантирован пуш на телефон; бот сам ставит под
    сообщением реакции-варианты, владелец нажимает одну:
      - варианты:     1️⃣ 2️⃣ 3️⃣ …  (:one: … :nine:)
      - ок / не ок:   ✅ / ❌       (ок также 👍, :ok:, :heavy_check_mark:, :tut_gotovo:; не ок — 👎, ⛔, :no_entry_sign:)
  * каналы задаются `--channels` или `~/.config/lp-agent/owner-channels.json`: slack-first (по умолчанию) /
    telegram-first с эскалацией после 60 с, либо slack-only / telegram-only;
  * `slack_text=copy` (по умолчанию, норма LP) принимает текст в треде копии self-DM, ссылка на копию — в сообщении
    бота; `bot-thread` принимает его в треде сообщения бота через bot token; `off` — правки только в Telegram.
    В пачке иной режим — только с `slack_text_reason` (видна владельцу), а copy без рабочего токена владельца —
    отказ send (exit 7), не тихий bot-thread (владелец 2026-10-21: «а где ссылка на сообщение в личке?»);
  * ответ принят → бот пишет в треде «принято: …»; таймаут → exit 3, и агент НЕ действует (молчание ≠ разрешение);
  * первая строка КАЖДОГО сообщения (и превью пуша) — id: `--id` или ведущий `[A-Z]+-\\d+` вопроса; происхождение
    (хост, сессия) — мелко в конце (решение владельца 2026-10-13: «все начинаются одинаково — путаюсь»).

Токены: бот — SLACK_BOT_TOKEN из ~/.config/lp-agent/slack-bot.env; пользователь — SLACK_XOXC/SLACK_XOXD из .env рядом.

Выход: stdout key=value (или --json); exit 0 — ответ получен, 3 — таймаут, 2 — ошибка аргументов/Slack.
  slack-ask "Залить PLAT-4802 на test6? Последняя заливка 40 мин назад: a.ivanov, do-menu" --yesno
  slack-ask "Что делать с test6?" --id TASK-901 -o "Заливай" -o "Подожди 30 мин" -o "Не заливай" --timeout 1800

Одиночный вопрос и пачка используют один batch state, журнал и цикл watch. Одиночный режим — пачка из одного.
  slack-ask batch send <spec.json> --batch NAME    каждый элемент — своё сообщение: ✅/❌ + правки текстом
  slack-ask batch watch [--batch NAME]             ОДИН процесс на бота: Slack + Telegram по всем элементам
  slack-ask batch show --batch NAME                таблица элементов, статусов и результатов
  slack-ask batch cancel --batch NAME [--item ID] [--reason "…"]  снять: кнопки у всех убираются, причина — каждому
  slack-ask batch resolve --batch NAME --item ID (--same "<итог>" | --dispute)  вердикт агента по REVIEW из wait
Формат spec и хуков — docstring batch_send() ниже и owner-decisions/skill.md §6b. Slack API-клиент блокирует
вызовы вне direct message владельца до обращения к сети. send: 0 · 6 spec не готов (ничего не отправлено: нет
on_approve/approve_only, after не на ранний элемент, не-copy без причины, recipients не при telegram-only или у
элемента с хуком действия…) · 7 копия для правок невозможна.
  slack-ask batch send <spec.json> --batch NAME --channels telegram-only --recipients-from <repo>/.owner-decisions.json
                                                   решают несколько человек в Telegram (правила ask_owner)
"""
from __future__ import annotations

import argparse
import datetime
import difflib
import hashlib
import html
import json
import os
import random
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

from slack_sdk import WebClient

HERE = Path(__file__).resolve().parent
NUM = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
NUM_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]
CHOICE_LATIN = "ABCDEFGHI"
CHOICE_CYRILLIC = "абвгдежзи"  # owner decision labels; ё and й are intentionally omitted
YES = {"white_check_mark", "heavy_check_mark", "+1", "thumbsup", "ok", "tut_gotovo"}
NO = {"x", "-1", "thumbsdown", "no_entry", "no_entry_sign", "negative_squared_cross_mark"}
ID_RE = re.compile(r"^\s*([A-Z]+-\d+)\b")


def read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def interpret(reactions: list[dict], owner: str, n_options: int, yesno: bool) -> tuple[str, str] | None:
    """Первая (по порядку Slack) реакция владельца из допустимого набора → (answer, emoji). Чужие и бот — игнор."""
    for r in reactions:
        if owner not in r.get("users", []):
            continue
        name = r.get("name", "").split("::")[0]  # :+1::skin-tone-2: → +1
        if name in NO:
            return "not_ok", name
        if yesno and name in YES:
            return "ok", name
        if not yesno and name in NUM[:n_options]:
            return str(NUM.index(name) + 1), name
    return None


def text_choice(text: str, n_options: int) -> int | None:
    """Return a numbered/alphabetic option label found as a standalone token, else None."""
    for i, emoji in enumerate(NUM_EMOJI[:n_options], 1):
        if emoji in text:
            return i
    match = re.search(r"(?i)\bвариант\s+([a-iа-и])\b", text)
    if match:
        letter = match.group(1).upper()
        order = CHOICE_LATIN.find(letter) + 1 if letter in CHOICE_LATIN else CHOICE_CYRILLIC.find(letter.lower()) + 1
        return order if 1 <= order <= n_options else None
    for match in re.finditer(r"(?<!\w)([1-9]|[A-IА-И])(?!\w)", text):
        token = match.group(1)
        if token.isdigit():
            choice = int(token)
        elif token.isascii():
            choice = CHOICE_LATIN.index(token) + 1
        else:
            choice = CHOICE_CYRILLIC.find(token.lower()) + 1
        if 1 <= choice <= n_options:
            return choice
    return None


def split_id(question: str, qid: str | None) -> tuple[str | None, str]:
    """`--id` or a leading `[A-Z]+-\\d+` of the question → (id, question without that leading id)."""
    if qid:
        body = question[len(qid):].lstrip(" ·:—-") if question.startswith(qid) else question
        return qid, body
    m = ID_RE.match(question)
    if m:
        return m.group(1), question[m.end():].lstrip(" ·:—-")
    return None, question


def question_id(question: str, qid: str | None, environ=None) -> str | None:
    """--id, else the question's own leading id, else LP_TASK_ID (cx sets it for its executors), else none."""
    if qid:
        return qid
    match = ID_RE.match(question)
    if match:
        return match.group(1)
    return (os.environ if environ is None else environ).get("LP_TASK_ID") or None


ASK_OWNER = Path(os.environ.get("ASK_OWNER_PY", Path.home() / ".claude/skills/owner-decisions/scripts/ask_owner.py"))


def user_client(env: dict[str, str]):
    """The owner's own client (xoxc + xoxd cookie) — for the copy in his self-DM; None without tokens."""
    if not (env.get("SLACK_XOXC") and env.get("SLACK_XOXD")):
        return None
    return WebClient(token=env["SLACK_XOXC"], headers={"Cookie": "d=" + env["SLACK_XOXD"]})


AUTH_ERRORS = {"invalid_auth", "token_revoked", "not_authed", "account_inactive"}


def slack_error_code(err: Exception) -> str | None:
    response = getattr(err, "response", None)
    try:
        return response.get("error")
    except Exception:
        return None


def is_auth_error(err: Exception) -> bool:
    return slack_error_code(err) in AUTH_ERRORS


class BotAuthFailure(RuntimeError):
    """The bot token is invalid; the watch process must exit with code 5."""


class SlackRetryExhausted(RuntimeError):
    """One Slack polling cycle exhausted transient retries and should be skipped."""


def auth_notification_once(token: str, state_path: Path, notify) -> bool:
    """Persist a token fingerprint before attempting its one fail-loud notification."""
    fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if state.get("invalid_token_sha256") == fingerprint:
        return False
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp = state_path.with_suffix(state_path.suffix + ".tmp")
    temp.write_text(json.dumps({"invalid_token_sha256": fingerprint, "notified_at": now_iso()}), encoding="utf-8")
    os.replace(temp, state_path)
    try:
        notify()
    except Exception as err:
        warn(f"Slack auth notification failed: {err}")
    return True


def auth_test_bot(client, token: str, state_path: Path, notify) -> dict:
    """Fail loudly on invalid bot auth, deduplicating the notification across timer restarts."""
    try:
        result = client.auth_test()
    except Exception as err:
        if not is_auth_error(err):
            raise
        auth_notification_once(token, state_path, notify)
        raise BotAuthFailure("Slack bot authorization failed") from err
    try:
        state_path.unlink()
    except FileNotFoundError:
        pass
    return result


def _slack_status(err: Exception) -> int | None:
    response = getattr(err, "response", None)
    status = getattr(response, "status_code", None)
    if status is None and isinstance(response, dict):
        status = response.get("status_code")
    return status or getattr(err, "status_code", None)


def _retry_after(err: Exception) -> float | None:
    response = getattr(err, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None and isinstance(response, dict):
        headers = response.get("headers")
    try:
        return max(0.0, float(headers.get("Retry-After"))) if headers else None
    except (TypeError, ValueError):
        return None


def _slack_transient(err: Exception) -> bool:
    status = _slack_status(err)
    return (isinstance(err, (OSError, TimeoutError)) or status == 429 or
            status is not None and 500 <= int(status) <= 599)


def slack_retry(call, *, sleep=time.sleep, jitter=random.random, attempts: int = 4):
    """Retry Slack network/5xx/429 failures with exponential jitter and Retry-After floor."""
    for attempt in range(attempts):
        try:
            return call()
        except Exception as err:
            if is_auth_error(err):
                raise
            if not _slack_transient(err):
                raise
            if attempt + 1 >= attempts:
                raise SlackRetryExhausted(str(err)) from err
            base = min(60.0, float(2 ** attempt))
            delay = base + base * min(1.0, max(0.0, float(jitter())))
            delay = max(delay, _retry_after(err) or 0.0)
            sleep(delay)
    raise SlackRetryExhausted("unreachable")


DEFAULT_TIMEOUT_S = 8 * 3600


def main(argv: list[str] | None = None) -> int:
    """Compatibility CLI: one question is a one-item batch using the batch sender and watcher."""
    ap = argparse.ArgumentParser(prog="slack-ask", description=(__doc__ or "").split("\n")[0])
    ap.add_argument("question")
    ap.add_argument("-o", "--option", action="append", default=[], help="вариант ответа (2..9), порядок = цифра")
    ap.add_argument("--yesno", action="store_true", help="вопрос ок / не ок")
    ap.add_argument("--notify", action="store_true", help="уведомление без ожидания ответа")
    ap.add_argument("--id", dest="qid", help="id вопроса (TASK-901, BUG-304)")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    ap.add_argument("--card-timeout", type=int, default=None,
                    help="срок жизни самой карточки в секундах (по умолчанию ≥ 8 ч; выход ожидающего по --timeout карточку не гасит)")
    ap.add_argument("--channels", choices=sorted(CHANNEL_MODES))
    ap.add_argument("--slack-text", choices=sorted(SLACK_TEXT_MODES), default="copy")
    ap.add_argument("--telegram-after", type=int)
    ap.add_argument("--poll", type=int, default=10)
    ap.add_argument("--owner", default=os.environ.get("SLACK_ASK_OWNER", "U034A9RE6RF"))
    ap.add_argument("--origin", default=os.environ.get("SLACK_ASK_ORIGIN", socket.gethostname()))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.notify and (a.yesno == bool(a.option)):
        ap.error("нужно либо --yesno, либо 2..9 вариантов -o, либо --notify")
    if a.notify and (a.yesno or a.option):
        ap.error("--notify — без вариантов и без --yesno")
    if a.option and (not 2 <= len(a.option) <= 9 or any(not x.strip() for x in a.option)):
        ap.error("вариантов должно быть 2..9 непустых строк")
    if not a.notify:
        minimum = int(os.environ.get("SLACK_ASK_MIN_TIMEOUT", "1800"))
        if a.timeout < minimum:
            print(f"slack-ask: --timeout {a.timeout} < минимума {minimum} — жду {minimum} с", file=sys.stderr)
            a.timeout = minimum
    try:
        channels = channel_settings(a.channels,
            a.telegram_after if a.telegram_after is not None else int(os.environ.get("SLACK_ASK_TELEGRAM_AFTER", "60")))
    except ValueError as err:
        ap.error(str(err))
    qid = question_id(a.question, a.qid)
    name = f"ask-{int(time.time())}-{random.randrange(1_000_000):06d}"
    item_id = qid or f"ASK-{int(time.time())}"
    body = split_id(a.question, qid)[1]
    first_line = (body.splitlines() or ["Вопрос"])[0]
    item = {"id": item_id, "title": first_line[:100], "payload": a.question, "approve_only": True}
    if a.yesno:
        item["yesno"] = True
    if a.notify:
        item["notify_only"] = True
    if a.option:
        item["options"] = a.option
    else:
        item["labels"] = {"approve": "ок", "reject": "не ок"}
    # Keep the historical three channel choices, now represented by the same batch transport.
    # Card lifetime is at least DEFAULT_TIMEOUT_S (8h) unless --card-timeout is explicitly set, so a shorter CLI wait
    # (--timeout) never expires the card before the owner can answer.
    card_timeout = a.card_timeout if a.card_timeout is not None else max(DEFAULT_TIMEOUT_S, a.timeout)
    spec = {"title": first_line[:100], "channels": channels, "timeout": card_timeout,
            "slack_text": a.slack_text, "slack_text_reason": "standalone compatibility mode" if a.slack_text != "copy" else None,
            "approve_only": True, "items": [item]}
    args = argparse.Namespace(owner=a.owner, origin=a.origin, slack_text=a.slack_text)
    try:
        ctx = make_ctx(args, telegram=channels["mode"] != "slack-only")
    except BotAuthFailure as err:
        warn(str(err))
        return 5
    try:
        state = batch_send(ctx, spec, name)
    except CopyUnavailable as err:
        print(f"slack-ask: {err}", file=sys.stderr)
        return 7
    except ValueError as err:
        print(f"slack-ask: {err}", file=sys.stderr)
        return 6
    sent_item = state["items"][0]
    permalink_out = sent_item["rounds"][0]["slack"].get("permalink")
    if a.notify:
        sent_item["status"] = "done"
        save_batch(state)
        result = {"answer": "sent", "via": "slack", "permalink": permalink_out, "waited_s": 0}
        print(json.dumps(result, ensure_ascii=False) if a.json else "\n".join(f"{k}={v}" for k, v in result.items() if v is not None))
        return 0

    started = time.time()
    consumer_busy = bool(ctx.tg and _ask_owner().consumer_busy())
    if consumer_busy:
        # The systemd watcher owns getUpdates; it also watches this batch after send.
        deadline = started + a.timeout
        while time.time() < deadline:
            current = load_batch(name)["items"][0]
            if not is_live(load_batch(name)):
                break
            time.sleep(max(1, a.poll))
        else:
            current = load_batch(name)["items"][0]
    else:
        code = batch_watch(ctx, names=[name], timeout=a.timeout, slack_poll=0, tg_poll=max(1, a.poll))
        current = load_batch(name)["items"][0]
        if code == 4:
            # A watcher may have claimed the lock between the check and startup.
            deadline = started + a.timeout
            while time.time() < deadline and is_live(load_batch(name)):
                time.sleep(max(1, a.poll))
            current = load_batch(name)["items"][0]
    waited = int(time.time() - started)
    latest_state = load_batch(name)
    current = latest_state["items"][0]
    if current.get("rounds"):
        permalink_out = (current["rounds"][-1]["slack"].get("permalink")
                         or ((current.get("result") or {}).get("permalink") if isinstance(current.get("result"), dict) else None)
                         or permalink_out)
    if latest_state.get("owner_auth_failed") and current["status"] in OPEN + BUSY + WAITING + ("pending",):
        print("slack-ask: пользовательский Slack-токен отклонён; чтение копии невозможно", file=sys.stderr)
        return 7
    choice = current.get("choice")
    decision = (current.get("last") or {}).get("decision")
    if current["status"] in OPEN + BUSY + WAITING + ("expired",) or current["status"] == "pending":
        out = {"answer": "timeout", "permalink": permalink_out, "waited_s": waited}
        print(json.dumps(out, ensure_ascii=False) if a.json else "\n".join(f"{k}={v}" for k, v in out.items()))
        return 3
    if choice is not None:
        answer = "text" if choice == 0 else str(choice)
        label = current.get("choice_label", "")
    elif decision in ("approve", "done"):
        answer, label = "ok", "ок"
    else:
        answer, label = "no", "не ок"
    out = {"answer": answer, "label": label, "via": (current.get("last") or {}).get("via", "batch"),
           "permalink": permalink_out, "waited_s": waited}
    print(json.dumps(out, ensure_ascii=False) if a.json else "\n".join(f"{k}={v}" for k, v in out.items()))
    return 0


WORKSPACE = "https://loyaltyplant.slack.com"
APPROVAL_MAX_AGE_S = 24 * 3600


def permalink(ch: str, ts: str) -> str:
    return f"{WORKSPACE}/archives/{ch}/p{ts.replace('.', '')}"


def parse_link(url: str) -> tuple[str, str]:
    """https://…/archives/<C>/p1791799757221449[?thread_ts=…] → (C, 1791799757.221449)."""
    m = re.search(r"/archives/([A-Z0-9]+)/p(\d{10})(\d{6})", url)
    if not m:
        raise ValueError(f"не ссылка на сообщение Slack: {url}")
    return m.group(1), f"{m.group(2)}.{m.group(3)}"


ID_PREFIX_RE = re.compile(r"^\*?[A-Z]+-\d+\*? · ")


def approval_problems(question: dict, replies: list[dict], bot_user: str, owner: str, key: str, now: float,
                      require: tuple[str, ...] = ()) -> list[str]:
    """Пусто = ссылка доказывает ✅ владельца на вопрос бота про `key`. Чистая функция — под тестом."""
    probs = []
    if question.get("user") != bot_user:
        probs.append("сообщение не от бота slack-ask (вопрос владельцу задаёт только он)")
    text = question.get("text", "")
    if key not in text:
        probs.append(f"в вопросе нет ключа {key} — ссылка на вопрос про другое")
    # ✅ на «залить PLAT-4802 на test6?» — не пропуск для Jira: вопрос обязан назвать само действие (2026-10-12)
    for word in require:
        if word.lower() not in text.lower():
            probs.append(f"в вопросе нет слова «{word}» — ✅ дан на другое действие")
    # Slack хранит эмодзи шорткодами: «✅» → «:white_check_mark:», «✔» → «:heavy_check_mark:» (замер 2026-10-12)
    if "✅ — ок" not in text and ":white_check_mark: — ок" not in text:
        probs.append("вопрос не вида ок/не ок (--yesno) — одобрение из вариантов не выводится")
    if now - float(question.get("ts", "0")) > APPROVAL_MAX_AGE_S:
        probs.append("вопросу больше 24 ч — спроси заново")
    # since 2026-10-13 every reply opens with the question id («PLAT-4802 · ✔ Принято: ок»)
    bot_replies = [ID_PREFIX_RE.sub("", m.get("text", "")) for m in replies if m.get("user") == bot_user]
    confirm = [t for t in bot_replies if t.startswith(("✔ Принято", ":heavy_check_mark: Принято"))]
    reacted = interpret([r for r in question.get("reactions", []) if r.get("users") != [bot_user]], owner, 0, True)
    ok = any(c.rstrip().endswith(": ок") for c in confirm) or (not confirm and reacted is not None and reacted[0] == "ok")
    if not ok:
        probs.append("нет положительного ответа владельца (✔ Принято: ок в треде или ✅ владельца) — молчание и ❌ не пропуск")
    return probs


def _verify_item_round(name: str, state: dict, item: dict, rnd: dict, a, expected_link: str | None = None) -> int:
    try:
        question = Path(rnd["snapshot"]).read_text(encoding="utf-8")
    except OSError:
        question = ""
    # the owner sees «*<id> · <title>*» above the payload — the key and the action live there too (2026-11-05)
    title = item.get("title") or (item.get("labels") or {}).get("title", "")
    question = f"{item.get('id', '')} · {title}\n{question}"
    problems = []
    # a ✅/❌ card without options is a yes/no question whether or not --yesno set the flag (2026-11-05:
    # six approve_only «Jira <KEY>: Reopen» cards refused, the owner had to approve twice)
    if item.get("options") or not (item.get("yesno") or item.get("approve_only")):
        problems.append("вопрос не помечен как --yesno")
    if a.key not in question:
        problems.append(f"в вопросе нет ключа {a.key}")
    for word in a.require:
        if word.lower() not in question.lower():
            problems.append(f"в вопросе нет слова «{word}»")
    if time.time() - float(rnd.get("sent_epoch", 0)) > APPROVAL_MAX_AGE_S:
        problems.append("вопросу больше 24 ч — спроси заново")
    journal_path = batch_dir(name) / "journal.jsonl"
    try:
        entries = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    except (OSError, ValueError):
        entries = []
    positive = any(
        e.get("item") == item.get("id")
        and e.get("decision") == "approve"
        and (expected_link is None or e.get("approval_permalink") in (expected_link, None))
        and e.get("via", "").startswith(("slack:", "telegram:"))
        for e in entries
    )
    if not positive:
        problems.append("в журнале batch нет положительного ответа владельца")
    if problems:
        print("slack-ask verify: ОТКАЗ — " + "; ".join(problems), file=sys.stderr)
        return 1
    shown_ref = expected_link or rnd.get("slack", {}).get("permalink") or name
    print(f"slack-ask verify: ✅ владельца на {a.key} подтверждён ({shown_ref})")
    return 0


def verify(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="slack-ask verify",
                                 description="exit 0 — batch state/journal подтверждают ✅ владельца на вопрос про KEY; иначе 1")
    ap.add_argument("url")
    ap.add_argument("--key", required=True, help="Jira-ключ, который обязан быть в вопросе (PLAT-4802)")
    ap.add_argument("--owner", default=os.environ.get("SLACK_ASK_OWNER", "U034A9RE6RF"))
    ap.add_argument("--require", action="append", default=[], help="слово, обязанное быть в вопросе (действие: jira)")
    a = ap.parse_args(argv)
    ref = a.url.strip()
    batch_candidate = ref[6:] if ref.startswith("batch:") else ref
    if (batch_dir(batch_candidate) / "state.json").exists():
        try:
            state = load_batch(batch_candidate)
        except (OSError, ValueError) as err:
            print(f"slack-ask verify: {err}", file=sys.stderr)
            return 1
        if state.get("owner") != a.owner:
            print(f"slack-ask verify: владелец пачки {batch_candidate} не совпадает с {a.owner}", file=sys.stderr)
            return 1
        candidates = [it for it in state.get("items", []) if it.get("id") == a.key] or list(state.get("items", []))
        for item in candidates:
            if item.get("rounds"):
                rnd = item["rounds"][-1]
                return _verify_item_round(batch_candidate, state, item, rnd, a,
                                          expected_link=rnd.get("slack", {}).get("permalink"))
        print(f"slack-ask verify: в пачке {batch_candidate} нет отправленных раундов", file=sys.stderr)
        return 1
    tg_match = re.match(r"^telegram:(-?\d+)/(\d+)$", ref)
    if tg_match:
        chat_s, card_id = tg_match.group(1), int(tg_match.group(2))
        for name in list_batches():
            try:
                state = load_batch(name)
            except (OSError, ValueError):
                continue
            if state.get("owner") != a.owner:
                continue
            for item in state.get("items", []):
                for rnd in item.get("rounds", []):
                    tg_info = rnd.get("telegram", {})
                    chat_cards = (tg_info.get("chats") or {}).get(chat_s) or {}
                    if tg_info.get("card_id") == card_id or chat_cards.get("card_id") == card_id:
                        return _verify_item_round(name, state, item, rnd, a, expected_link=ref)
        print(f"slack-ask verify: permalink {a.url} не найден в state/journal пачек", file=sys.stderr)
        return 1
    try:
        ch, ts = parse_link(a.url)
    except ValueError as e:
        print(f"slack-ask verify: {e}", file=sys.stderr)
        return 1
    for name in list_batches():
        try:
            state = load_batch(name)
        except (OSError, ValueError):
            continue
        if state.get("owner") != a.owner:
            continue
        for item in state.get("items", []):
            for rnd in item.get("rounds", []):
                slack = rnd.get("slack", {})
                if slack.get("ts") != ts or state.get("slack_channel") != ch:
                    continue
                return _verify_item_round(name, state, item, rnd, a, expected_link=permalink(ch, ts))
    print(f"slack-ask verify: permalink {a.url} не найден в state/journal пачек", file=sys.stderr)
    return 1


# ======================================================================================================
# batch — many items at once, one poller for the bot (owner's decision 2026-10-13)
# ======================================================================================================
#
# Every item is its own message: ✅ / ❌ reactions and text corrections in Slack (thread of the item's copy
# in the owner's self-DM), the same text + «✅ / ❌ / ✍ Правки» buttons in Telegram. One `watch` process is
# the only getUpdates consumer of the Telegram bot and polls Slack for every waiting item. An answer runs
# the item's hook: on_approve (after a sha256 check of the payload), on_corrections (then a new round with a
# diff), on_reject. State, journal, round snapshots and hook logs live in $SLACK_ASK_HOME/batches/<name>/.

BATCH_HOME = Path(os.environ.get("SLACK_ASK_HOME", Path.home() / ".local/share/slack-ask"))
OPEN = ("pending", "approve_failed")   # statuses that take an owner decision
BUSY = ("approving", "correcting")     # a hook of the item is running
WAITING = ("waiting_after",)           # approved; its action waits for the item named in `after` (a thread starter)
STATUS_RU = {"pending": "ждёт ответа", "approving": "выполняется после ✅", "approve_failed": "действие не выполнено",
             "correcting": "корректор работает", "done": "выполнено", "rejected": "отклонено",
             "commented": "правки записаны", "cancelled": "снято", "expired": "истекло", "new": "не отправлено",
             "waiting_after": "одобрено, ждёт публикации зависимости", "review": "на сверке у агента",
             "instructed": "по указанию", "blocked": "не выполнено: зависимость не выполнена"}
DEAD = ("rejected", "cancelled", "expired", "blocked", "instructed")   # a dependency in these states never publishes
# The default corrector of an item without on_corrections (owner 2026-10-21: corrections must come back as a corrected
# round, never «правки записаны (корректора у элемента нет)»).
DEFAULT_CORRECTOR = Path(os.environ.get("SLACK_ASK_CORRECTOR", Path.home() / ".claude/skills/owner-decisions/scripts/correct_payload.py"))
APPROVE_ONLY_NOTE = "ℹ Только согласование: по ✅ бот ничего не публикует — публикует агент."


def approve_only_note(item: dict) -> str:
    if item.get("notify_only"):
        return "Уведомление от агента — ответа не требуется."
    if item.get("options"):
        return "ℹ Только согласование: выбор фиксирует решение — действия выполняет агент."
    return APPROVE_ONLY_NOTE


class CopyUnavailable(RuntimeError):
    """slack_text=copy, but the owner's copy cannot be made: nothing more is sent (exit 7), never a silent other mode."""
DEFAULT_LABELS = {"title": "вопрос", "approve": "ок", "reject": "не ок", "done": "Выполнено"}
HOOKS = ("on_approve", "on_corrections", "on_reject", "on_timeout")
CHANNEL_MODES = {"slack-first", "telegram-first", "slack-only", "telegram-only"}
SLACK_TEXT_MODES = {"copy", "bot-thread", "off"}
TG_LIMIT = 4096
SECTION_LIMIT = 3000


def channel_settings(mode: str | None = None, escalate_after: int | None = None, *, default="slack-first",
                     default_escalate=60) -> dict:
    """Resolve explicit settings over the shared owner-channels.json preference."""
    path = Path(os.environ.get("OWNER_CHANNELS_CONFIG", Path.home() / ".config/lp-agent/owner-channels.json"))
    configured = {}
    try:
        configured = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    except (ValueError, OSError) as err:
        raise ValueError(f"invalid channel config {path}: {err}") from err
    picked = mode if mode is not None else configured.get("mode", default)
    delay = escalate_after if escalate_after is not None else configured.get("escalate_after", default_escalate)
    if picked not in CHANNEL_MODES:
        raise ValueError(f"channels must be one of {', '.join(sorted(CHANNEL_MODES))}")
    if not isinstance(delay, int) or delay < 0:
        raise ValueError("escalate_after must be a non-negative integer")
    return {"mode": picked, "escalate_after": delay}


def _ask_owner():
    """owner-decisions/scripts/ask_owner.py as a module: Telegram client, state I/O, consumer lock."""
    if "ask_owner" not in sys.modules:
        import importlib.util
        spec = importlib.util.spec_from_file_location("ask_owner", ASK_OWNER)
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        sys.modules["ask_owner"] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return sys.modules["ask_owner"]


def _send_blocks():
    """slack-tools' send_blocks: the very note→post rendering and chunking `slack md` uses."""
    if "send_blocks" not in sys.modules:
        dirs = [os.environ.get("SLACK_TOOLS_DIR"), HERE, Path.home() / "slack-tools", Path.home() / "PythonProjects/slack-tools"]
        found = next((Path(d) for d in dirs if d and (Path(d) / "send_blocks.py").is_file()), None)
        if found is None:
            raise RuntimeError("send_blocks.py (slack-tools) not found — set SLACK_TOOLS_DIR")
        sys.path.insert(0, str(found))
    import send_blocks  # type: ignore[import-not-found]
    return send_blocks


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def warn(text: str) -> None:
    # Time first: watch.log had none, so Telegram resets could not be laid against taps and restarts (TASK-1125 p.5).
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} slack-ask batch: {text}", file=sys.stderr, flush=True)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render_payload(item: dict) -> str:
    """The text shown for review and acted upon. render=note: a note-cli note without its YAML, exactly as
    `slack md` posts it."""
    text = Path(item["payload_file"]).read_text(encoding="utf-8") if item.get("payload_file") else item.get("payload", "")
    if item.get("render") == "note":
        text = _send_blocks().note_to_post(text)[0]
    return text


def short_diff(old: str, new: str, limit: int = 2500) -> str:
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0))[2:]
    lines = [ln for ln in lines if not ln.startswith("@@")]
    plus, minus = sum(ln.startswith("+") for ln in lines), sum(ln.startswith("-") for ln in lines)
    body = "\n".join(lines)
    if len(body) > limit:
        body = body[:limit] + "\n…"
    return f"+{plus} −{minus} строк\n{body}"


# Markdown per batch message. slack-tools' 11 000 plus the item header did not fit: 2026-10-16 the 16 KB note of
# BUG-345 got `msg_blocks_too_long` and stopped the batch after two items. The rest goes on as thread continuations.
BATCH_MD_LIMIT = 4000


def split_text(text: str, limit: int) -> list[str]:
    """Split at line boundaries into parts of at most `limit` characters (a longer line is cut)."""
    parts, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) > limit:
            parts.append(cur)
            cur = ""
        cur += line
    if cur.strip():
        parts.append(cur)
    return [p.rstrip("\n") for p in parts] or [""]


def tg_parts(text: str, reserve: int) -> list[str]:
    """Raw parts whose HTML-escaped form fits one Telegram message next to `reserve` characters of markup."""
    room = TG_LIMIT - reserve

    def fit(part: str) -> list[str]:  # escaping (& < >) grows the text: halve until it fits
        if len(html.escape(part)) <= room or len(part) < 2:
            return [part]
        halves = split_text(part, len(part) // 2 + 1)
        if len(halves) < 2:
            halves = [part[: len(part) // 2], part[len(part) // 2:]]
        return [piece for half in halves for piece in fit(half)]

    return [piece for part in split_text(text, room) for piece in fit(part)]


# --- storage ----------------------------------------------------------------------------------------

def batch_dir(name: str) -> Path:
    return BATCH_HOME / "batches" / name


class Batch(dict):
    """A loaded state.json that remembers what it was when read (`_base`): `save_batch` then writes only what this
    process changed and keeps what another wrote meanwhile — `send` (14×2 cards took minutes on 2026-10-27) and
    `watch` (answering taps on the cards already sent) work on one batch at the same time."""
    _base: str | None = None


def _snapshot(state: dict) -> str:
    return json.dumps(state, sort_keys=True, ensure_ascii=False)


def load_batch(name: str) -> dict:
    state = Batch(json.loads((batch_dir(name) / "state.json").read_text(encoding="utf-8")))
    state._base = _snapshot(state)
    return state


LOCK_STALE_S = 30


class state_lock:
    """Exclusive lock file next to state.json (O_EXCL; works on Windows and Linux). Held only around
    read-merge-write, never across a network call; a lock older than LOCK_STALE_S is left by a dead process."""

    def __init__(self, path: Path):
        self.path = path.with_name(path.name + ".lock")

    def __enter__(self):
        deadline = time.time() + 2 * LOCK_STALE_S
        while True:
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > LOCK_STALE_S or time.time() > deadline:
                        self.path.unlink()
                        continue
                except FileNotFoundError:
                    continue
                time.sleep(0.01)

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def _merge_into(state: dict, base: dict, disk: dict) -> None:
    """Three-way merge IN PLACE by top-level key and by item: what this process changed since it read wins, the rest
    is taken from disk, so another process's changes survive. In place — callers (the send loop) hold the item dicts."""
    for key in (set(state) | set(disk)) - {"items"}:
        if state.get(key, KeyError) == base.get(key, KeyError):  # untouched here
            if key in disk:
                state[key] = disk[key]
            else:
                state.pop(key, None)
    bi, di = base.get("items", []), disk.get("items", [])
    for n, item in enumerate(state.get("items", [])):
        if n < len(bi) and n < len(di) and item == bi[n] and item != di[n]:
            item.clear()
            item.update(di[n])


def save_batch(state: dict) -> None:
    path = batch_dir(state["name"]) / "state.json"
    base = getattr(state, "_base", None)
    with state_lock(path):
        if base is not None and path.exists():
            _merge_into(state, json.loads(base), json.loads(path.read_text(encoding="utf-8")))
        _ask_owner().save_state(path, state)
        if isinstance(state, Batch):
            state._base = _snapshot(state)


def list_batches() -> list[str]:
    return sorted(p.parent.name for p in (BATCH_HOME / "batches").glob("*/state.json"))


def journal(state: dict, item: dict, decision: str, **fields) -> None:
    """Append-only log of every owner decision and hook outcome: what, when, where from."""
    entry = {"at": now_iso(), "batch": state["name"], "item": item["id"], "n": item["n"], "round": item["round"],
             "decision": decision, **{k: v for k, v in fields.items() if v not in (None, "")}}
    with open(batch_dir(state["name"]) / "journal.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    item["last"] = {k: entry[k] for k in ("at", "decision", "via") if k in entry}


# --- hooks ------------------------------------------------------------------------------------------

_WRAP = (
    "import os, subprocess, sys\n"
    "try:\n"
    "    code = subprocess.run(sys.argv[2:]).returncode\n"
    "except OSError as err:\n"
    "    print(f'hook did not start: {err}', flush=True)\n"
    "    code = 127\n"
    "open(sys.argv[1] + '.tmp', 'w').write(str(code))\n"
    "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
)


class Runner:
    """Starts hooks detached and reads their outcome from files: a hook outlives a restarted `watch`, and
    the exit code it leaves behind is collected by the next one."""

    def __init__(self) -> None:
        self.procs: dict[str, subprocess.Popen] = {}

    def start(self, argv: list[str], log: Path, exit_file: Path, cwd: str | None = None) -> dict:
        with open(log, "ab") as fh:
            proc = subprocess.Popen([sys.executable, "-c", _WRAP, str(exit_file), *argv], stdout=fh,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=cwd or None,
                                    start_new_session=os.name != "nt")
        self.procs[str(exit_file)] = proc
        return {"pid": proc.pid, "log": str(log), "exit_file": str(exit_file), "argv": argv, "started": now_iso()}

    def poll(self, handle: dict) -> tuple[int, str] | None:
        exit_file, log = Path(handle["exit_file"]), Path(handle["log"])
        proc = self.procs.get(str(exit_file))
        if proc is not None:
            proc.poll()
        output = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        if exit_file.exists():
            self.procs.pop(str(exit_file), None)
            return int(exit_file.read_text(encoding="utf-8").strip() or -1), output
        gone = proc.returncode is not None if proc is not None else not _pid_alive(handle.get("pid"))
        if gone:  # killed before it could write the exit file (the wrapper writes it before exiting)
            self.procs.pop(str(exit_file), None)
            return -1, output + "\n(hook process is gone without an exit code)"
        return None


def _pid_alive(pid) -> bool:
    if not pid or os.name == "nt":
        return True  # Windows: os.kill(pid, 0) would terminate the process; wait for the exit file instead
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def hook_argv(hook, values: dict) -> list[str]:
    """argv list: {name} placeholders are substituted verbatim; a string is a bash command with quoted values."""
    def sub(text: str, quote: bool) -> str:
        return re.sub(r"\{(\w+)\}", lambda m: (shlex.quote(str(values[m.group(1)])) if quote else str(values[m.group(1)]))
                      if m.group(1) in values else m.group(0), text)
    if isinstance(hook, str):
        return ["bash", "-c", sub(hook, True)]
    return [sub(str(part), False) for part in hook]


def parse_hook_output(output: str) -> dict:
    """The last JSON object line of the hook's output, else its key=value lines."""
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except ValueError:
                pass
    pairs = [ln.split("=", 1) for ln in output.splitlines() if re.match(r"^[a-z_]+=", ln)]
    return {k: v for k, v in pairs}


# --- context ----------------------------------------------------------------------------------------

class Ctx:
    """The seams: Slack bot and owner clients (slack_sdk WebClient or fakes), Telegram `call(method, **p)`,
    the hook runner and the clock."""

    def __init__(self, bot, usr=None, tg=None, chat_id=None, owner: str = "U034A9RE6RF", origin: str = "",
                 runner=None, clock=time.time, sleep=time.sleep, pace: float = 0.0):
        self.bot, self.usr = OwnerDMClient(bot, owner), OwnerDMClient(usr, owner) if usr else None
        self.tg, self.chat_id = tg, chat_id
        self.owner, self.origin = owner, origin or socket.gethostname()
        self.runner = runner or Runner()
        self.clock, self.sleep, self.pace = clock, sleep, pace
        self.bot_user = str(self.bot.auth_test()["user_id"])
        self.bot.conversations_open(users=owner)
        if self.usr:
            self.usr.conversations_open(users=owner)
        self.tg_failures = 0
        self.reload_owner_client = None
        self.auth_notifier = None
        self.owner_auth_reload_attempted = False
        self.owner_auth_failed = False
        self.owner_auth_transition_logged = False
        self.slack_retry_degraded = False


class OwnerDMClient:
    """Reject any Slack call outside the owner's DM before it can reach the network."""
    def __init__(self, client, owner: str):
        self._client, self.owner, self.channels = client, owner, set()

    def __getattr__(self, name):
        target = getattr(self._client, name)
        if not callable(target):
            return target
        def checked(*args, **kwargs):
            if name == "conversations_open":
                if str(kwargs.get("users", "")) != self.owner:
                    raise ValueError("Slack client may open only the owner's direct message")
                result = target(*args, **kwargs)
                self.channels.add(str(result["channel"]["id"]))
                return result
            channel = kwargs.get("channel")
            if channel is not None and str(channel) not in self.channels:
                raise ValueError(f"Slack client may access only the owner's direct message (channel={channel})")
            return target(*args, **kwargs)
        return checked


def tg_send(ctx: Ctx, **params) -> int:
    """sendMessage to the owner (or `chat_id=` — another recipient of the batch) with retries on 429/5xx/network;
    returns message_id."""
    ao = _ask_owner()
    chat = params.pop("chat_id", None) or ctx.chat_id
    for attempt in range(6):
        try:
            sent = ctx.tg("sendMessage", chat_id=chat, disable_web_page_preview=True, **params)
            if ctx.pace:
                ctx.sleep(ctx.pace)
            return int(sent["message_id"])
        except (ao.TelegramError, OSError) as err:
            if not ao.transient(err) or attempt == 5:
                raise
            # 1→2→4→8→16 s: a Connection reset on the rdsh2→Telegram link passes in about a second; the old 2→4→…
            # stretched a 14×2 send to minutes (2026-10-27)
            ctx.sleep(min(30, 2 ** attempt))
    raise RuntimeError("unreachable")


def tg_cosmetic(ctx: Ctx, method: str, **params):
    """Best-effort Telegram edit/markup call with retries on transient network/429/5xx errors; ignores only benign
    400s («message is not modified», «message to edit not found», «query is too old»)."""
    if not ctx.tg:
        return None
    ao = _ask_owner()
    sleep = getattr(ctx, "sleep", time.sleep)
    for attempt in range(4):
        try:
            return ctx.tg(method, **params)
        except (ao.TelegramError, OSError) as err:
            if ao.transient(err) and attempt < 3:
                sleep(min(8, 2 ** attempt))
                continue
            warn(f"{method} skipped: {err}")
            return None


# --- recipients: several people decide one batch in Telegram (the rules of ask_owner.record/resolve) ------------
#
# state["recipients"] exists only when more than one person decides: [owner, …]. A state without it (every batch
# sent before, every single-owner batch) is the owner alone; per-chat message ids live in the round's
# telegram["chats"], the owner's are also kept in the old card_id / part_ids.

def recipients_of(ctx: Ctx, state: dict) -> list[dict]:
    return state.get("recipients") or [{"chat_id": ctx.chat_id, "name": None}]


def is_multi(state: dict) -> bool:
    return len(state.get("recipients") or []) > 1


def person_name(ctx: Ctx, state: dict, chat) -> str:
    for person in recipients_of(ctx, state):
        if int(person["chat_id"]) == int(chat):
            return person.get("name") or ("владелец" if int(chat) == int(ctx.chat_id or 0) else str(chat))
    return str(chat)


def tg_cards(ctx: Ctx, rnd_telegram: dict) -> dict[int, dict]:
    """{chat: {"card_id", "part_ids", "extra_ids", "notice_ids"}} of one round (an old round: the owner's only).
    notice_ids: the other messages the bot sent about the item to that chat (argument requests, opponent's
    argument, «закрыто: …») — only so that a reply to any of them finds the question (owner 2026-10-28)."""
    if rnd_telegram.get("chats"):
        return {int(c): v for c, v in rnd_telegram["chats"].items()}
    if rnd_telegram.get("card_id") is None and not rnd_telegram.get("part_ids"):
        return {}
    return {ctx.chat_id: {"card_id": rnd_telegram.get("card_id"), "part_ids": rnd_telegram.get("part_ids", []),
                          "extra_ids": rnd_telegram.get("extra_ids", []),
                          "notice_ids": rnd_telegram.get("notice_ids", [])}}


def remember_message(state: dict, item: dict, chat, mid) -> None:
    """Keep a message the bot sent about `item` in the card map of that chat: a reply to it is bound to the question
    by reply_to_message.message_id alone (rule 4 of Р5)."""
    if mid is None or not item.get("rounds"):
        return
    telegram = item["rounds"][-1].setdefault("telegram", {})
    holder = telegram.setdefault("chats", {}).setdefault(str(chat), {}) if is_multi(state) else telegram
    ids = holder.setdefault("notice_ids", [])
    if mid not in ids:
        ids.append(mid)
        del ids[:-60]


def resolve_recipients(spec: dict, owner_chat) -> list[dict]:
    """The owner (name from spec owner_name) + spec recipients, as ask_owner.load_config builds them."""
    raw = spec.get("recipients") or []
    if not raw:
        return []
    if not isinstance(raw, list):
        raise ValueError("recipients must be a list of {chat_id, name}")
    if owner_chat is None:
        raise ValueError("spec not ready, nothing sent: recipients need Telegram (no bot config ~/.owner-decisions-bot)")
    people = [{"chat_id": int(owner_chat), "name": spec.get("owner_name")}]
    for r in raw:
        try:
            chat = int(r["chat_id"])
        except (TypeError, KeyError, ValueError):
            raise ValueError(f"spec not ready, nothing sent: recipients entry without an integer chat_id: {r!r}")
        if all(chat != p["chat_id"] for p in people):
            people.append({"chat_id": chat, "name": r.get("name")})
    return people if len(people) > 1 else []


def prefill_problems(items: list[dict], people: list[dict]) -> list[str]:
    """`prefill: [{"chat_id", "choice" (1-based) | "text", "argument"?}]` — answers counted in advance (the owner decided, a colleague
    verifies). Only with recipients, only for options items, chat among the recipients, choice in range, not everyone."""
    problems, chats = [], {p["chat_id"] for p in people}
    for it in items:
        prefill = it.get("prefill")
        if not prefill:
            continue
        iid, options = it.get("id"), it.get("options") or []
        if not people:
            problems.append(f"{iid}: prefill needs recipients (several people decide) — the owner alone just answers")
            continue
        if not isinstance(prefill, list) or not options:
            problems.append(f"{iid}: prefill must be a list and the item must have options")
            continue
        seen = set()
        for pre in prefill:
            try:
                chat = int(pre["chat_id"])
            except (TypeError, KeyError, ValueError):
                problems.append(f"{iid}: prefill entry without an integer chat_id: {pre!r}")
                continue
            if chat not in chats:
                problems.append(f"{iid}: prefill chat_id {chat} is not a recipient of the batch")
            if chat in seen:
                problems.append(f"{iid}: prefill chat_id {chat} twice")
            seen.add(chat)
            has_choice, has_text = pre.get("choice") is not None, pre.get("text") is not None
            if has_choice == has_text:
                problems.append(f"{iid}: prefill for {chat} needs exactly one of choice (1..{len(options)}) or text")
            elif has_choice and not (isinstance(pre["choice"], int) and 1 <= pre["choice"] <= len(options)):
                problems.append(f"{iid}: prefill choice {pre['choice']!r} for {chat} is outside 1..{len(options)}")
            elif has_text and not str(pre["text"]).strip():
                problems.append(f"{iid}: prefill text for {chat} is empty")
        if chats and chats <= seen:
            problems.append(f"{iid}: prefill answers every recipient — nobody is left to verify; record the decision "
                            "without a batch")
    return problems


def item_line(state: dict, item: dict) -> str:
    """First line of every message of the item — the id leads (owner's decision 2026-10-13)."""
    total, r = len(state["items"]), item["round"]
    tags = ([f"{item['n']}/{total}"] if total > 1 else []) + ([f"раунд {r}"] if r > 1 else [])
    return f"{item['id']} · {item['labels']['title']}" + (f" ({', '.join(tags)})" if tags else "")


def section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:SECTION_LIMIT]}}


def callback(state: dict, item: dict, action: str) -> str:
    return f"sa|{state['key']}|{item['n']}|{item['round']}|{action}"


def send_round(ctx: Ctx, state: dict, item: dict, text: str, diff: str = "", strict: bool = False) -> None:
    """Show the item under one Slack root and one owner copy; later rounds update the root and reply in both threads.

    strict (batch send): in copy mode a copy that could not be made raises CopyUnavailable BEFORE the bot message —
    2026-10-21 an item reached the owner without the link to its copy and he asked «а где ссылка на сообщение в
    личке?»."""
    r, lab, line = item["round"], item["labels"], item_line(state, item)
    rounds_dir = batch_dir(state["name"]) / "rounds"
    rounds_dir.mkdir(parents=True, exist_ok=True)
    snapshot = rounds_dir / f"{item['n']:02d}-r{r}.md"
    snapshot.write_text(text, encoding="utf-8")
    previous_round = item["rounds"][-1] if item["rounds"] else {}
    prior = previous_round if previous_round.get("round") == r else {}
    rnd: dict = {**prior, "round": r, "sha256": sha256(text), "snapshot": str(snapshot), "sent_at": prior.get("sent_at", now_iso()),
                 "sent_epoch": prior.get("sent_epoch", ctx.clock()),
                 "slack": {"parts": [], "seen": [], "used": [], **previous_round.get("slack", {}), **prior.get("slack", {})},
                 "telegram": prior.get("telegram", {})}
    # Preserve consumed root reactions: the same root survives later rounds. The owner can toggle
    # a reaction off and back on; polling observes its absence before treating it as a new vote.
    if previous_round.get("round") != r:
        rnd["telegram"] = {}
    s = rnd["slack"]
    is_new_root = not s.get("ts")
    detail = item.get("detail", "")
    text_mode = state.get("slack_text", "copy")
    if text_mode == "copy" and ctx.usr and state.get("copy_channel") and "slack" in state.get("initial_channels", ["slack", "telegram"]) and not s.get("copy_ts"):
        try:
            copy = ctx.usr.chat_postMessage(channel=state["copy_channel"], unfurl_links=False,
                                            text=f"{line} · копия для правок текстом — пиши их в ТРЕДЕ этого сообщения\n{detail}")
            s["copy_ts"] = str(copy["ts"])
            s["copy_link"] = str(ctx.usr.chat_getPermalink(channel=state["copy_channel"], message_ts=s["copy_ts"])["permalink"])
        except Exception as err:  # text corrections then go through Telegram only
            if strict:
                raise CopyUnavailable(f"{item['id']}: the owner's copy for text corrections was not created: {err}") from err
            warn(f"{item['id']}: copy for text answers not created: {err}")
    chunks = _send_blocks().chunk_markdown(text, BATCH_MD_LIMIT) if text.strip() else []
    head = [f"*{line}*"] + ([detail] if detail else [])
    if diff:
        head.append(f"Изменения: {diff.splitlines()[0]} — в треде")
    reason = state.get("slack_text_reason")
    why = f" (копии нет: {reason})" if reason else ""
    edit_route = (f"в треде копии: {s['copy_link']}" if s.get("copy_link") else
                  "в треде этого сообщения" + why if text_mode == "bot-thread" else "в Telegram" + why)
    options = item.get("options") or []
    if item.get("notify_only"):
        legend = "Уведомление от агента — ответа не требуется."
    elif options:
        legend = "  ".join([f"{NUM_EMOJI[i]} {opt}" for i, opt in enumerate(options)] +
                           [f"❌ — {lab['reject']}", f"✍ свой вариант — текстом {edit_route}"])
    else:
        legend = f"✅ — {lab['approve']}   ❌ — {lab['reject']}   ✍ правки — текстом {edit_route}"
    if text_mode == "off" and not item.get("notify_only"):
        legend = f"✅ — {lab['approve']}   ❌ — {lab['reject']}   Правки — только в Telegram.{why}"
    head += ["", legend] + ([approve_only_note(item)] if item.get("approve_only") else [])
    blocks = [section("\n".join(head))] + chunks[:1] + [
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"🤖 {ctx.origin} · slack-ask batch {state['name']}"}]}]
    if "slack" in state.get("initial_channels", ["slack", "telegram"]) and is_new_root:
        first = ctx.bot.chat_postMessage(channel=state["slack_channel"], text=line, blocks=blocks,
                                         unfurl_links=False, unfurl_media=False)
        s["ts"] = str(first["ts"])
        s["permalink"] = permalink(state["slack_channel"], s["ts"])
        reactions = [] if item.get("notify_only") else (NUM[:len(options)] + ["x"] if options else ["white_check_mark", "x"])
        for name in reactions:
            try:
                ctx.bot.reactions_add(channel=state["slack_channel"], timestamp=s["ts"], name=name)
            except Exception as err:
                warn(f"{item['id']}: reaction {name} not preset: {err}")
    thread = []
    if diff:
        thread.append((f"{line} · изменения раунда {r}", [section(f"*{line}* · изменения раунда {r}"),
                                                          section("```\n" + diff[:SECTION_LIMIT - 10] + "\n```")]))
    # Later rounds put the complete revised text in the root thread, even though its first chunk
    # is also shown by chat_update for the push preview.
    body_chunks = chunks if not is_new_root else chunks[1:]
    for i, block in enumerate(body_chunks, 1 if not is_new_root else 2):
        thread.append((f"{line} · продолжение {i}/{len(chunks)}", [section(f"*{line}* · продолжение {i}/{len(chunks)}"), block]))
    if s.get("ts"):
        if not is_new_root:
            ctx.bot.chat_update(channel=state["slack_channel"], ts=s["ts"], text=line, blocks=blocks,
                                unfurl_links=False, unfurl_media=False)
        if text_mode == "bot-thread":
            s["text_ts"] = s["ts"]
            s["text_channel"] = state["slack_channel"]
        for fallback, blks in thread:
            reply = ctx.bot.chat_postMessage(channel=state["slack_channel"], thread_ts=s["ts"], text=fallback, blocks=blks,
                                             unfurl_links=False, unfurl_media=False)
            s["parts"].append(str(reply["ts"]))
    if not is_new_root and text_mode == "copy" and ctx.usr and s.get("copy_ts"):
        preview = (diff.splitlines()[0] if diff else "")
        notice = f"{line} · раунд {r} готов: {preview}\n{text}"
        reply = ctx.usr.chat_postMessage(channel=state["copy_channel"], thread_ts=s["copy_ts"], text=notice,
                                         unfurl_links=False)
        # seen only: copy_latest stays where the last poll left it, otherwise an owner reply written before this
        # notice (2026-11-18 TASK-1319: 9 s before the 60 s escalation) is never read
        s["seen"].append(str(reply["ts"]))
    has_tg_round = bool(rnd["telegram"].get("card_id") or rnd["telegram"].get("chats"))
    if ctx.tg and "telegram" in state.get("initial_channels", ["slack", "telegram"]) and not has_tg_round:
        t = rnd["telegram"]
        esc = html.escape
        multi = is_multi(state)
        root = tg_cards(ctx, item["rounds"][0].get("telegram", {})) if item["rounds"] else {}
        buttons, choice_legend = item_buttons(state, item)
        markup = {"inline_keyboard": buttons}
        who = ""
        if multi:
            names = ", ".join(person_name(ctx, state, p["chat_id"]) for p in recipients_of(ctx, state))
            who = f"Решают: {esc(names)} — решение принято, когда все ответят одинаково."
            ready = [a for a in (item.get("answers") or {}).values() if a.get("prefilled")]
            who += "".join(f"\n{esc(a['by'])} уже ответил: {esc(answer_label(item, a))}"
                           + (f". Аргумент: «{esc(a['argument'])}»" if a.get("argument") else "") for a in ready)
        card = "\n".join([f"<b>{esc(line)}</b>"] + ([esc(detail)] if detail else []) + [
            "", choice_legend,
            ("✍ Свой вариант — нажми кнопку и напиши текст." if options else "Правки — «✍ Правки», затем просто напиши текст."),
            f"{TO_AGENT} — указание агенту, который прислал вопрос (не правка текста)."]
            + ([who] if who else [])
            + ([esc(approve_only_note(item))] if item.get("approve_only") else [])
            + ["", f"<i>🤖 {esc(ctx.origin)} · {esc(state['name'])}</i>"])
        # the text folds under an expandable quote (owner, 2026-10-13: «спойлеры — удобно»): the id line
        # stays visible, 30 reports do not flood the chat
        parts = tg_parts(text, len(line) + 80)
        per_chat = {}
        for person in recipients_of(ctx, state):
            chat = person["chat_id"]
            if ((item.get("answers") or {}).get(str(chat)) or {}).get("prefilled"):
                continue  # his answer is counted in advance: nothing to tap; a disagreement card still reaches him
            try:
                ids = []
                root_card = (root.get(int(chat)) or {}).get("card_id") if chat is not None else None
                reply = {"reply_to_message_id": root_card, "allow_sending_without_reply": True} if root_card else {}
                if diff:
                    ids.append(tg_send(ctx, chat_id=chat, text=f"{line} · изменения\n\n{diff}"[:TG_LIMIT], **reply))
                for i, part in enumerate(parts, 1):
                    ids.append(tg_send(ctx, chat_id=chat, parse_mode="HTML",
                                       text=f"<b>{esc(line)} · текст {i}/{len(parts)}</b>\n"
                                            f"<blockquote expandable>{esc(part)}</blockquote>", **reply))
                card_id = tg_send(ctx, chat_id=chat, text=card, parse_mode="HTML", reply_markup=markup, **reply)
                per_chat[str(chat)] = {"part_ids": ids, "card_id": card_id}
                if not multi or chat == ctx.chat_id:
                    t["part_ids"], t["card_id"] = ids, card_id
            except Exception as err:  # Slack already has the item; Telegram is the second channel
                warn(f"{item['id']}: Telegram not sent to {chat}: {err}")
                t["error"] = str(err)
        if multi:
            t["chats"] = per_chat
    if item["rounds"] and item["rounds"][-1]["round"] == r:
        item["rounds"][-1] = rnd
    else:
        item["rounds"].append(rnd)
    item["sha256"] = rnd["sha256"]
    item["status"] = "done" if item.get("notify_only") else "pending"
    if item.get("notify_only"):
        item["result"] = {"notified": True}
        journal(state, item, "done", via="notify-only")


def item_buttons(state: dict, item: dict) -> tuple[list, str]:
    """Telegram keyboard of the item's current round and its one-line legend (HTML)."""
    esc, lab, options = html.escape, item["labels"], item.get("options") or []
    up = lambda w: w[:1].upper() + w[1:]  # noqa: E731
    if item.get("notify_only"):
        return [], "Уведомление от агента — ответа не требуется."
    if options:
        buttons = [[{"text": f"{NUM_EMOJI[i]} {opt[:45]}", "callback_data": callback(state, item, f"c{i+1}")}]
                   for i, opt in enumerate(options)]
        buttons.append([{"text": f"❌ {up(lab['reject'])}", "callback_data": callback(state, item, "r")}])
        buttons.append([{"text": "✍ Свой вариант", "callback_data": callback(state, item, "e")},
                        {"text": TO_AGENT, "callback_data": callback(state, item, "g")}])
        return buttons, " · ".join(f"{NUM_EMOJI[i]} {esc(opt)}" for i, opt in enumerate(options))
    buttons = [[{"text": f"✅ {up(lab['approve'])}", "callback_data": callback(state, item, "a")},
                {"text": f"❌ {up(lab['reject'])}", "callback_data": callback(state, item, "r")}],
               [{"text": "✍ Правки", "callback_data": callback(state, item, "e")},
                {"text": TO_AGENT, "callback_data": callback(state, item, "g")}]]
    return buttons, f"✅ — {esc(lab['approve'])} · ❌ — {esc(lab['reject'])}"


def notify(ctx: Ctx, state: dict, item: dict, text: str, telegram: bool = True) -> None:
    """A short line to both channels (every recipient in Telegram); like every message of the item it opens with
    the item id."""
    msg = f"{item['id']} · {text}"
    cur = item["rounds"][-1] if item["rounds"] else {"slack": {}, "telegram": {}}
    if cur["slack"].get("ts"):
        try:
            ctx.bot.chat_postMessage(channel=state["slack_channel"], thread_ts=cur["slack"]["ts"], text=msg, unfurl_links=False)
        except Exception as err:
            warn(f"{item['id']}: Slack notice lost: {err}")
    if ctx.tg and telegram:
        cards = tg_cards(ctx, cur["telegram"])
        for person in recipients_of(ctx, state):
            card = (cards.get(int(person["chat_id"])) or {}).get("card_id") if person["chat_id"] is not None else None
            reply = {"reply_to_message_id": card, "allow_sending_without_reply": True} if card else {}
            sent = _ask_owner().cosmetic(ctx.tg, "sendMessage", chat_id=person["chat_id"], text=msg,
                                         disable_web_page_preview=True, **reply)
            if isinstance(sent, dict):
                remember_message(state, item, person["chat_id"], sent.get("message_id"))


def _closed_summary(item: dict, summary: str | None = None) -> str:
    if summary:
        return summary
    status = item.get("status", "")
    lab = item.get("labels") or DEFAULT_LABELS
    if status == "done":
        if item.get("choice_label"):
            return f"✔ Решено: {item['choice_label']}"
        return f"✔ Принято: {lab.get('approve', 'ок')}"
    if status == "rejected":
        return f"✔ Отклонено: {lab.get('reject', 'не ок')}"
    if status == "instructed":
        return f"✔ По указанию: {item.get('result') or ''}".rstrip()
    if status == "commented":
        return "✍ Правки записаны"
    if status == "correcting":
        return "✍ Правки приняты — готовится новый раунд"
    if status == "approving":
        return f"✔ Принято: {lab.get('approve', 'ок')} — выполняется"
    if status == "waiting_after":
        return f"✔ Принято: {lab.get('approve', 'ок')} — ждёт зависимости"
    if status == "expired":
        return "⌛ Истекло — действие не запускается"
    if status == "cancelled":
        return f"✖ Снято: {item.get('cancel_reason') or CANCEL_REASON}"
    return f"Статус: {STATUS_RU.get(status, status)}"


def disarm(ctx: Ctx, item: dict, state: dict | None = None, summary: str | None = None) -> None:
    """Close the item's cards across all channels: in Telegram, mark the card with the terminal outcome and remove its
    inline keyboard across every round/extra card; in Slack, update the root message with the terminal outcome line."""
    if not item.get("rounds"):
        return
    outcome_line = _closed_summary(item, summary)
    title = (item.get("labels") or {}).get("title") or item.get("title") or ""
    header = f"{item['id']} · {title}".strip(" ·")
    if ctx.tg:
        tg_text = f"<b>{html.escape(header)}</b>\n{html.escape(outcome_line)}"
        for rnd in item["rounds"]:
            for chat, ids in tg_cards(ctx, rnd.get("telegram", {})).items():
                for card in [ids.get("card_id")] + list(ids.get("extra_ids") or []):
                    if card:
                        tg_cosmetic(ctx, "editMessageText", chat_id=chat, message_id=card,
                                    text=tg_text, parse_mode="HTML", reply_markup={"inline_keyboard": []})
                        tg_cosmetic(ctx, "editMessageReplyMarkup", chat_id=chat, message_id=card,
                                    reply_markup={"inline_keyboard": []})
    bot = getattr(ctx, "bot", None)
    cur_slack = item["rounds"][-1].get("slack", {})
    slack_ch = (state or {}).get("slack_channel") or cur_slack.get("text_channel")
    if bot is not None and slack_ch and cur_slack.get("ts"):
        closed_line = f"{header} · {outcome_line}"
        blocks = [section(f"*{header}*\n{outcome_line}")]
        if state and state.get("name"):
            blocks.append({"type": "context", "elements": [
                {"type": "mrkdwn", "text": f"🤖 {getattr(ctx, 'origin', '')} · slack-ask batch {state['name']}"}]})
        try:
            bot.chat_update(channel=slack_ch, ts=cur_slack["ts"], text=closed_line, blocks=blocks,
                            unfurl_links=False, unfurl_media=False)
        except Exception as err:
            warn(f"{item['id']}: Slack card update skipped: {err}")


def ensure_slack_mirror(ctx: Ctx, state: dict, item: dict) -> str | None:
    """Ensure the current round has a verifiable permalink even when the owner answered in Telegram before
    `telegram-first` escalation fired."""
    if not item.get("rounds"):
        return None
    cur = item["rounds"][-1]
    s = cur.setdefault("slack", {})
    if s.get("permalink"):
        return s["permalink"]
    bot = getattr(ctx, "bot", None)
    mode = (state.get("channels") or {}).get("mode", "slack-first")
    if bot is not None and mode != "telegram-only":
        try:
            if not state.get("slack_channel"):
                state["slack_channel"] = str(bot.conversations_open(users=ctx.owner)["channel"]["id"])
            title = (item.get("labels") or {}).get("title") or item.get("title") or ""
            line = f"{item['id']} · {title}".strip(" ·")
            try:
                body = Path(cur["snapshot"]).read_text(encoding="utf-8")
            except OSError:
                body = render_payload(item)
            chunks = _send_blocks().chunk_markdown(body, BATCH_MD_LIMIT) if body.strip() else []
            lab = item.get("labels") or DEFAULT_LABELS
            options = item.get("options") or []
            legend = ("  ".join([f"{NUM_EMOJI[i]} {opt}" for i, opt in enumerate(options)] + [f"❌ — {lab['reject']}"])
                      if options else f"✅ — {lab['approve']}   ❌ — {lab['reject']}")
            head = [f"*{line}*"] + ([item["detail"]] if item.get("detail") else []) + ["", legend]
            if item.get("approve_only"):
                head.append(approve_only_note(item))
            blocks = [section("\n".join(head))] + chunks[:1] + [
                {"type": "context", "elements": [{"type": "mrkdwn", "text": f"🤖 {ctx.origin} · slack-ask batch {state['name']}"}]}]
            first = bot.chat_postMessage(channel=state["slack_channel"], text=line, blocks=blocks,
                                         unfurl_links=False, unfurl_media=False)
            s["ts"] = str(first["ts"])
            s["permalink"] = permalink(state["slack_channel"], s["ts"])
            cur["secondary_sent"] = "telegram"
            return s["permalink"]
        except Exception as err:
            warn(f"{item['id']}: Slack mirror on Telegram answer failed: {err}")
    tg_info = cur.get("telegram") or {}
    card_id = tg_info.get("card_id")
    if card_id and getattr(ctx, "chat_id", None) is not None:
        s["permalink"] = f"telegram:{ctx.chat_id}/{card_id}"
        return s["permalink"]
    return None


def new_round(ctx: Ctx, state: dict, item: dict, text: str) -> None:
    previous = Path(item["rounds"][-1]["snapshot"]).read_text(encoding="utf-8")
    disarm(ctx, item, state, summary=f"↻ Заменён раундом {item['round'] + 1}")
    if item.get("answers"):  # a new text is a new question: answers to the old one do not carry over
        item["answers"] = {}
    item["round"] += 1
    send_round(ctx, state, item, text, diff=short_diff(previous, text))
    journal(state, item, "round_sent", sha256=item["sha256"])


def start_hook(ctx: Ctx, state: dict, item: dict, kind: str, text: str | None = None) -> None:
    cur = item["rounds"][-1]
    hooks_dir = batch_dir(state["name"]) / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{item['n']:02d}-r{item['round']}-{kind}-{int(ctx.clock() * 1000)}"
    text_file = ""
    if text is not None:
        (hooks_dir / f"{stem}.txt").write_text(text, encoding="utf-8")
        text_file = str(hooks_dir / f"{stem}.txt")
    dep = dependency(state, item)
    choice_file = ""
    if kind == "on_approve" and item.get("choice") is not None:
        choice_path = hooks_dir / f"{stem}-choice.txt"
        contents = f"Решение владельца: {item.get('choice_label', '')}"
        if item.get("choice_comment"):
            contents += f"\nКомментарий: {item['choice_comment']}"
        choice_path.write_text(contents, encoding="utf-8")
        choice_file = str(choice_path)
    values = {**item.get("vars", {}), "id": item["id"], "n": item["n"], "batch": state["name"], "round": item["round"],
              "payload": cur["snapshot"], "source": item.get("payload_file") or "", "sha256": cur["sha256"],
              "text_file": text_file, "text": text or "", "choice": item.get("choice", ""),
              "choice_label": item.get("choice_label", ""), "choice_comment": item.get("choice_comment", ""),
              "choice_file": choice_file,
              "state_dir": str(batch_dir(state["name"])),
              "after_permalink": ((dep or {}).get("result") or {}).get("permalink", ""),
              "slice": re.sub(r"[^A-Za-z0-9._-]", "_", f"sa-{state['name']}-{item['id']}-r{item['round']}")}
    argv = hook_argv(item["hooks"][kind], values)
    if kind == "on_approve":
        item["status"] = "approving"
    elif kind == "on_corrections":
        item["status"] = "correcting"
    # the decision is on disk before anything runs: a restarted watch never re-runs it blindly
    item["hook"] = {"kind": kind, "argv": argv, "pending_start": True}
    save_batch(state)
    item["hook"] = {"kind": kind, **ctx.runner.start(argv, hooks_dir / f"{stem}.log", hooks_dir / f"{stem}.exit")}
    save_batch(state)
    journal(state, item, f"{kind}_started", log=item["hook"].get("log"))


def decide(ctx: Ctx, state: dict, item: dict, action: str, via: str, text: str | None = None,
           comment: str | None = None, by: str | None = None) -> str:
    """One owner decision on the item's current round → a short toast for the Telegram button. Several recipients:
    called once everyone agreed (`answer`), with `by` — all names and `comment` — their merged comments."""
    cur, lab = item["rounds"][-1], item["labels"]
    src = " (из Telegram)" if via.startswith("telegram") else ""
    if by:
        src = f" ({by})"
        item["by"] = by
    if via.startswith("telegram:") and not cur["slack"].get("permalink"):
        ensure_slack_mirror(ctx, state, item)
    approval = cur["slack"].get("permalink") or f"telegram:{ctx.chat_id}/{cur['telegram'].get('card_id')}"
    choice = int(action[1:]) if action.startswith("c") and action[1:].isdigit() else None
    choice_text = action == "choice_text"
    choice_comment = ""
    name = {"a": "approve", "r": "reject", "e": "corrections", "choice_text": "choice"}.get(action, "choice" if choice else "choice")
    if action == "e" and item.get("options") and item.get("options_text") != "corrections":
        parsed_choice = text_choice(text or "", len(item["options"]))
        if parsed_choice:
            action = f"c{parsed_choice}"
            choice = parsed_choice
            choice_comment = text or ""
        else:
            action = "choice_text"
            choice_text = True
    if action == "e" and item["status"] == "correcting":
        item.setdefault("queued", []).append(text or "")
        journal(state, item, "corrections_queued", via=via, text=text, approval_permalink=approval)
        notify(ctx, state, item, "✍ правки добавлены в очередь — корректор ещё работает над прошлыми")
        return "в очереди"
    if item["status"] not in OPEN:
        return f"уже: {STATUS_RU.get(item['status'], item['status'])}"
    if choice:
        item["choice"] = choice
        item["choice_label"] = item["options"][choice - 1]
        item["choice_comment"] = comment if comment is not None else choice_comment
    elif choice_text:
        item["choice"] = 0
        item["choice_label"] = text or ""
        item["choice_comment"] = ""
    if choice or choice_text:
        journal(state, item, "choice", via=via, text=text, choice=item["choice"],
                choice_label=item["choice_label"], choice_comment=item.get("choice_comment", ""),
                approval_permalink=approval, sha256=cur["sha256"], by=by)
        action = "a"
    else:
        journal(state, item, name, via=via, text=text, approval_permalink=approval, sha256=cur["sha256"], by=by)
    close_item_prompts(ctx, state, item, "ответ записан")
    if action == "a":
        current = render_payload(item)
        if sha256(current) != cur["sha256"]:  # approved text ≠ text now: never act on what was not reviewed
            journal(state, item, "stale", via=via, sha256=sha256(current))
            notify(ctx, state, item, f"⚠ текст изменился после отправки на ревью — не выполняю, шлю раунд {item['round'] + 1}")
            new_round(ctx, state, item, current)
            return "текст изменился — новый раунд"
        if "on_approve" not in item["hooks"]:  # send admits this only with approve_only, said in the message itself
            if by:  # several recipients agreed: who and what, the cards close for everyone below
                arguments = f" · доводы: {comment}" if comment else ""
                notify(ctx, state, item, f"✔ Решено: {verdict(item, 'a')} ({by}){arguments} — бот ничего не публикует, действует агент")
            else:
                notify(ctx, state, item, f"✔ Принято{src}: {lab['approve']} — согласовано; бот ничего не публикует, публикует агент")
            item["status"] = "done"
            if cur["slack"].get("permalink") and not isinstance(item.get("result"), dict):
                item["result"] = {"permalink": cur["slack"]["permalink"]}
            disarm(ctx, item, state)
            return "принято"
        dep = dependency(state, item)
        if dep is not None and not may_run_dependent(state, item):
            item["status"] = "waiting_after"
            journal(state, item, "waiting_after", after=dep["id"])
            notify(ctx, state, item, f"✔ Принято{src}: {lab['approve']} — выполню после {dep['id']} "
                                     f"(сейчас: {STATUS_RU.get(dep['status'], dep['status'])})")
            disarm(ctx, item, state)
            return f"принято, жду {dep['id']}"
        notify(ctx, state, item, f"✔ Принято{src}: {lab['approve']}")
        start_hook(ctx, state, item, "on_approve")
        disarm(ctx, item, state)
        return "принято, выполняю"
    if action == "r":
        instruction = next((note for note in reversed(state.get("to_agent") or [])
                            if note.get("id") == item["id"] and note.get("round") == item["round"]
                            and str(note.get("chat", ctx.chat_id)) == str(ctx.chat_id)), None)
        if instruction is not None:
            item["status"] = "instructed"
            item["result"] = instruction.get("text", "")
            journal(state, item, "instructed", via=via, text=item["result"])
            notify(ctx, state, item, f"✔ По указанию: {item['result']}")
            disarm(ctx, item, state)
            return "по указанию"
        item["status"] = "rejected"
        notify(ctx, state, item, f"✔ Решено: {verdict(item, 'r')} ({by})" +
               (f" · доводы: {comment}" if comment else "") if by else f"✔ Принято{src}: {lab['reject']}")
        disarm(ctx, item, state)
        if "on_reject" in item["hooks"]:
            start_hook(ctx, state, item, "on_reject")
        return "отклонено"
    if "on_corrections" not in item["hooks"]:
        item["status"] = "commented"
        if text:
            item["result"] = {"text": text, **({"permalink": cur["slack"]["permalink"]} if cur["slack"].get("permalink") else {})}
        notify(ctx, state, item, f"✍ правки записаны{src} (корректора у элемента нет)")
        disarm(ctx, item, state)
        return "записано"
    start_hook(ctx, state, item, "on_corrections", text=text or "")
    disarm(ctx, item, state)
    notify(ctx, state, item, f"✍ Правки приняты{src} — корректор работает, потом пришлю раунд {item['round'] + 1}")
    return "правки приняты"


def answer_label(item: dict, a: dict) -> str:
    """One recipient's answer as people read it: «2️⃣ Beta — комментарий», «✍ свой текст», «❌ не ок»."""
    act = a.get("action", "")
    if act.startswith("c") and act[1:].isdigit():
        i = int(act[1:])
        label = f"{NUM_EMOJI[i - 1]} {item['options'][i - 1]}"
    elif act == "choice_text":
        label = f"✍ {a.get('text') or ''}"
    elif act == "a":
        label = f"✅ {item['labels']['approve']}"
    else:
        label = f"❌ {item['labels']['reject']}"
    return label + (f" — {a['comment']}" if a.get("comment") else "")


def send_argument_notice(ctx: Ctx, state: dict, item: dict, chat, record: dict) -> None:
    """Show one recipient's dispute choice and argument to every other recipient, on each change."""
    argument = f"«{record['argument']}»" if record.get("argument") else "без аргумента"
    message = (f"{item['id']} · {record.get('by') or person_name(ctx, state, chat)} выбрал(а): "
               f"{answer_label(item, record)}. Аргумент: {argument}")
    for person in recipients_of(ctx, state):
        if int(person["chat_id"]) == int(chat):
            continue
        card = latest_card(ctx, item, person["chat_id"])
        if card:
            _ask_owner().cosmetic(ctx.tg, "editMessageText", chat_id=person["chat_id"], message_id=card,
                                  text=dispute_card_text(item), parse_mode="HTML",
                                  reply_markup={"inline_keyboard": item_buttons(state, item)[0]})
        sent = _ask_owner().cosmetic(ctx.tg, "sendMessage", chat_id=person["chat_id"], text=message,
                                     disable_web_page_preview=True)
        remember_message(state, item, person["chat_id"], (sent or {}).get("message_id"))


def question_context_html(item: dict, room: int) -> str:
    """The full text of the original question under a spoiler, so that nobody has to scroll back to remember it
    (owner 2026-10-28: «в расхождениях не хватает полного текста под спойлером — я забываю контекст»): title, detail,
    «что сейчас» (now), the question (ask), the payload, every option with its text, the recommendation. It shares one
    Telegram message with the card (`room` characters of HTML): the longest piece is cut with «…» first, options are
    never dropped."""
    try:
        payload = render_payload(item)
    except Exception:  # a missing payload file must not cost the disagreement card
        payload = ""
    esc = html.escape
    options = item.get("options") or []
    pieces = [("<b>", "</b>", (item.get("labels") or {}).get("title") or ""), ("", "", item.get("detail") or ""),
              ("<b>Что сейчас:</b> ", "", item.get("now") or ""), ("<b>Вопрос:</b> ", "", item.get("ask") or ""),
              ("", "", payload)]
    pieces += [("", "", f"{NUM_EMOJI[i]} {opt}") for i, opt in enumerate(options)]
    if item.get("recommendation"):
        pieces.append(("<b>Рекомендация:</b> ", "", str(item["recommendation"])))
    pieces = [[pre, post, str(raw).strip()] for pre, post, raw in pieces if str(raw).strip()]
    if not pieces:
        return ""
    shell = "<blockquote expandable><tg-spoiler>", "</tg-spoiler></blockquote>"

    def size() -> int:
        return len(shell[0]) + len(shell[1]) + sum(len(pre) + len(post) + len(esc(raw)) + 1 for pre, post, raw in pieces)

    while size() > room:
        biggest = max(pieces, key=lambda piece: len(esc(piece[2])))
        raw = biggest[2].rstrip("…")
        if len(raw) <= 12:
            return ""  # the card itself leaves no room for a context
        biggest[2] = raw[:max(11, len(raw) - max(1, size() - room))].rstrip() + "…"
    return shell[0] + "\n".join(pre + esc(raw) + post for pre, post, raw in pieces) + shell[1]


def _context_tail(item: dict, head: str) -> str:
    context = question_context_html(item, TG_LIMIT - len(head) - 2)
    return "\n\n" + context if context else ""


def cap_html(text, limit: int) -> str:
    """`text` HTML-escaped and cut with «…» so that the escaped form is at most `limit` characters."""
    raw = str(text)
    if len(html.escape(raw)) <= limit:
        return html.escape(raw)
    low, high = 0, len(raw)
    while low < high:  # the longest prefix that fits together with the «…»
        mid = (low + high + 1) // 2
        if len(html.escape(raw[:mid].rstrip() + "…")) <= limit:
            low = mid
        else:
            high = mid - 1
    return html.escape(raw[:low].rstrip() + "…")


def dispute_lines(item: dict, answers: dict, people: int, arg_word: str, empty: str, half: bool = False) -> str:
    """One line per person: name — choice; argument. The unbounded parts (own text of a choice, the argument) are cut so
    that the head of the card leaves room for the question context under the spoiler (4096 characters in all)."""
    cap = max(30, 500 // max(1, people)) // (2 if half else 1)
    return "\n".join(f"{cap_html(a.get('by') or 'Участник', 40)} — {cap_html(answer_label(item, a), cap)}; {arg_word}: "
                     + (cap_html(a['argument'], cap) if a.get("argument") else empty) for a in answers.values())


def dispute_card_text(item: dict) -> str:
    disputes = item.get("disputes") or []
    if not disputes:
        return ""
    attempt = len(disputes)
    answers = disputes[-1].get("answers") or {}
    people = list(answers.values())
    head = (f"<b>{html.escape(item['id'])} · Расхождение · попытка {attempt}</b>\n" +
            dispute_lines(item, answers, len(people), "довод", "без довода") +
            "\n\nВыбери общий вариант.")
    return head + _context_tail(item, head)


def _telegram_state() -> tuple[Path, dict]:
    path = BATCH_HOME / "telegram.json"
    try:
        return path, json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return path, {"offset": None, "prompts": {}}


def close_item_prompts(ctx: Ctx, state: dict, item: dict, reason: str) -> None:
    """Close all text requests for one item in Telegram's global prompt state."""
    path, gs = _telegram_state()
    gs = getattr(ctx, "telegram_state", None) or gs
    changed = False
    for field in ("prompts", "argument_prompts", "reason_prompts"):
        prompts = gs.setdefault(field, {})
        for key, value in list(prompts.items()):
            if value and value[0] == state["name"] and int(value[1]) == int(item["n"]):
                prompts.pop(key, None)
                if ctx.tg:
                    chat, mid = key.split(":", 1)
                    _ask_owner().cosmetic(ctx.tg, "editMessageText", chat_id=int(chat), message_id=int(mid),
                                          text=f"закрыто: {reason}")
                changed = True
    if changed and not hasattr(ctx, "telegram_state"):
        _ask_owner().save_state(path, gs)


def prune_closed_prompts(ctx: Ctx, gstate: dict, batches: dict[str, dict]) -> None:
    """Repair legacy/orphan prompt entries whose item is no longer open."""
    changed = False
    for field in ("prompts", "argument_prompts", "reason_prompts"):
        prompts = gstate.setdefault(field, {})
        for key, value in list(prompts.items()):
            state = batches.get(value[0])
            if not state or int(value[1]) > len(state["items"]):
                continue
            item = state["items"][int(value[1]) - 1]
            if item["status"] in OPEN + ("correcting",):
                continue
            prompts.pop(key, None)
            changed = True
            if ctx.tg and field in ("argument_prompts", "reason_prompts"):
                chat, mid = key.split(":", 1)
                reason = "истекло" if item["status"] == "expired" else \
                    f"снято: {item.get('cancel_reason')}" if item["status"] == "cancelled" else \
                    "отклонено" if item["status"] in ("rejected", "approve_failed") else "решено"
                _ask_owner().cosmetic(ctx.tg, "editMessageText", chat_id=int(chat), message_id=int(mid),
                                      text=f"закрыто: {reason}")
    if changed:
        _ask_owner().save_state(BATCH_HOME / "telegram.json", gstate)


def consume_prompt(ctx: Ctx, gstate: dict, field: str, state: dict, item: dict, chat, reason: str) -> None:
    prompts = gstate.setdefault(field, {})
    for key, value in list(prompts.items()):
        if value[:3] == [state["name"], item["n"], chat]:
            prompts.pop(key, None)
            _ask_owner().cosmetic(ctx.tg, "editMessageText", chat_id=chat,
                                  message_id=int(key.split(":", 1)[1]), text=f"закрыто: {reason}")


def ask_argument(ctx: Ctx, gstate: dict, state: dict, item: dict, chat, action: str) -> None:
    """Maintain one argument request per person and item, replacing it after a changed vote."""
    option = (item["options"][int(action[1:]) - 1]
              if action.startswith("c") and action[1:].isdigit() else "этот выбор")
    prompts = gstate.setdefault("argument_prompts", {})
    for key, value in list(prompts.items()):
        if value[:3] == [state["name"], item["n"], chat]:
            prompts.pop(key, None)
            _ask_owner().cosmetic(ctx.tg, "editMessageText", chat_id=chat, message_id=int(key.split(":", 1)[1]),
                                  text="закрыто: заменён новым запросом")
    buttons = [[{"text": "Без аргумента", "callback_data": callback(state, item, "na")}]]
    question = "Что не так? Напиши — уйдёт агенту." if action == "r" else \
        f"Почему ты за {option}? Ответь одним сообщением или нажми «Без довода»."
    prompt = tg_send(ctx, chat_id=chat, text=f"{item['id']} · {question}",
                     reply_markup={"inline_keyboard": buttons},
                     **({"reply_to_message_id": latest_card(ctx, item, chat), "allow_sending_without_reply": True}
                        if latest_card(ctx, item, chat) else {}))
    prompts[f"{chat}:{prompt}"] = [state["name"], item["n"], chat, item["round"]]
    remember_message(state, item, chat, prompt)


def open_argument_requests(ctx: Ctx, state: dict, item: dict) -> None:
    if not ctx.tg:
        return
    path, gs = _telegram_state()
    gs = getattr(ctx, "telegram_state", None) or gs
    for p in recipients_of(ctx, state):
        chat = p["chat_id"]
        if chat in (item.get("dispute_delivery") or {}).get("pending_chats", []):
            continue
        mine = (item.get("disputes") or [])[-1].get("answers", {}).get(str(chat), {})
        ask_argument(ctx, gs, state, item, chat, mine.get("action", ""))
    if not hasattr(ctx, "telegram_state"):
        _ask_owner().save_state(path, gs)


def verdict(item: dict, action: str) -> str:
    """The agreed answer as people read it: «2️⃣ Beta», «✍ свой текст», «✅ ок», «❌ не ок»."""
    if action == "a" and item.get("choice") is not None:
        if not item["choice"]:  # own text (or the agent's wording after review): the words themselves
            return item.get("choice_label") or ""
        action = f"c{item['choice']}"
    return answer_label(item, {"action": action, "text": item.get("choice_label")})


def answer_toast(ctx: Ctx, state: dict, item: dict, chat, action: str) -> str:
    """What a button answer will do, known before it is recorded — the callback is answered with it first."""
    if item["status"] not in OPEN:
        return f"уже: {STATUS_RU.get(item['status'], item['status'])}"
    old = (item.get("answers") or {}).get(str(chat)) if is_multi(state) else None
    if old and old.get("action") == action:
        return f"уже записано: {answer_label(item, old)}"
    label = answer_label(item, {"action": action})
    answers = {**(item.get("answers") or {}), str(chat): {"action": action, "text": None}}
    people = recipients_of(ctx, state)
    missing = [person_name(ctx, state, p["chat_id"]) for p in people if str(p["chat_id"]) not in answers]
    if missing:
        return f"Записал: {label}; ждём {', '.join(missing)}"
    picks = [answers[str(p["chat_id"])] for p in people]
    return {"agreed": f"Решено: {label}", "review": REVIEW_NOTE}.get(
        outcome(picks), "Расхождение — ответы у всех, выберите общий вариант")


CHANGE_ANSWER = "✏ Изменить ответ"


def latest_card(ctx: Ctx, item: dict, chat) -> int | None:
    """The recipient's live card of the item: his last disagreement card, else the round's card."""
    ids = tg_cards(ctx, item["rounds"][-1]["telegram"]).get(int(chat)) or {}
    return (ids.get("extra_ids") or [None])[-1] or ids.get("card_id")


def set_keyboard(ctx: Ctx, item: dict, chat, buttons: list) -> int | None:
    """Replace the keyboard of the recipient's live card in HIS chat only (cosmetic: «not modified» is fine)."""
    card = latest_card(ctx, item, chat)
    if ctx.tg and card:
        tg_cosmetic(ctx, "editMessageReplyMarkup", chat_id=chat, message_id=card,
                    reply_markup={"inline_keyboard": buttons})
    return card


def answered_keyboard(state: dict, item: dict) -> list:
    """After a recipient answered (owner 2026-10-27: «у меня кнопки пропадали, когда я отвечал»): the options go away
    at once, «✏ Изменить ответ» brings them back, «↩ Агенту» stays. ~~8894757: ✅ on the chosen button~~."""
    return [[{"text": CHANGE_ANSWER, "callback_data": callback(state, item, "m")},
             {"text": TO_AGENT, "callback_data": callback(state, item, "g")}]]


def answer(ctx: Ctx, state: dict, item: dict, chat, action: str, via: str, text: str | None = None) -> str:
    """One recipient's answer → a toast. The owner alone: `decide` at once, as always. Several recipients (the rules
    of ask_owner.record/resolve): each answer is kept per chat and may be changed until the decision; everyone
    answered the same (the same option; own text — the same text) → one decision, `by` all names; answers differ →
    everyone gets «Расхождение · …» with the answers by name and the same buttons, the attempt goes to
    `disputes`, answers restart. Text corrections (an item without options) still go to the corrector."""
    if not is_multi(state):
        return decide(ctx, state, item, action, via, text)
    comment = None
    if action == "e":
        if not item.get("options") or item.get("options_text") == "corrections":
            return decide(ctx, state, item, "e", via, text)
        parsed = text_choice(text or "", len(item["options"]))
        action, comment = (f"c{parsed}", text) if parsed else ("choice_text", None)
    if item["status"] not in OPEN:
        return f"уже: {STATUS_RU.get(item['status'], item['status'])}"
    ao, esc = _ask_owner(), html.escape
    by = person_name(ctx, state, chat)
    answers = item.setdefault("answers", {})
    mine = {"action": action, "text": (text or "").strip() if action == "choice_text" else None,
            "comment": comment, "by": by, "via": via, "at": now_iso()}
    previous = answers.get(str(chat))
    if previous and previous.get("action") == mine.get("action") and previous.get("text") == mine.get("text") and \
            previous.get("comment") == mine.get("comment"):
        return f"уже записано: {answer_label(item, previous)}"
    answers[str(chat)] = mine
    journal(state, item, "answer", via=via, by=by, choice_label=answer_label(item, mine), text=text,
            argument=mine.get("argument"))
    people = recipients_of(ctx, state)
    missing = [person_name(ctx, state, p["chat_id"]) for p in people if str(p["chat_id"]) not in answers]
    if missing:
        card = set_keyboard(ctx, item, chat, answered_keyboard(state, item))
        if card:
            ao.cosmetic(ctx.tg, "editMessageText", chat_id=chat, message_id=card,
                        text=f"{item['id']} · ✔ Ваш ответ: {html.escape(answer_label(item, mine))} · ждём {html.escape(', '.join(missing))}",
                        parse_mode="HTML", reply_markup={"inline_keyboard": answered_keyboard(state, item)})
        return f"записал, ждём {', '.join(missing)}"
    picks = [answers[str(p["chat_id"])] for p in people]
    names = ", ".join(a["by"] for a in picks)
    act = picks[0]["action"]
    result = outcome(picks)
    if result == "agreed":
        merged = "; ".join(f"{a['by']}: " + "; ".join(
            value for value in (a.get("comment"), a.get("argument")) if value)
            for a in picks if a.get("comment") or a.get("argument"))
        return decide(ctx, state, item, act, "telegram:agreed", text=picks[0]["text"] if act == "choice_text" else None,
                      comment=merged if act.startswith("c") else None, by=names)
    if result == "review":
        item["status"] = "review"
        journal(state, item, "review", via=via, by=names, text="; ".join(f"{a['by']}: {review_text(item, a)}" for a in picks))
        for p in people:  # everyone's options are folded, as after an answer; the agent compares the meaning
            card = set_keyboard(ctx, item, p["chat_id"], answered_keyboard(state, item))
            reply = {"reply_to_message_id": card, "allow_sending_without_reply": True} if card else {}
            ao.cosmetic(ctx.tg, "sendMessage", chat_id=p["chat_id"], disable_web_page_preview=True,
                        text=f"{item['id']} · {REVIEW_NOTE}", **reply)
        return REVIEW_NOTE
    return send_dispute(ctx, state, item, via)


REVIEW_NOTE = "Записал; сверяю ответы по смыслу"


def norm_text(text: str | None) -> str:
    """Own texts are compared after case, ё/е, punctuation and spaces are folded."""
    folded = re.sub(r"[^\w\s]|_", " ", (text or "").lower().replace("ё", "е"))
    return " ".join(folded.split())


def ends_with_question(text: str) -> bool:
    closers = set("\"'»”’)]}）)！。…")
    while text and (text[-1] in closers or unicodedata.category(text[-1]) in {"So", "Sk", "Cf", "Mn"}):
        text = text[:-1]
    return text.rstrip().endswith("?")


def outcome(picks: list[dict]) -> str:
    """Everyone answered: «agreed» — the same option, or own texts equal after norm_text; «review» — answers differ and
    at least one is own text (2026-10-27, OQ-100: «Без пробного режима в мвп» vs «Без пробного» is one meaning — the
    agent compares, `batch resolve`); «dispute» — different buttons only."""
    actions = {a["action"] for a in picks}
    if len(actions) == 1 and (picks[0]["action"] != "choice_text" or len({norm_text(a["text"]) for a in picks}) == 1):
        return "agreed"
    return "review" if "choice_text" in actions else "dispute"


JUDGE_SCHEMA = {"type": "object", "properties": {"rationale": {"type": "string"},
               "same": {"type": "boolean"}, "agreed": {"type": "string"}},
               "required": ["rationale", "same", "agreed"], "additionalProperties": False}
JUDGE_ASK = None  # test seam; None loads the shared owner-decisions model ladder


def answer_version(item: dict) -> str:
    return hashlib.sha256(json.dumps(item.get("answers") or {}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def judge_result_current(item: dict, version: str) -> bool:
    return item.get("status") == "review" and answer_version(item) == version


def judge_prompt(state: dict, item: dict) -> str:
    picks = []
    for person in state.get("recipients") or []:
        answer = (item.get("answers") or {}).get(str(person["chat_id"]))
        if answer:
            picks.append({"person": answer.get("by") or person.get("name") or str(person["chat_id"]),
                          "answer": review_text(item, answer)})
    card = item.get("payload") or ""
    if item.get("payload_file"):
        try:
            card = Path(item["payload_file"]).read_text(encoding="utf-8")
        except OSError:
            card = ""
    return ("Сравни ответы участников на вопрос. Реши, выражают ли они одно и то же решение, включая случай, когда "
            "свободный ответ равен по смыслу одной кнопке. Если да, предложи короткую формулировку общего решения. "
            "Если нет — same=false. Верни rationale одной короткой фразой.\n"
            f"Вопрос: {item.get('id')} · {(item.get('labels') or {}).get('title', '')}\n"
            f"Карточка: {item.get('detail') or ''}\n{card}\n"
            f"Варианты: {json.dumps(item.get('options') or [], ensure_ascii=False)}\n"
            f"Ответы: {json.dumps(picks, ensure_ascii=False)}")


def ask_judge(state: dict, item: dict) -> tuple[dict, str]:
    if JUDGE_ASK is not None:
        return JUDGE_ASK(judge_prompt(state, item), JUDGE_SCHEMA)
    import importlib.util
    path = DEFAULT_CORRECTOR.parent / "model_ladder.py"
    spec = importlib.util.spec_from_file_location("owner_decisions_model_ladder", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"model ladder unavailable: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ask_json(judge_prompt(state, item), JUDGE_SCHEMA)


def review_text(item: dict, a: dict) -> str:
    """«текст или вариант» of one answer for the agent's review."""
    act = a.get("action", "")
    if act == "choice_text":
        return a.get("text") or ""
    if act.startswith("c") and act[1:].isdigit():
        return item["options"][int(act[1:]) - 1]
    return answer_label(item, a)


def send_dispute(ctx: Ctx, state: dict, item: dict, via: str) -> str:
    """Everyone gets «Расхождение · …» with the answers by name and the full buttons; the attempt goes to `disputes`,
    answers restart."""
    esc, people = html.escape, recipients_of(ctx, state)
    answers = item.get("answers") or {}
    picks = [answers[str(p["chat_id"])] for p in people if str(p["chat_id"]) in answers]
    item["status"] = "pending"
    item.setdefault("disputes", []).append({"round": item["round"], "at": now_iso(), "answers": dict(answers)})
    previous = item["disputes"][-2] if len(item["disputes"]) > 1 else None
    item["answers"] = {}
    journal(state, item, "disagreement", via=via, by=", ".join(a["by"] for a in picks),
            text="; ".join(f"{a['by']}: {answer_label(item, a)}" for a in picks))
    buttons, _ = item_buttons(state, item)
    previous_line = ("\nПредыдущая попытка:\n" + dispute_lines(
        item, previous["answers"], len(previous["answers"]), "аргумент", "без аргумента", half=True)) if previous else ""
    card = (f"<b>{esc(item['id'])} · Расхождение · попытка {len(item['disputes'])}</b>\n"
            f"{esc(item['labels']['title'])}\n"
            + dispute_lines(item, {i: a for i, a in enumerate(picks)}, len(picks), "довод", "без довода")
            + previous_line
            + "\n\nОбсудите и выберите общий вариант — каждый жмёт ту же кнопку."
            + f"\n<i>{esc(item['id'])} · попытка {len(item['disputes']) + 1}</i>")
    card += _context_tail(item, card)
    chats = item["rounds"][-1]["telegram"].setdefault("chats", {})
    for p in people:
        chat = p["chat_id"]
        old_ids = tg_cards(ctx, item["rounds"][-1]["telegram"]).get(int(chat)) or {}
        stale = ([old_ids.get("card_id")] if old_ids.get("card_id") else []) + list(old_ids.get("extra_ids") or [])
        for mid in stale:
            _ask_owner().cosmetic(ctx.tg, "editMessageText", chat_id=chat, message_id=mid,
                                  text=f"{item['id']} · Расхождение — см. новую карточку")
            _ask_owner().cosmetic(ctx.tg, "editMessageReplyMarkup", chat_id=chat, message_id=mid,
                                  reply_markup={"inline_keyboard": []})
    item["dispute_delivery"] = {"attempt": len(item["disputes"]), "text": card,
                                "pending_chats": [p["chat_id"] for p in people]}
    retry_dispute_cards(ctx, state, item, buttons)
    open_argument_requests(ctx, state, item)
    return "расхождение — ответы у всех, выберите общий вариант"


def retry_dispute_cards(ctx: Ctx, state: dict, item: dict, buttons: list | None = None) -> None:
    delivery = item.get("dispute_delivery") or {}
    if not delivery.get("pending_chats") or not ctx.tg:
        return
    buttons = buttons if buttons is not None else item_buttons(state, item)[0]
    chats = item["rounds"][-1]["telegram"].setdefault("chats", {}) if item.get("rounds") else {}
    remain = []
    for chat in list(delivery["pending_chats"]):
        try:
            mid = tg_send(ctx, chat_id=chat, text=delivery["text"], parse_mode="HTML",
                          reply_markup={"inline_keyboard": buttons})
        except Exception as err:
            warn(f"{item['id']}: disagreement card not sent to {chat}: {err}")
            remain.append(chat)
            continue
        chats.setdefault(str(chat), {}).setdefault("extra_ids", []).append(mid)
    delivery["pending_chats"] = remain


def check_hooks(ctx: Ctx, state: dict) -> None:
    for item in state["items"]:
        hook = item.get("hook")
        if not hook:
            continue
        res = (-1, "watch stopped before the hook started") if hook.get("pending_start") else ctx.runner.poll(hook)
        if res is None:
            continue
        code, output = res
        item["hook"] = None
        kind, lab, log = hook["kind"], item["labels"], hook.get("log", "")
        result = parse_hook_output(output)
        if kind == "on_approve":
            if code == 0:
                item["status"], item["result"] = "done", result
                journal(state, item, "done", result=result, exit=code)
                link = result.get("permalink") or result.get("url")
                # a hook that published but could not finish the record (ref/commit/push) says so in `warning`
                tail = f"\n⚠ {result['warning']}" if result.get("warning") else ""
                notify(ctx, state, item, (f"{lab['done']}: {link}" if link else f"✔ {lab['done']}") + tail)
                disarm(ctx, item, state)
            else:
                item["status"] = "approve_failed"
                journal(state, item, "approve_failed", exit=code, log=log)
                if ctx.tg and item.get("rounds"):
                    buttons, _ = item_buttons(state, item)
                    for person in recipients_of(ctx, state):
                        set_keyboard(ctx, item, person["chat_id"], buttons)
                notify(ctx, state, item, f"⚠ действие после ✅ не выполнено (exit {code}), лог {log}. "
                                         "Повтор — 👍 в Slack или ✅ в Telegram")
        elif kind == "on_corrections":
            if item.get("queued"):
                text, item["queued"] = "\n\n".join(item["queued"]), []
                start_hook(ctx, state, item, "on_corrections", text=text)
                continue
            try:
                current = render_payload(item)
            except OSError as err:
                current = None
                warn(f"{item['id']}: payload unreadable after corrections: {err}")
            if current is not None and sha256(current) != item["rounds"][-1]["sha256"]:
                journal(state, item, "corrected", exit=code, log=log)
                new_round(ctx, state, item, current)
            else:
                item["status"] = "pending"
                journal(state, item, "corrector_no_change", exit=code, log=log)
                if ctx.tg and item.get("rounds"):
                    buttons, _ = item_buttons(state, item)
                    for person in recipients_of(ctx, state):
                        set_keyboard(ctx, item, person["chat_id"], buttons)
                notify(ctx, state, item, f"⚠ корректор закончил (exit {code}), текст не изменился — жду ответа на "
                                         f"раунд {item['round']}. Лог: {log}")
        else:
            if kind == "on_reject" and code != 0:
                item["status"] = "approve_failed"
                journal(state, item, "reject_failed", exit=code, log=log)
                notify(ctx, state, item, f"⚠ действие после ❌ не выполнено (exit {code}), лог {log}")
            else:
                journal(state, item, f"{kind}_done", exit=code, result=result, log=log)
    release_waiting(ctx, state)


def dependency(state: dict, item: dict) -> dict | None:
    """The item named in `after` (e.g. the thread starter whose permalink is the thread of this post)."""
    after = item.get("after")
    return next((i for i in state["items"] if i["id"] == after), None) if after else None


def may_run_dependent(state: dict, item: dict) -> bool:
    """The dependency is published and no sibling (same `after`) is publishing right now: posts land in the thread
    one by one, in approval order, never racing each other."""
    dep = dependency(state, item)
    if dep is None:
        return True
    if dep["status"] != "done":
        return False
    return not any(i is not item and i.get("after") == item.get("after") and i["status"] == "approving"
                   for i in state["items"])


def release_waiting(ctx: Ctx, state: dict) -> None:
    """Approved items waiting for their dependency: run when it is done, close as blocked when it never will be."""
    for item in state["items"]:
        if item["status"] not in WAITING:
            continue
        dep = dependency(state, item)
        if dep is None or dep["status"] in DEAD or (dep["status"] == "done" and not (dep.get("result") or {}).get("permalink")):
            why = "нет зависимости" if dep is None else (
                f"{dep['id']}: {STATUS_RU.get(dep['status'], dep['status'])}" if dep["status"] in DEAD
                else f"{dep['id']} выполнен без permalink")
            item["status"] = "blocked"
            journal(state, item, "blocked", reason=why)
            notify(ctx, state, item, f"⛔ не выполняю: {why} — публиковать некуда")
            continue
        if may_run_dependent(state, item):
            start_hook(ctx, state, item, "on_approve")


def expire_items(ctx: Ctx, state: dict) -> None:
    """Expire open items before polling, so a late answer cannot run its action."""
    for item in state["items"]:
        timeout = item.get("timeout")
        if timeout is None or item["status"] not in OPEN or not item.get("rounds"):
            continue
        if ctx.clock() - item["rounds"][-1].get("sent_epoch", ctx.clock()) < timeout:
            continue
        item["status"] = "expired"
        close_item_prompts(ctx, state, item, "истекло")
        journal(state, item, "expired", via="timeout")
        notify(ctx, state, item, "⌛ истекло — действие не запускается")
        disarm(ctx, item, state)
        if "on_timeout" in item["hooks"]:
            start_hook(ctx, state, item, "on_timeout")


def send_escalations(ctx: Ctx, state: dict) -> None:
    cfg = state.get("channels", {})
    mode, delay = cfg.get("mode", "slack-first"), cfg.get("escalate_after", 0)
    if mode.endswith("-only") or (delay == 0 and len(state.get("initial_channels", [])) == 2):
        return
    primary = "slack" if mode == "slack-first" else "telegram"
    for item in state["items"]:
        if item["status"] not in OPEN or not item.get("rounds"):
            continue
        rnd = item["rounds"][-1]
        secondary_sent = rnd.get("secondary_sent")
        if secondary_sent or ctx.clock() - rnd.get("sent_epoch", ctx.clock()) < delay:
            continue
        state["initial_channels"] = ["slack", "telegram"]
        if "slack" not in state or not state.get("slack_channel"):
            state["slack_channel"] = str(ctx.bot.conversations_open(users=ctx.owner)["channel"]["id"])
        if state.get("slack_text") == "copy" and ctx.usr and not state.get("copy_channel"):
            state["copy_channel"] = str(ctx.usr.conversations_open(users=ctx.owner)["channel"]["id"])
        send_round(ctx, state, item, Path(rnd["snapshot"]).read_text(encoding="utf-8"))
        item["rounds"][-1]["secondary_sent"] = primary
        state["initial_channels"] = [primary]
        journal(state, item, "channel_escalated", channel="telegram" if primary == "slack" else "slack")


# --- polling ----------------------------------------------------------------------------------------

def _history(client, channel: str, oldest: str, *, sleep=time.sleep, jitter=random.random) -> dict[str, dict]:
    out: dict[str, dict] = {}
    cursor = None
    while True:
        resp = slack_retry(lambda: client.conversations_history(
            channel=channel, oldest=oldest, inclusive=True, limit=200,
            **({"cursor": cursor} if cursor else {})), sleep=sleep, jitter=jitter)
        for m in resp.get("messages", []):
            out[str(m.get("ts"))] = m
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return out


def _history_with_owner_auth(ctx: Ctx, client, channel: str, oldest: str) -> dict[str, dict] | None:
    try:
        return _history(client, channel, oldest, sleep=ctx.sleep)
    except Exception as err:
        if client is not ctx.usr or not is_auth_error(err):
            raise
    if not ctx.owner_auth_reload_attempted:
        ctx.owner_auth_reload_attempted = True
        try:
            fresh = ctx.reload_owner_client() if ctx.reload_owner_client else None
            if fresh is not None:
                if not isinstance(fresh, OwnerDMClient):
                    fresh = OwnerDMClient(fresh, ctx.owner)
                fresh.auth_test()
                fresh.conversations_open(users=ctx.owner)
                ctx.usr = fresh
                try:
                    return _history(fresh, channel, oldest, sleep=ctx.sleep)
                except Exception as retry_err:
                    if not is_auth_error(retry_err):
                        raise
        except Exception as reload_err:
            if not is_auth_error(reload_err) and not isinstance(reload_err, (OSError, RuntimeError)):
                raise
    ctx.owner_auth_failed = True
    return None


FALLBACK_REASON = "пользовательский Slack-токен отклонён"


def switch_to_bot_thread(ctx: Ctx, state: dict) -> None:
    """Copy mode lost the owner's token mid-flight: text goes to the bot thread, and every open item SAYS so in its
    own thread — the owner looks for the copy link there, a silent switch reads as «where is the link?»."""
    state["slack_text"] = "bot-thread"
    state["slack_text_reason"] = FALLBACK_REASON
    for item in state["items"]:
        if item["status"] not in OPEN or not item["rounds"]:
            continue
        s = item["rounds"][-1]["slack"]
        if s.get("ts"):
            s["text_ts"] = s["ts"]
            s["text_channel"] = state.get("slack_channel")
            notify(ctx, state, item, f"✍ копия для правок больше не читается ({FALLBACK_REASON}) — правки пиши "
                                     "в треде ЭТОГО сообщения", telegram=False)
        journal(state, item, "slack_text_fallback", via="auth", mode="bot-thread")


def ensure_owner_client(ctx: Ctx, states: dict[str, dict]) -> None:
    """If a previous batch lost `ctx.usr` on an expired token and `.env` was refreshed later (or a new batch with
    `copy_ts` arrived), try reloading `ctx.usr` once for the new batch so the owner's replies in copy threads are read
    instead of silently switching the new batch to bot-thread."""
    if ctx.usr is not None or not getattr(ctx, "reload_owner_client", None):
        return
    checked = getattr(ctx, "owner_auth_checked_batches", None)
    if checked is None:
        checked = set()
        ctx.owner_auth_checked_batches = checked
    candidates = [
        name for name, state in states.items()
        if name not in checked
        and (state.get("slack_text") == "copy" or state.get("slack_text_reason") == FALLBACK_REASON)
        and any(i["status"] in OPEN + ("correcting",) and i.get("rounds") and i["rounds"][-1]["slack"].get("copy_ts")
                for i in state.get("items", []))
    ]
    if not candidates:
        return
    checked.update(candidates)
    try:
        fresh = ctx.reload_owner_client()
        if fresh is None:
            return
        if not isinstance(fresh, OwnerDMClient):
            fresh = OwnerDMClient(fresh, ctx.owner)
        fresh.auth_test()
        fresh.conversations_open(users=ctx.owner)
        ctx.usr = fresh
        ctx.owner_auth_failed = False
        for name in candidates:
            state = states[name]
            if state.get("slack_text_reason") == FALLBACK_REASON and any(
                i.get("rounds") and i["rounds"][-1]["slack"].get("copy_ts") for i in state.get("items", [])
            ):
                state["slack_text"] = "copy"
                state["slack_text_reason"] = None
                state.pop("owner_auth_failed", None)
    except Exception:
        return


def _fallback_owner_text(ctx: Ctx, states: dict[str, dict]) -> None:
    if not ctx.owner_auth_failed:
        return
    ctx.owner_auth_failed = False
    ctx.usr = None
    checked = getattr(ctx, "owner_auth_checked_batches", None)
    if checked is None:
        checked = set()
        ctx.owner_auth_checked_batches = checked
    for name, state in states.items():
        checked.add(name)
        if state.get("slack_text") != "copy":
            continue
        state["owner_auth_failed"] = True
        switch_to_bot_thread(ctx, state)
    if not ctx.owner_auth_transition_logged:
        ctx.owner_auth_transition_logged = True
        warn("owner Slack auth rejected after one reload; text edits switched to bot-thread")
        try:
            if ctx.auth_notifier:
                ctx.auth_notifier()
            elif ctx.tg:
                tg_send(ctx, text="slack-ask: пользовательский Slack-токен отклонён повторно; правки переведены в bot-thread.")
            else:
                channel = str(ctx.bot.conversations_open(users=ctx.owner)["channel"]["id"])
                ctx.bot.chat_postMessage(channel=channel,
                    text="slack-ask: пользовательский Slack-токен отклонён повторно; правки переведены в bot-thread.")
        except Exception as notify_err:
            warn(f"owner Slack auth notification failed: {notify_err}")


def poll_slack(ctx: Ctx, state: dict, cache: dict | None = None) -> None:
    """One history call per channel for all waiting items: reactions on the bot messages, replies in copies."""
    live = [i for i in state["items"] if i["rounds"] and i["status"] in OPEN + ("correcting",)]
    opened = [i for i in live if i["status"] in OPEN]
    if opened and state.get("slack_channel"):
        msgs = (cache or {}).get((id(ctx.bot), state["slack_channel"])) or _history(
            ctx.bot, state["slack_channel"], min(i["rounds"][-1]["slack"]["ts"] for i in opened))
        for item in opened:
            s = item["rounds"][-1]["slack"]
            m = msgs.get(s["ts"])
            if not m or item["status"] not in OPEN:
                continue
            current_owner_names = {r.get("name", "").split("::")[0] for r in m.get("reactions", [])
                                   if ctx.owner in r.get("users", [])}
            s["used"] = [name for name in s["used"] if name in current_owner_names]
            fresh = [r for r in m.get("reactions", []) if r.get("name", "").split("::")[0] not in s["used"]]
            options = item.get("options") or []
            got = interpret(fresh, ctx.owner, len(options), not options)
            if got:
                s["used"].append(got[1])  # a reaction counts once: a failed action is retried by a new one
                reaction_action = (f"c{got[0]}" if got[0].isdigit() else "a" if got[0] == "ok" else "r")
                decide(ctx, state, item, reaction_action, f"slack:reaction:{got[1]}")
    mode = state.get("slack_text", "copy")
    if mode == "off":
        return
    sources: list[tuple[object, str | None, str]] = []
    if mode == "copy":
        sources.append((ctx.usr, state.get("copy_channel"), "copy_ts"))
    else:
        sources.append((ctx.bot, state.get("slack_channel"), "text_ts"))
        if ctx.usr is not None and state.get("copy_channel"):
            sources.append((ctx.usr, state.get("copy_channel"), "copy_ts"))
    for client, channel, key in sources:
        if client is None or not channel:
            continue
        with_copy = [i for i in live if i["rounds"][-1]["slack"].get(key)]
        if not with_copy:
            continue
        msgs = (cache or {}).get((id(client), channel)) or _history(
            client, channel, min(i["rounds"][-1]["slack"][key] for i in with_copy))
        for item in with_copy:
            s = item["rounds"][-1]["slack"]
            thread_ts = s[key]
            m = msgs.get(thread_ts)
            latest_key = "copy_latest" if key == "copy_ts" else "text_latest"
            if not m or not m.get("latest_reply") or m["latest_reply"] == s.get(latest_key):
                continue
            replies = client.conversations_replies(channel=channel, ts=thread_ts, limit=200).get("messages", [])
            new = [r for r in replies if r.get("ts") != thread_ts and r.get("ts") not in s["seen"]]
            s["seen"] += [str(r.get("ts")) for r in new]
            s[latest_key] = m["latest_reply"]
            text = "\n\n".join(html.unescape(r.get("text", "")) for r in new if r.get("user") == ctx.owner)
            if not text.strip():
                continue
            if item["status"] in OPEN and item.get("options") and item.get("options_text") != "corrections" \
                    and ends_with_question(text.strip()) and not text_choice(text, len(item["options"])):
                # a question to the agent, not an own option (2026-11-19 rtr-b1g1: choice=0, label = the question)
                to_agent(ctx, state, item, text, "slack:thread-question", chat=ctx.chat_id,
                         response="Передал агенту как вопрос")
                continue
            decide(ctx, state, item, "e", "slack:thread", text)


def slack_groups(ctx: Ctx, states: dict[str, dict]) -> dict:
    """{(id(client), channel): (client, [timestamps], [batch names])} — one history call per channel for every batch."""
    ensure_owner_client(ctx, states)
    groups: dict[tuple[int, str], tuple[object, list[str], list[str]]] = {}
    for name, state in states.items():
        opened = [i for i in state["items"] if i["status"] in OPEN and i["rounds"]]
        if state.get("slack_channel") and opened:
            key = (id(ctx.bot), state["slack_channel"])
            groups.setdefault(key, (ctx.bot, [], []))[1].extend(i["rounds"][-1]["slack"]["ts"] for i in opened)
            groups[key][2].append(name)
        mode = state.get("slack_text", "copy")
        client = ctx.usr if mode == "copy" else ctx.bot
        channel = state.get("copy_channel") if mode == "copy" else state.get("slack_channel")
        if mode != "off" and client and channel:
            key = (id(client), channel)
            key_name = "copy_ts" if mode == "copy" else "text_ts"
            ts = [i["rounds"][-1]["slack"][key_name] for i in state["items"]
                  if i["status"] in OPEN + ("correcting",) and i["rounds"] and i["rounds"][-1]["slack"].get(key_name)]
            if ts:
                groups.setdefault(key, (client, [], []))[1].extend(ts)
                groups[key][2].append(name)
        if mode != "copy" and ctx.usr is not None and state.get("copy_channel"):
            copy_ts = [i["rounds"][-1]["slack"]["copy_ts"] for i in state["items"]
                       if i["status"] in OPEN + ("correcting",) and i["rounds"] and i["rounds"][-1]["slack"].get("copy_ts")]
            if copy_ts:
                ckey = (id(ctx.usr), state["copy_channel"])
                groups.setdefault(ckey, (ctx.usr, [], []))[1].extend(copy_ts)
                groups[ckey][2].append(name)
    return {k: v for k, v in groups.items() if v[1]}


def slack_fetch(ctx: Ctx, groups: dict) -> dict:
    """The network part of a Slack round: history per channel. Reads no state and writes none, so it may run in a
    thread while the main loop keeps answering Telegram taps (2026-10-27: a tap waited for the Slack round)."""
    cache = {}
    for key, (client, timestamps, _names) in groups.items():
        was_owner_client = client is ctx.usr
        result = _history_with_owner_auth(ctx, client, key[1], min(timestamps)) if was_owner_client else _history(
            client, key[1], min(timestamps), sleep=ctx.sleep)
        if result is not None:
            cache[key] = result
            if was_owner_client and ctx.usr is not None and client is not ctx.usr:
                cache[(id(ctx.usr), key[1])] = result
    return cache


def slack_apply(ctx: Ctx, states: dict[str, dict], groups: dict, cache: dict) -> None:
    """Decisions from a fetched Slack round, in the main loop (the only writer of state)."""
    for _client, _ts, names in groups.values():
        for name in set(names):
            if name in states:
                states[name]["history_requests"] = states[name].get("history_requests", 0) + 1
    _fallback_owner_text(ctx, states)
    for state in states.values():
        poll_slack(ctx, state, cache)


def poll_slack_batches(ctx: Ctx, states: dict[str, dict]) -> None:
    """Fetch each owner's DM history once per cycle, spanning every live batch, and act on it (synchronously)."""
    groups = slack_groups(ctx, states)
    slack_apply(ctx, states, groups, slack_fetch(ctx, groups))


def prompt_key(key: str, owner) -> tuple[int, int]:
    """Prompt keys are "<chat>:<message_id>"; telegram.json written before recipients has "<message_id>" (the owner's)."""
    chat, _, mid = str(key).rpartition(":")
    return (int(chat) if chat else int(owner or 0)), int(mid)


def may_answer(ctx: Ctx, state: dict, chat) -> bool:
    return any(p["chat_id"] is not None and int(p["chat_id"]) == int(chat) for p in recipients_of(ctx, state))


def batch_by_key(key: str) -> dict | None:
    """The batch whose callback key this is, read from disk — whatever its state."""
    for name in list_batches():
        if hashlib.sha1(name.encode("utf-8")).hexdigest()[:6] == key:
            try:
                return load_batch(name)
            except (OSError, ValueError):
                return None
    return None


NO_REPLY_HINT = ("Не понял, к какому вопросу. Ответь (свайп по сообщению → «Ответить») на карточку или запрос "
                 "нужного вопроса")


def accept_argument(ctx: Ctx, gstate: dict, state: dict, item: dict, chat, text: str, via: str) -> bool:
    """Record `text` as the argument of the person `chat` for `item`, whichever way it came: the opponent is told, the
    person's open argument request of this question is closed («закрыто: довод получен», Р5 п.5)."""
    disputes = item.get("disputes") or []
    record = (item.get("answers") or {}).get(str(chat)) or \
        (((disputes[-1].get("answers") or {}).get(str(chat))) if disputes else None)
    if record is None or item["status"] not in OPEN:
        tg_send(ctx, chat_id=chat, text=f"{item['id']} · Довод не записан: сначала выбери вариант кнопкой под карточкой")
        return False
    record["argument"] = text
    journal(state, item, "argument", via=via, by=record.get("by"), choice_label=answer_label(item, record),
            argument=text)
    send_argument_notice(ctx, state, item, chat, record)
    consume_prompt(ctx, gstate, "argument_prompts", state, item, chat, "довод получен")
    return True


def log_inbound(ctx: Ctx, update: dict) -> None:
    """One watch.log line per incoming Telegram event: time (warn), chat, message, reply_to, where it was routed and the
    first 80 characters of the text — before 2026-10-28 nobody could tell which message an argument was a reply to."""
    try:
        cq, msg = update.get("callback_query"), update.get("message") or {}
        if cq:
            origin = cq.get("message") or {}
            chat, mid, reply, what = (origin.get("chat") or {}).get("id", (cq.get("from") or {}).get("id")), \
                origin.get("message_id"), None, f"callback={cq.get('data')}"
        else:
            chat, mid, reply = (msg.get("chat") or {}).get("id", (msg.get("from") or {}).get("id")), \
                msg.get("message_id"), (msg.get("reply_to_message") or {}).get("message_id")
            what = "text=" + json.dumps((msg.get("text") or "")[:80], ensure_ascii=False)
        warn(f"inbound chat={chat} msg={mid} reply_to={reply} route={getattr(ctx, 'route', None) or 'ignored'} {what}")
    except Exception as err:  # the log is never worth an answer
        warn(f"inbound log failed: {err}")


def handle_update(ctx: Ctx, gstate: dict, states: dict, update: dict, backlog: bool) -> None:
    ctx.route = None
    try:
        _handle_update(ctx, gstate, states, update, backlog)
    finally:
        log_inbound(ctx, update)


def _handle_update(ctx: Ctx, gstate: dict, states: dict, update: dict, backlog: bool) -> None:
    ao = _ask_owner()
    ctx.telegram_state = gstate
    cq, msg = update.get("callback_query"), update.get("message")
    sender = ((cq or msg or {}).get("from") or {}).get("id")
    known = sender is not None and (sender == ctx.chat_id or any(may_answer(ctx, s, sender) for s in states.values()))
    if not known and sender is not None:  # recipients may reply to a card from a batch that is no longer live
        parts = (cq.get("data") or "").split("|") if cq else []
        disk = batch_by_key(parts[1]) if len(parts) == 5 and parts[0] == "sa" else None
        known = disk is not None and may_answer(ctx, disk, sender)
        if not known and msg:
            for batch_name in list_batches():
                try:
                    disk = load_batch(batch_name)
                except (OSError, ValueError):
                    continue
                if may_answer(ctx, disk, sender):
                    known = True
                    break
    if not known:
        return  # only the owner and the recipients of a batch answer
    by_key = {s["key"]: s for s in states.values()}
    if cq:
        parts = cq.get("data", "").split("|")
        state = by_key.get(parts[1]) if len(parts) == 5 and parts[0] == "sa" else None
        if state is None and len(parts) == 5 and parts[0] == "sa":
            state = batch_by_key(parts[1])  # not live in this cycle (closed, or appeared since): it says «уже: …»
            if state is not None:
                states[state["name"]] = state  # saved with the others after the update
        if state is not None and not may_answer(ctx, state, sender):
            return  # a recipient of another batch: this one is not his to decide
        if state is None:
            ao.cosmetic(ctx.tg, "answerCallbackQuery", callback_query_id=cq["id"], text="Этот вопрос сейчас никто не слушает")
            return
        n = int(parts[2]) - 1
        if n >= len(state["items"]) or int(parts[3]) > state["items"][n]["round"]:
            # the card is newer than this cycle's copy (send goes on in parallel): re-read the state from disk
            state = states[state["name"]] = load_batch(state["name"])
        if n >= len(state["items"]) or not state["items"][n]["rounds"]:
            ao.cosmetic(ctx.tg, "answerCallbackQuery", callback_query_id=cq["id"],
                        text="Пачка ещё отправляется — нажми ещё раз через минуту")
            return
        item = state["items"][n]
        ctx.route = f"button {parts[4]} {state['name']}/{item['n']}"
        if item["status"] == "cancelled":  # its keyboard was not taken away (old cancel, Telegram down): say so, record nothing
            ao.cosmetic(ctx.tg, "answerCallbackQuery", callback_query_id=cq["id"],
                        text=f"Снято: {item.get('cancel_reason') or CANCEL_REASON}")
            old_round = next((r for r in item.get("rounds", []) if r.get("round") == int(parts[3])), None)
            if old_round:
                ids = tg_cards(ctx, old_round["telegram"]).get(int(sender)) or {}
                for mid in [ids.get("card_id"), *(ids.get("extra_ids") or [])]:
                    if mid:
                        _ask_owner().cosmetic(ctx.tg, "editMessageReplyMarkup", chat_id=sender, message_id=mid,
                                              reply_markup={"inline_keyboard": []})
            return
        if int(parts[3]) != item["round"]:
            ao.cosmetic(ctx.tg, "answerCallbackQuery", callback_query_id=cq["id"],
                        text=f"Это раунд {parts[3]}, актуальный — {item['round']}")
            old_round = next((r for r in item.get("rounds", []) if r.get("round") == int(parts[3])), None)
            if old_round:
                ids = tg_cards(ctx, old_round["telegram"]).get(int(sender)) or {}
                for mid in [ids.get("card_id"), *(ids.get("extra_ids") or [])]:
                    if mid:
                        _ask_owner().cosmetic(ctx.tg, "editMessageReplyMarkup", chat_id=sender, message_id=mid,
                                              reply_markup={"inline_keyboard": []})
            return
        # The callback is answered FIRST: behind a slow link (rdsh2→Telegram, Connection reset) an answer sent after
        # the state write and other calls came too late — «400 query is too old», the button kept spinning (2026-10-27).
        # Its text is known up front; a late answer is cosmetic and never costs the tap.
        def ack(toast: str) -> None:
            ao.cosmetic(ctx.tg, "answerCallbackQuery", callback_query_id=cq["id"], text=toast)
        if item["status"] not in OPEN and parts[4] not in ("g", "show", "picktext", "oldtext"):
            ack(f"уже: {STATUS_RU.get(item['status'], item['status'])}")
            disarm(ctx, item)
            return
        if parts[4] == "g":
            if any(v[:2] == [state["name"], item["n"]] and prompt_key(k, ctx.chat_id)[0] == sender
                   for k, v in gstate.setdefault("agent_prompts", {}).items()):
                ack("Запрос агенту уже открыт")
                return
            ack("Напиши агенту")
            prompt = tg_send(ctx, chat_id=sender, text=f"{item_line(state, item)} · напиши агенту, что сделать иначе",
                             reply_markup={"force_reply": True, "input_field_placeholder": "агенту"})
            gstate.setdefault("agent_prompts", {})[f"{sender}:{prompt}"] = [state["name"], item["n"]]
            return
        if parts[4] == "show":
            ack("Показываю карточку")
            card = latest_card(ctx, item, sender)
            if card:
                tg_send(ctx, chat_id=sender, text=f"{item['id']} · актуальная карточка", reply_to_message_id=card,
                        allow_sending_without_reply=True)
            return
        if parts[4] == "picktext":
            pending = gstate.setdefault("pending_text", {}).pop(str(sender), None)
            if pending is None:
                ack("Текст уже обработан")
                return
            prompt = next((v for v in gstate.get("argument_prompts", {}).values()
                           if v[:3] == [state["name"], item["n"], sender]), None)
            reason = next((v for v in gstate.get("reason_prompts", {}).values()
                           if v[:3] == [state["name"], item["n"], sender]), None)
            if prompt:
                record = (item.get("answers") or {}).get(str(sender)) or \
                         (((item.get("disputes") or [])[-1].get("answers") or {}).get(str(sender))
                          if item.get("disputes") else None)
                if record and item["status"] in OPEN:
                    record["argument"] = pending
                    journal(state, item, "argument", via="telegram:text", by=record.get("by"),
                            choice_label=answer_label(item, record), argument=pending)
                    send_argument_notice(ctx, state, item, sender, record)
                    consume_prompt(ctx, gstate, "argument_prompts", state, item, sender, "довод получен")
            elif reason:
                to_agent(ctx, state, item, f"Причина отказа: {pending}", "telegram:rejection-reason",
                         chat=sender, response="Передал агенту как причину отказа")
                consume_prompt(ctx, gstate, "reason_prompts", state, item, sender, "причина получена")
            else:
                to_agent(ctx, state, item, pending, "telegram:free_text", chat=sender)
            ack("Записал")
            return
        if parts[4] == "oldtext":
            stale = gstate.setdefault("stale_text", {})
            key = next((k for k, v in stale.items() if prompt_key(k, ctx.chat_id)[0] == sender and
                        v[:2] == [state["name"], item["n"]]), None)
            if key:
                pending = stale.pop(key)
                item.setdefault("comments", []).append({"by": person_name(ctx, state, sender),
                                                         "text": pending[2], "at": now_iso()})
                journal(state, item, "comment", via="telegram:old-round", by=person_name(ctx, state, sender),
                        text=pending[2])
                to_agent(ctx, state, item, pending[2], "telegram:old-round", chat=sender)
            ack("Записал к актуальному")
            return
        if parts[4] == "e":
            if item["status"] not in OPEN + ("correcting",):
                ack(f"уже: {STATUS_RU.get(item['status'])}")
                return
            what = ("свой вариант" if is_multi(state) and item.get("options") and
                    item.get("options_text") != "corrections" else "правки")
            ack(f"Напиши {what}")
            prompt = tg_send(ctx, chat_id=sender, text=f"{item_line(state, item)} · напиши {what} одним сообщением",
                             reply_markup={"force_reply": True, "input_field_placeholder": what})
            gstate["prompts"][f"{sender}:{prompt}"] = [state["name"], item["n"]]
            return
        if parts[4] == "na":
            prompts = gstate.setdefault("argument_prompts", {})
            for key, value in list(prompts.items()):
                if value[:3] == [state["name"], item["n"], sender]:
                    prompts.pop(key, None)
                    _ask_owner().cosmetic(ctx.tg, "editMessageText", chat_id=sender,
                                          message_id=int(key.split(":", 1)[1]), text="закрыто: без довода")
            answer_record = (item.get("answers") or {}).get(str(sender))
            if answer_record is None:
                for dispute in reversed(item.get("disputes") or []):
                    if str(sender) in dispute.get("answers", {}):
                        answer_record = dispute["answers"][str(sender)]
                        break
            if not answer_record:
                ack("Сначала выбери вариант")
                return
            answer_record["argument"] = None
            journal(state, item, "argument", via="telegram:button", by=answer_record.get("by"),
                    choice_label=answer_label(item, answer_record), argument="без аргумента")
            ack("Без аргумента")
            send_argument_notice(ctx, state, item, sender, answer_record)
            return
        if parts[4] == "m":  # «✏ Изменить ответ»: options back in his chat; his answer stays until a new one
            if not is_multi(state) or item["status"] not in OPEN:
                ack(f"уже: {STATUS_RU.get(item['status'], item['status'])}" if item["status"] not in OPEN else "Нечего менять")
                return
            ack("Выбери заново — прежний ответ записан, пока не выберешь новый")
            set_keyboard(ctx, item, sender, item_buttons(state, item)[0])
            return
        if is_multi(state):
            toast = answer_toast(ctx, state, item, sender, parts[4])
            ack(toast)
            disputes_before = len(item.get("disputes") or [])
            answer(ctx, state, item, sender, parts[4], "telegram:button")
            if (len(item.get("disputes") or []) == disputes_before and item["status"] in OPEN and
                    (disputes_before or item.get("dispute") or parts[4] == "r") and toast and
                    not toast.startswith("уже записано")):
                ask_argument(ctx, gstate, state, item, sender, parts[4])
            return
        decision = decide(ctx, state, item, parts[4], "telegram:button")
        ack(decision)  # the owner alone: as before
        if parts[4] == "r" and item.get("options") and decision == "отклонено":
            instructions = gstate.setdefault("agent_prompts", {})
            for key, value in list(instructions.items()):
                if value[:2] == [state["name"], item["n"]] and prompt_key(key, ctx.chat_id)[0] == sender:
                    instructions.pop(key, None)
                    _ask_owner().cosmetic(ctx.tg, "editMessageText", chat_id=sender,
                                          message_id=int(key.split(":", 1)[1]), text="закрыто: заменён новым запросом")
            prompt = tg_send(ctx, chat_id=sender, text=f"{item['id']} · Что не так? Напиши — уйдёт агенту.",
                             reply_markup={"force_reply": True, "input_field_placeholder": "причина отказа"})
            instructions[f"{sender}:{prompt}"] = [state["name"], item["n"], "rejection-reason", item["round"]]
        return
    if not msg:
        return
    text = (msg.get("text") or "").strip()
    reply_to = (msg.get("reply_to_message") or {}).get("message_id")
    agent = gstate.setdefault("agent_prompts", {})
    mine_agent = {k: prompt_key(k, ctx.chat_id)[1] for k in agent if prompt_key(k, ctx.chat_id)[0] == sender}
    prompts = gstate.setdefault("prompts", {})
    mine_edit = {k: prompt_key(k, ctx.chat_id)[1] for k in prompts if prompt_key(k, ctx.chat_id)[0] == sender}
    newest_edit = max(mine_edit.values(), default=-1)
    newest_agent = max(mine_agent, key=mine_agent.get) if mine_agent else None
    persisted_replies = []
    for batch_name in list_batches():
        try:
            saved = load_batch(batch_name)
        except (OSError, ValueError):
            continue
        for batch_item in saved["items"]:
            for round_ in batch_item.get("rounds") or []:
                for prompt in round_.get("agent_reply_ids") or []:
                    if int(prompt.get("chat_id", -1)) == int(sender):
                        persisted_replies.append((int(prompt["message_id"]), saved, batch_item))
    reply_hit = next((entry for entry in persisted_replies if entry[0] == reply_to), None) if reply_to is not None else None
    newest_persisted = max(persisted_replies, key=lambda entry: entry[0], default=None)
    existing_latest = max([newest_edit, mine_agent.get(newest_agent, -1) if newest_agent else -1])
    if reply_hit or (reply_to is None and newest_persisted and newest_persisted[0] > existing_latest):
        _, target_state, target_item = reply_hit or newest_persisted
        states[target_state["name"]] = target_state
        ctx.route = f"to_agent {target_state['name']}/{target_item['n']}"
        if may_answer(ctx, target_state, sender):
            to_agent(ctx, target_state, target_item, text, "telegram:to_agent", chat=sender)
        else:
            tg_send(ctx, chat_id=sender, text="Не удалось записать указание агенту: пачка недоступна. Напиши агенту напрямую.")
        return
    hit = next((k for k, mid in mine_agent.items() if mid == reply_to), None) if reply_to is not None else (
        newest_agent if newest_agent is not None and mine_agent[newest_agent] > newest_edit else None)
    if hit:
        prompt_value = agent.pop(hit)
        name, n = prompt_value[:2]
        ctx.route = f"to_agent {name}/{n}"
        target_state = states.get(name)
        if target_state is None:
            try:
                target_state = load_batch(name)
            except (OSError, ValueError):
                target_state = None
            if target_state is not None:
                states[name] = target_state
        if target_state is not None and may_answer(ctx, target_state, sender):
            if len(prompt_value) > 2 and prompt_value[2] == "rejection-reason":
                to_agent(ctx, target_state, target_state["items"][int(n) - 1], f"Причина отказа: {text}",
                         "telegram:rejection-reason", chat=sender,
                         response="Передал агенту как причину отказа")
                _ask_owner().cosmetic(ctx.tg, "editMessageText", chat_id=sender,
                                      message_id=int(hit.split(":", 1)[1]),
                                      text="закрыто: причина передана агенту")
            else:
                to_agent(ctx, target_state, target_state["items"][int(n) - 1], text,
                         "telegram:to_agent", chat=sender)
        else:
            tg_send(ctx, chat_id=sender, text="Не удалось записать указание агенту: пачка недоступна. Напиши агенту напрямую.")
        return
    argument_prompts = gstate.setdefault("argument_prompts", {})
    mine_arguments = {k: prompt_key(k, ctx.chat_id)[1] for k in argument_prompts
                      if prompt_key(k, ctx.chat_id)[0] == sender}
    argument_hit = next((k for k, mid in mine_arguments.items() if mid == reply_to), None) if reply_to is not None else None
    if argument_hit:
        prompt_value = argument_prompts[argument_hit]
        batch_name, n, chat = prompt_value[:3]
        prompt_attempt = int(prompt_value[3]) if len(prompt_value) > 3 else 0
        target_state = states.get(batch_name)
        if target_state is None:
            try:
                target_state = states[batch_name] = load_batch(batch_name)
            except (OSError, ValueError):
                target_state = None
        if target_state and may_answer(ctx, target_state, sender):
            target_item = target_state["items"][int(n) - 1]
            ctx.route = f"argument {batch_name}/{n}"
            if target_item["status"] in OPEN and len(target_item.get("disputes") or []) == prompt_attempt:
                accept_argument(ctx, gstate, target_state, target_item, chat, text, "telegram:text")
            else:
                argument_prompts.pop(argument_hit, None)
                tg_send(ctx, chat_id=sender, text="закрыто: вопрос уже решён или запрос заменён")
        else:
            ctx.route = "hint: unknown-reply"
            tg_send(ctx, chat_id=sender, text=NO_REPLY_HINT)
        return
    reason_prompts = gstate.setdefault("reason_prompts", {})
    my_reasons = {k: prompt_key(k, ctx.chat_id)[1] for k in reason_prompts
                  if prompt_key(k, ctx.chat_id)[0] == sender}
    reason_hit = next((k for k, mid in my_reasons.items() if mid == reply_to), None) if reply_to is not None else (
        max(my_reasons, key=my_reasons.get) if len(my_reasons) == 1 else None)
    if reason_hit:
        name, n, chat, _round = reason_prompts.pop(reason_hit)
        ctx.route = f"to_agent {name}/{n} reason"
        target_state = states.get(name) or load_batch(name)
        target_item = target_state["items"][int(n) - 1]
        to_agent(ctx, target_state, target_item, text, "telegram:rejection-reason", chat=chat,
                 response="Передал агенту как причину отказа")
        _ask_owner().cosmetic(ctx.tg, "editMessageText", chat_id=chat,
                              message_id=int(reason_hit.split(":", 1)[1]),
                              text="закрыто: причина передана агенту")
        return
    target = None
    reply_kind = None
    stale_round = None
    edit_hit = next((k for k, mid in mine_edit.items() if mid == reply_to), None) if reply_to is not None else None
    if edit_hit:
        target = prompts.pop(edit_hit)
    elif reply_to is not None:
        for fresh in (False, True):  # second pass: batches being sent, re-read — the card may be newer than this cycle
            if fresh:
                for name in [n for n, s in states.items() if s.get("sending")]:
                    states[name] = load_batch(name)
            for batch_name in list_batches():
                state = states.get(batch_name)
                if state is None:
                    try:
                        state = load_batch(batch_name)
                    except (OSError, ValueError):
                        continue
                for item in state["items"]:
                    for r_index, round_ in enumerate(item.get("rounds") or []):
                        own = tg_cards(ctx, round_["telegram"]).get(int(sender)) or {}
                        if reply_to == own.get("card_id") or reply_to in (own.get("part_ids") or []) + \
                                (own.get("extra_ids") or []) + (own.get("notice_ids") or []):
                            target = [state["name"], item["n"]]
                            if r_index < len(item["rounds"]) - 1:
                                stale_round = round_.get("round")
                                reply_kind = "stale"
                            elif item.get("disputes") and item["status"] in OPEN:
                                reply_kind = "argument"  # any message of a question in disagreement: a reply is an argument
                            else:
                                reply_kind = "card"
                            states[state["name"]] = state
            if target is not None:
                break
    other_requests = any(prompt_key(k, ctx.chat_id)[0] == sender for field in (reason_prompts, prompts) for k in field)
    if reply_to is None and target is None and mine_arguments and not other_requests:
        ctx.route = "hint: no-reply"  # Р5: an argument is only a reply to a message of its question — never guessed
        tg_send(ctx, chat_id=sender, text=NO_REPLY_HINT)
        return
    if reply_to is None and target is None:
        candidates = []
        for key, value in reason_prompts.items():
            if prompt_key(key, ctx.chat_id)[0] == sender:
                candidates.append(("reason", value[0], int(value[1])))
        for key, value in prompts.items():
            if prompt_key(key, ctx.chat_id)[0] == sender:
                candidates.append(("edit", value[0], int(value[1])))
        unique = list(dict.fromkeys(candidates))
        fallback = not unique
        if fallback:
            for saved in states.values():
                if not may_answer(ctx, saved, sender):
                    continue
                for open_item in saved["items"]:
                    if open_item["status"] in OPEN:
                        unique.append(("agent", saved["name"], open_item["n"]))
        if len(unique) == 1 and not fallback and not mine_arguments:  # with a request for an argument open: ask, never guess
            kind, name, n = unique[0]
            target = [name, n]
            reply_kind = kind
        elif unique:
            routes = gstate.setdefault("pending_text", {})
            routes[str(sender)] = text
            buttons = []
            labels = {"edit": "Правки", "reason": "Причина отказа", "agent": "Агенту"}
            for kind, name, n in unique[:20]:
                if name in states:
                    buttons.append([{"text": f"{labels[kind]} · {states[name]['items'][n-1]['id']}",
                                     "callback_data": callback(states[name], states[name]["items"][n-1], "picktext")}])
            tg_send(ctx, chat_id=sender, text="Не понял, к чему это" if fallback else "К какому вопросу?",
                    reply_markup={"inline_keyboard": buttons})
            return
    if target is None:  # a reply to a message of the bot that belongs to no question: never lost silently (Р5 п.3)
        ctx.route = "hint: unknown-reply"
        tg_send(ctx, chat_id=sender, text=NO_REPLY_HINT)
        return
    state = states.get(target[0])
    if state is None or not may_answer(ctx, state, sender):
        return
    item = state["items"][int(target[1]) - 1]
    ctx.route = f"{reply_kind or 'edit'} {state['name']}/{item['n']}" if reply_kind in ("agent", "reason", "edit", "stale") \
        else f"text {state['name']}/{item['n']}"
    if reply_kind == "stale":
        buttons = [[{"text": "Записать к актуальному", "callback_data": callback(state, item, "oldtext")}]]
        prompt = tg_send(ctx, chat_id=sender, text=f"Это раунд {stale_round}, актуальный — {item['round']}. Текст можно перенести кнопкой.",
                         reply_markup={"inline_keyboard": buttons})
        gstate.setdefault("stale_text", {})[f"{sender}:{prompt}"] = [state["name"], item["n"], text]
        return
    if reply_kind == "agent":
        to_agent(ctx, state, item, text, "telegram:free_text", chat=sender)
        return
    if reply_kind == "argument":
        ctx.route = f"argument {state['name']}/{item['n']}"
        accept_argument(ctx, gstate, state, item, sender, text, "telegram:reply")
        return
    if reply_kind == "reason":
        to_agent(ctx, state, item, f"Причина отказа: {text}", "telegram:rejection-reason", chat=sender,
                 response="Передал агенту как причину отказа")
        consume_prompt(ctx, gstate, "reason_prompts", state, item, sender, "причина получена")
        return
    if reply_kind == "edit":
        if item["status"] in OPEN + ("correcting",):
            answer(ctx, state, item, sender, "e", "telegram:text", text)
        return
    if item["status"] not in OPEN + ("correcting",):
        ctx.route = f"to_agent {state['name']}/{item['n']} after-decision"
        note = to_agent(ctx, state, item, f"после решения: {text}", "telegram:after_decision", chat=sender)
        (state.setdefault("to_agent", [])[-1])["after_decision"] = True
        return note
    if item.get("options") and item.get("options_text") != "corrections" and ends_with_question(text) and \
            not text_choice(text, len(item["options"])) and item["status"] in OPEN:
        ctx.route = f"to_agent {state['name']}/{item['n']} question"
        to_agent(ctx, state, item, text, "telegram:question", chat=sender,
                 response="Передал агенту как вопрос")
        return
    if reply_kind == "card":
        ctx.route = f"comment {state['name']}/{item['n']}"
        if is_multi(state):
            record = (item.get("answers") or {}).get(str(sender))
            if record is not None:
                record["comment"] = text
                journal(state, item, "comment", via="telegram:reply", by=record.get("by"),
                        choice_label=answer_label(item, record), text=text)
                for person in recipients_of(ctx, state):
                    if int(person["chat_id"]) != int(sender):
                        tg_send(ctx, chat_id=person["chat_id"], text=f"{item['id']} · {record['by']}: комментарий к ответу «{answer_label(item, record)}»: «{text}»")
            else:
                journal(state, item, "comment", via="telegram:reply", by=person_name(ctx, state, sender), text=text)
        else:
            item.setdefault("comments", []).append({"by": person_name(ctx, state, sender), "text": text, "at": now_iso()})
            journal(state, item, "comment", via="telegram:reply", by=person_name(ctx, state, sender), text=text)
        to_agent(ctx, state, item, f"Комментарий к ответу: {text}", "telegram:comment", chat=sender)
        return
    if item.get("options") and item.get("options_text") != "corrections" and ends_with_question(text) and not text_choice(text, len(item["options"])) and item["status"] in OPEN:
        to_agent(ctx, state, item, text, "telegram:question", chat=sender,
                 response="Передал агенту как вопрос")
    elif item["status"] in OPEN + ("correcting",):
        ctx.route = f"edit {state['name']}/{item['n']}"
        answer(ctx, state, item, sender, "e", "telegram:text", text)
    else:
        tg_send(ctx, chat_id=sender, text=f"{item['id']} · уже: {STATUS_RU.get(item['status'])} — правки не приняты")


def poll_telegram(ctx: Ctx, gstate: dict, states: dict, timeout: int) -> None:
    ao = _ask_owner()
    try:
        updates = ctx.tg("getUpdates", offset=gstate.get("offset") or 0, timeout=timeout,
                         allowed_updates=["message", "callback_query"])
        ctx.tg_failures = 0
    except (ao.TelegramError, OSError) as err:
        code = getattr(err, "code", None)
        if code == 409 or ao.transient(err):  # 409: someone else polls the bot — wait it out, never die
            ctx.tg_failures += 1
            pause = min(60, 5 * 2 ** (ctx.tg_failures - 1))
            warn(f"telegram: {err} — pause {pause}s (#{ctx.tg_failures})")
            ctx.sleep(pause)
            return
        warn(f"telegram: {err} — Telegram off for this watch, Slack goes on")
        ctx.tg = None
        return
    backlog = gstate.get("offset") is None  # first watch ever: old free texts are not answers
    for update in updates:
        gstate["offset"] = update["update_id"] + 1
        try:
            handle_update(ctx, gstate, states, update, backlog)
        finally:
            ao.save_state(BATCH_HOME / "telegram.json", gstate)
            for state in states.values():
                save_batch(state)


STATUS_QUIET_S = 3 * 24 * 60 * 60


def refresh_status(ctx: Ctx, gstate: dict, batches: dict[str, dict]) -> None:
    """Edit one pinned status message per recipient, shared across every batch."""
    if not ctx.tg:
        return
    live = {name: state for name, state in batches.items() if is_live(state)}
    fresh = {name: state for name, state in live.items() if batch_status_fresh(ctx, state)}
    quiet = [state for state in live.values() if not batch_status_fresh(ctx, state) and
             any(item["status"] in OPEN for item in state["items"])]
    recipients = {}
    for state in fresh.values():
        for person in recipients_of(ctx, state):
            recipients[str(person["chat_id"])] = person
    registry = gstate.setdefault("status_messages", {})
    for chat in registry:
        recipients.setdefault(chat, {"chat_id": int(chat)})
    for chat, person in recipients.items():
        quiet_count = sum(1 for state in quiet
                          if chat in {str(p["chat_id"]) for p in recipients_of(ctx, state)})
        rows, buttons = [], []
        waiting_me = waiting_other = 0
        today_done = 0
        for state in fresh.values():
            mine_items = []
            for item in state["items"]:
                if item["status"] == "done":
                    if (item.get("last") or {}).get("at", "")[:10] == now_iso()[:10]:
                        today_done += 1
                    continue
                if item["status"] not in OPEN or not status_item_live(ctx, state, item):
                    continue
                answers = item.get("answers") or {}
                my_answer = answers.get(chat)
                if my_answer:
                    waiting_other += 1
                else:
                    waiting_me += 1
                if item.get("disputes"):
                    rows.append(f"Расхождение: {item['id']}")
                mine_items.append(item)
            if mine_items:
                sent_note = f" · пачка отправлена целиком {state['sent_whole_at']}" if state.get("sent_whole_at") else ""
                rows.append(f"{state.get('title') or state['name']}: ждут тебя {sum(1 for i in mine_items if chat not in (i.get('answers') or {}))}, "
                            f"ждут другого {sum(1 for i in mine_items if chat in (i.get('answers') or {}))}{sent_note}")
                for item in mine_items:
                    buttons.append([{"text": f"Показать {item['id']}",
                                     "callback_data": callback(state, item, "show")}])
        if quiet_count:
            rows.append(f"Давно без ответа: {quiet_count} пачек (show — slack-ask batch show)")
        summary = [f"Ждут тебя: {waiting_me}", f"Ждут другого: {waiting_other}", *rows,
                   f"Решено сегодня: {today_done}"]
        last_done = max(((state.get("title") or state.get("name", ""), (item.get("last") or {}).get("at", ""))
                         for state in batches.values() for item in state["items"] if item["status"] == "done" and
                         recent_status_event(ctx, item)),
                        key=lambda pair: pair[1], default=("", ""))
        empty = f"Открытых вопросов нет · последнее: {last_done[0]}, {last_done[1]}" if last_done[0] else \
            "Открытых вопросов нет"
        text = "Статус вопросов\n" + "\n".join(summary) if rows else empty
        record = registry.get(chat) or {}
        mid = record.get("message_id")
        has_fresh_open = any(item["status"] in OPEN and status_item_live(ctx, state, item)
                              for state in fresh.values() for item in state["items"]
                              if chat in {str(p["chat_id"]) for p in recipients_of(ctx, state)})
        if not has_fresh_open and not mid:
            continue
        if mid:
            if record.get("text") != text:
                try:
                    ctx.tg("editMessageText", chat_id=int(chat), message_id=mid, text=text,
                           reply_markup={"inline_keyboard": buttons})
                except (_ask_owner().TelegramError, OSError) as err:
                    if "message to edit not found" not in str(err).casefold():
                        warn(f"status edit for {chat}: {err}")
                        continue
                    try:
                        mid = tg_send(ctx, chat_id=int(chat), text=text,
                                      reply_markup={"inline_keyboard": buttons})
                        ctx.tg("pinChatMessage", chat_id=int(chat), message_id=mid, disable_notification=True)
                        registry[chat] = {"message_id": mid, "text": text}
                    except Exception as err:
                        warn(f"status recreate for {chat}: {err}")
                else:
                    record["text"] = text
        else:
            try:
                mid = tg_send(ctx, chat_id=int(chat), text=text, reply_markup={"inline_keyboard": buttons})
                registry[chat] = {"message_id": mid, "text": text}
                ctx.tg("pinChatMessage", chat_id=int(chat), message_id=mid, disable_notification=True)
            except Exception as err:
                warn(f"status pin for {chat}: {err}")
    _ask_owner().save_state(BATCH_HOME / "telegram.json", gstate)


def status_item_live(ctx: Ctx, state: dict, item: dict) -> bool:
    """Whether an open item is still within its own timeout and the seven-day no-activity window."""
    rounds = item.get("rounds") or []
    last_round = rounds[-1] if rounds else {}
    sent = last_round.get("sent_epoch")
    timeout = item.get("timeout")
    if sent is not None and timeout is not None and ctx.clock() - float(sent) >= float(timeout):
        return False
    activity = float(sent) if sent is not None else None
    last_at = (item.get("last") or {}).get("at")
    if last_at:
        try:
            activity = max(activity or 0, datetime.datetime.fromisoformat(last_at).timestamp())
        except (TypeError, ValueError, OverflowError):
            pass
    if activity is None:
        try:
            activity = (batch_dir(state["name"]) / "state.json").stat().st_mtime
        except OSError:
            activity = ctx.clock()
    return ctx.clock() - activity < 7 * 24 * 60 * 60


def batch_status_fresh(ctx: Ctx, state: dict) -> bool:
    """Use the last journal event, or creation time for a batch without events, for status recency."""
    at = None
    try:
        journal_path = batch_dir(state["name"]) / "journal.jsonl"
        for line in reversed(journal_path.read_text(encoding="utf-8").splitlines()):
            try:
                at = json.loads(line).get("at")
            except (json.JSONDecodeError, AttributeError):
                continue
            if at:
                break
    except OSError:
        pass
    at = at or state.get("created")
    if not at:
        return True
    try:
        age = ctx.clock() - datetime.datetime.fromisoformat(at).timestamp()
    except (TypeError, ValueError, OverflowError):
        return True
    return age <= STATUS_QUIET_S


def recent_status_event(ctx: Ctx, item: dict) -> bool:
    at = (item.get("last") or {}).get("at")
    if not at:
        return False
    try:
        epoch = datetime.datetime.fromisoformat(at).timestamp()
    except (TypeError, ValueError, OverflowError):
        return False
    return 0 <= ctx.clock() - epoch < 7 * 24 * 60 * 60


SEND_FRESH_S = 600  # a `sending` mark older than this is a send that died; its sent items still wait


def is_sending(state: dict, now: float | None = None) -> bool:
    return bool(state.get("sending")) and (now or time.time()) - float(state.get("sending_at") or 0) < SEND_FRESH_S


def is_live(state: dict) -> bool:
    """~~`not sending and …`: a batch being sent was invisible to watch~~ — 2026-10-27 a tap on the first of 14×2 cards
    was answered «никто не слушает» and dropped while send went on for minutes. Now: a fresh send is live, and items
    already sent wait like any other."""
    return is_sending(state) or any(i["status"] in OPEN + BUSY + WAITING or i.get("hook") for i in state["items"])


def _raise(err: BaseException):
    raise err


def slack_round(ctx: Ctx, run) -> None:
    """One Slack round with the watch's error policy: retries exhausted — pause and say so once; else log."""
    try:
        run()
        if ctx.slack_retry_degraded:
            ctx.slack_retry_degraded = False
            warn("Slack polling recovered")
    except SlackRetryExhausted as err:
        if not ctx.slack_retry_degraded:
            warn(f"Slack polling paused after transient retries: {err}")
        ctx.slack_retry_degraded = True
    except Exception as err:
        warn(f"slack poll: {err!r}")


def slack_cadence(age: float, slack_poll: int) -> int:
    """Seconds between Slack rounds for a card `age` s old. One history call per channel per round is far below the
    rate limit, so it never goes slower than a minute: an owner answers an hour-old card as readily as a fresh one
    (2026-11-19 rtr-b1g1: a 600 s cadence delayed a copy-thread answer by minutes)."""
    return 0 if slack_poll == 0 else (20 if age < 600 else 60)


def start_slack_fetch(ctx: Ctx, states: dict[str, dict]) -> dict:
    """Plan the round now (main loop), fetch in a daemon thread; the result is applied by the main loop."""
    import threading
    job: dict = {"groups": slack_groups(ctx, states)}

    def fetch():
        try:
            job["cache"] = slack_fetch(ctx, job["groups"])
        except BaseException as err:  # re-raised in the main loop under the same policy
            job["error"] = err

    job["thread"] = threading.Thread(target=fetch, name="slack-fetch", daemon=True)
    job["thread"].start()
    return job


def batch_watch(ctx: Ctx, names: list[str] | None = None, timeout: int = 0, slack_poll: int = 20,
                tg_poll: int = 10) -> int:
    """The one poller: Telegram long-poll + Slack history every `slack_poll` s + finished hooks. New batches
    sent meanwhile are picked up (without --batch). Ends by itself when nothing waits (0); 3 on --timeout."""
    ao = _ask_owner()
    if ctx.tg:
        busy = ao.consumer_busy()
        if busy:
            warn(f"the bot is already served by {busy.get('what')} pid {busy.get('pid')} on {busy.get('host')} — "
                 "one watch per bot: `slack-ask batch send` adds batches to it")
            return 4
    gpath = BATCH_HOME / "telegram.json"
    gstate = json.loads(gpath.read_text(encoding="utf-8")) if gpath.exists() else {"offset": None, "prompts": {}}
    ctx.telegram_state = gstate
    t0, last_slack = ctx.clock(), None
    judge_jobs: dict[tuple[str, str], dict] = {}
    # With Telegram on, the Slack round's network part runs in a thread: a tap is handled by the next long-poll, not
    # after two dozen LP batches' history (2026-10-27). Without Telegram (or slack_poll=0, tests) — synchronously.
    background, fetching = bool(ctx.tg) and slack_poll > 0, None
    try:
        while True:
            if ctx.tg:
                ao.claim_consumer({"what": "slack-ask batch watch", "host": socket.gethostname()})
            loaded = {n: load_batch(n) for n in (names or list_batches())}
            refresh_status(ctx, gstate, loaded)
            ensure_owner_client(ctx, loaded)
            if ctx.usr is None:
                for state in loaded.values():
                    if state.get("slack_text") == "copy" and any(i["status"] in OPEN for i in state["items"]):
                        state["owner_auth_failed"] = True
                        switch_to_bot_thread(ctx, state)
                        save_batch(state)
            pending_arguments = {str(v[0]) for field in ("argument_prompts", "reason_prompts", "agent_prompts")
                                 for v in gstate.get(field, {}).values()}
            judge_enabled = os.environ.get("SLACK_ASK_JUDGE") != "agent"
            states = {n: s for n, s in loaded.items() if is_live(s) or n in pending_arguments or
                      (judge_enabled and s.get("judge", "model") == "model" and
                       any(i["status"] == "review" for i in s["items"]))}
            for state in states.values():
                for item in state["items"]:
                    if item.get("dispute_delivery", {}).get("pending_chats"):
                        retry_dispute_cards(ctx, state, item)
            if not states:
                warn("nothing waits — done")
                return 0
            before = {n: json.dumps(s, sort_keys=True) for n, s in states.items()}
            # Model calls never hold the state lock and never block Telegram/Slack polling. Results are applied only
            # by this loop after reloading the batch and checking the answer-set digest.
            for state in states.values():
                if not judge_enabled or state.get("judge", "model") != "model":
                    continue
                for item in state["items"]:
                    if item["status"] != "review":
                        continue
                    key, version = (state["name"], item["id"]), answer_version(item)
                    if (item.get("judge") or {}).get("version") == version:
                        continue
                    if key in judge_jobs:
                        continue
                    job = {"version": version, "done": False}
                    judge_jobs[key] = job
                    state_copy, item_copy = json.loads(json.dumps(state)), json.loads(json.dumps(item))
                    def judge_worker(job=job, state_copy=state_copy, item_copy=item_copy):
                        try:
                            job["result"] = ask_judge(state_copy, item_copy)
                        except Exception as err:
                            job["error"] = f"{type(err).__name__}: {err}"
                        finally:
                            job["done"] = True
                    threading.Thread(target=judge_worker, name="slack-ask-judge", daemon=True).start()
            for key, job in list(judge_jobs.items()):
                if not job["done"]:
                    continue
                judge_jobs.pop(key, None)
                latest = load_batch(key[0])
                target = next((i for i in latest["items"] if i["id"] == key[1]), None)
                if target is None or not judge_result_current(target, job["version"]):
                    continue  # an answer changed while the model was thinking
                if "error" in job:
                    target["judge"] = {"source": "model", "version": job["version"], "error": one_line(job["error"])}
                    journal(latest, target, "сверка моделью не удалась: " + one_line(job["error"], 300))
                    save_batch(latest)
                    continue
                verdict, model_name = job["result"]
                rationale = one_line(str(verdict.get("rationale", "")), 300)
                target["judge"] = {"source": "model", "version": job["version"],
                                    "rationale": rationale, "model": model_name}
                if verdict.get("same") and str(verdict.get("agreed", "")).strip():
                    journal(latest, target, "сверка моделью: одинаково", rationale=rationale, model=model_name)
                    batch_resolve(latest, target["id"], same=str(verdict["agreed"]).strip(), tg=ctx.tg,
                                  chat_id=ctx.chat_id)
                elif verdict.get("same"):
                    target["judge"]["error"] = "модель вернула пустую формулировку agreed"
                    journal(latest, target, "сверка моделью не удалась: пустая формулировка agreed")
                    save_batch(latest)
                else:
                    journal(latest, target, "сверка моделью: расхождение", rationale=rationale, model=model_name)
                    batch_resolve(latest, target["id"], dispute=True, tg=ctx.tg, chat_id=ctx.chat_id)
            for state in states.values():
                expire_items(ctx, state)
            prune_closed_prompts(ctx, gstate, states)
            if ctx.tg:
                poll_telegram(ctx, gstate, states, tg_poll)
            else:
                ctx.sleep(tg_poll)
            if last_slack is None:
                due = True
            else:
                waiting = [i for s in states.values() for i in s["items"] if i["status"] in OPEN and i["rounds"]]
                age = ctx.clock() - max((i["rounds"][-1].get("sent_epoch", ctx.clock()) for i in waiting), default=ctx.clock())
                cadence = slack_cadence(age, slack_poll)
                due = ctx.clock() - last_slack >= cadence
            if fetching is not None and not fetching["thread"].is_alive():
                done, fetching = fetching, None
                slack_round(ctx, lambda: slack_apply(ctx, states, done["groups"], done["cache"])
                            if "error" not in done else _raise(done["error"]))
            if due and fetching is None:
                for state in states.values():
                    try:
                        send_escalations(ctx, state)
                    except Exception as err:
                        warn(f"slack escalation {state['name']}: {err!r}")
                if background:
                    fetching = start_slack_fetch(ctx, states)
                else:
                    slack_round(ctx, lambda: poll_slack_batches(ctx, states))
                last_slack = ctx.clock()
            for state in states.values():
                check_hooks(ctx, state)
            for name, state in states.items():
                if json.dumps(state, sort_keys=True) != before.get(name):
                    save_batch(state)
            if timeout and ctx.clock() - t0 >= timeout:
                return 3
    finally:
        if ctx.tg:
            ao.release_consumer()


TO_AGENT = "↩ Агенту"
CLAUDE_BIN = os.environ.get("SLACK_ASK_CLAUDE", "claude")
WAIT_FRESH_S = 90  # a `batch wait` beats every poll; older than this — nobody is waiting


def wait_heartbeat(name: str, now: float | None = None) -> None:
    """`batch wait` says it is alive: `watch` then leaves an instruction to it instead of resuming the session."""
    (batch_dir(name) / "wait.heartbeat").write_text(str(now if now is not None else time.time()), encoding="utf-8")


def wait_alive(name: str, now: float) -> bool:
    try:
        return now - float((batch_dir(name) / "wait.heartbeat").read_text(encoding="utf-8")) <= WAIT_FRESH_S
    except (OSError, ValueError):
        return False


def to_agent(ctx: "Ctx", state: dict, item: dict, text: str, via: str, chat=None, response: str | None = None) -> str:
    """«↩ Агенту»: the instruction of the owner (or of any recipient of the batch — `by` names who) goes to the agent
    that sent the batch, not to the corrector. A waiting `batch wait` wakes it (exit 7); nobody waiting and the
    session on this host — `claude --bg --resume` continues it in its folder; otherwise it waits for the next `wait`."""
    chat = chat if chat is not None else ctx.chat_id
    duplicate = next((n for n in reversed(state.get("to_agent") or [])
                      if n.get("id") == item["id"] and n.get("round") == item["round"]
                      and str(n.get("chat", chat)) == str(chat) and n.get("text") == text), None)
    if duplicate:
        return f"{item['id']} · уже передал агенту"
    note = {"id": item["id"], "n": item["n"], "round": item["round"], "text": text, "via": via, "at": now_iso(),
            "delivered": None}
    if is_multi(state):
        note["by"], note["chat"] = person_name(ctx, state, chat), chat
    state.setdefault("to_agent", []).append(note)
    origin = state.get("origin") or {}
    if wait_alive(state["name"], ctx.clock()):
        note["delivered"] = "wait"
        reply = f"{item['id']} · Передал агенту — он ждёт ответа и сейчас продолжит."
    elif origin.get("session_id") and origin.get("host") == socket.gethostname():
        d = batch_dir(state["name"])
        k = len(state["to_agent"])
        who = f"{note['by']} (получатель пачки)" if note.get("by") else "Владелец"
        brief = (f"{who} ответил агенту по пачке «{state.get('title', state['name'])}», элемент {item['id']} "
                 f"(раунд {item['round']}): «{text}». Это указание к действию, а не правка текста: пересмотри и "
                 f"выполни, итог — ответом в той же пачке или `ask_owner.py notify`.")
        note["hook"] = ctx.runner.start([CLAUDE_BIN, "--bg", "--resume", origin["session_id"], brief],
                                        d / f"to_agent-{k}.log", d / f"to_agent-{k}.exit", cwd=origin.get("cwd"))
        note["delivered"] = "resume"
        reply = f"{item['id']} · Агент не ждал — продолжил его сессию в фоне ({origin['session_id'][:8]})."
    else:
        note["delivered"] = "pending"
        reply = f"{item['id']} · Указание записано: агент не ждёт, его сессия не на этом хосте — увидит при следующем wait."
    if response:
        reply = f"{item['id']} · {response}"
    journal(state, item, "to_agent", via=via, text=text, delivered=note["delivered"], by=note.get("by"))
    remember_message(state, item, chat, tg_send(ctx, chat_id=chat, text=reply))
    save_batch(state)
    return reply


def batch_reply(state: dict, item_id: str, text: str, tg=None, chat_id=None) -> list[int]:
    """Send an agent clarification as a reply to each recipient's current-round card."""
    item = next((it for it in state["items"] if it["id"] == item_id), None)
    if item is None or not item.get("rounds"):
        raise ValueError(f"{item_id}: no current round")
    if not str(text).strip():
        raise ValueError("reply text is empty")
    if tg is None:
        raise ValueError("Telegram is not configured")
    ctx = tg_only_ctx(tg, chat_id)
    round_ = item["rounds"][-1]
    sent = []
    for person in recipients_of(ctx, state):
        chat = person["chat_id"]
        telegram = round_["telegram"]
        cards = tg_cards(ctx, telegram).get(int(chat)) or {}
        card = cards.get("card_id")
        reply_to = {"reply_to_message_id": card, "allow_sending_without_reply": True} if card else {}
        mid = tg_send(ctx, chat_id=chat, text=f"{item_id} · {text.strip()}", **reply_to)
        if telegram.get("chats"):
            telegram["chats"].setdefault(str(chat), cards).setdefault("extra_ids", []).append(mid)
        else:
            telegram.setdefault("extra_ids", []).append(mid)
        round_.setdefault("agent_reply_ids", []).append({"chat_id": int(chat), "message_id": mid,
                                                           "sent_at": now_iso()})
        sent.append(mid)
    journal(state, item, "agent_reply", via="cli", text=text.strip(), message_ids=sent)
    save_batch(state)
    return sent


def batch_send(ctx: Ctx, spec: dict, name: str) -> dict:
    """Create the batch and show every item. Re-running with the same name resumes an interrupted send.

    spec = {"title": "...", "items": [{
        "id": "BUG-303",                         # first word of every message of the item
        "title": "ревью репорта",                # rest of the first line: "BUG-303 · ревью репорта (k/N, раунд r)"
        "detail": "→ пост в <тред>: …",          # second line
        "now": "…", "ask": "…", "recommendation": "…",   # optional: «что сейчас», the question, the agent's pick —
                                                 # with title, detail, payload and options they go under the spoiler
                                                 # of the disagreement card
        "payload_file": "/abs/note.md" | "payload": "text",   # shown in full; its sha256 is what ✅ approves
        "render": "note" | "text",               # note: YAML stripped exactly as `slack md` does
        "labels": {"approve": "публиковать", "reject": "не публиковать", "done": "Опубликовано"},
        "on_approve": [argv…] | "bash command",  # run after approval/choice if the payload sha256 is unchanged
        "on_corrections": [...],                 # gets {text_file} with the owner's text verbatim; then a new round
        "on_reject": [...],
        "after": "THREAD-1021",                  # run on_approve only after that (earlier) item is done; its result
                                                 # permalink is {after_permalink} — posts into a thread started by it
        "approve_only": true,                    # without an action hook, the owner agrees and the agent acts by hand
        "options": ["A", "B"],                  # 2..9 labels: numbered reactions/buttons and text choices
        "options_text": "corrections",           # optional route for thread text to the corrector
        "vars": {"thread": "…"}}]}               # extra {placeholders}; built-in: id n batch round payload source
                                                 # sha256 text_file text choice choice_label choice_comment choice_file
                                                 # slice state_dir after_permalink
    Text in an `options` item is a choice if it contains an option token; remaining text is choice_comment.
    Without a token it is choice=0 and the exact text is choice_label. `options_text: corrections` overrides this.
    Refused (ValueError, nothing sent): an item with neither on_approve nor approve_only (2026-10-21: nine approved
    items, nothing published); `after` not naming an earlier item; render=note without on_corrections; slack_text other
    than copy without slack_text_reason. An item without on_corrections gets the default corrector
    (DEFAULT_CORRECTOR: owner's text → corrected text → new round); an inline payload is written to a file for it.
    Copy mode without a working owner client raises CopyUnavailable — never a silent switch to bot-thread.

    Several people decide (the rules of ask_owner with .owner-decisions.json): spec "recipients": [{"chat_id": int,
    "name": "…"}] and optional "owner_name"; the owner (owner_chat_id of the bot config) is added first by itself
    (`send --recipients-from <.owner-decisions.json>` fills both from the project file). Allowed only with
    channels telegram-only and only for items with options / approve_only: true without on_approve/on_reject/on_timeout
    — otherwise ValueError, nothing sent. Each recipient gets his own copy of every message; an answer is kept per chat
    (item.answers), the item is decided when all answered the same, a disagreement goes to everyone (item.disputes).
    Item "prefill": [{"chat_id": int, "choice": 1-based} | {"chat_id": int, "text": "own option"}] — answers counted
    in advance (answers[<chat>].prefilled): that recipient gets no card, the others see «<имя> уже ответил: …»; a
    disagreement card reaches everyone, him included. Only with recipients and options, chat among the recipients,
    choice in range, not every recipient (prefill_problems) — otherwise ValueError, nothing sent.
    """
    d = batch_dir(name)
    raw_channels = spec.get("channels")
    if isinstance(raw_channels, dict):
        ch_mode = raw_channels.get("mode")
        ch_delay = raw_channels.get("escalate_after")
    else:
        ch_mode, ch_delay = raw_channels, None
    channels = channel_settings(ch_mode, ch_delay, default="slack-first", default_escalate=0)
    slack_text = spec.get("slack_text", "copy")
    if slack_text not in SLACK_TEXT_MODES:
        raise ValueError("slack_text must be copy, bot-thread, or off")
    if slack_text != "copy" and not str(spec.get("slack_text_reason") or "").strip():
        raise ValueError(f"spec not ready, nothing sent: slack_text={slack_text} needs slack_text_reason — copy (a thread "
                         "in the owner's self-DM, linked from the bot message) is the LP norm; the reason is shown to him")
    first = {"slack-first": "slack", "slack-only": "slack", "telegram-first": "telegram",
             "telegram-only": "telegram"}[channels["mode"]]
    both_now = not channels["mode"].endswith("-only") and channels["escalate_after"] == 0
    initial = ["slack", "telegram"] if both_now else [first]
    if (d / "state.json").exists():
        state = load_batch(name)
    else:
        items = spec.get("items") or []
        ids = [str(i.get("id", "")) for i in items]
        problems = [f"item {n}: no id" for n, i in enumerate(ids, 1) if not i]
        problems += [f"duplicate id {i}" for i in set(ids) if ids.count(i) > 1]
        for it in items:
            if not it.get("payload_file") and "payload" not in it:
                problems.append(f"{it.get('id')}: no payload / payload_file")
            elif it.get("payload_file") and not Path(it["payload_file"]).is_file():
                problems.append(f"{it.get('id')}: payload_file not found: {it['payload_file']}")
            problems += [f"{it.get('id')}: {h} must be an argv list or a string" for h in HOOKS
                         if h in it and not isinstance(it[h], (list, str))]
            # {text_file}/{text} are filled only for corrections; on approve they are empty and
            # `slack md <thread> @{text_file}` becomes `@.` → approve_failed. The approved text is {payload}.
            for h in ("on_approve", "on_reject", "on_timeout"):
                hook = it.get(h)
                flat = " ".join(hook) if isinstance(hook, list) else (hook or "")
                if "{text_file}" in flat or "{text}" in flat:
                    problems.append(f"{it.get('id')}: {h} uses {{text_file}}/{{text}} — empty outside corrections; "
                                    f"the approved snapshot is {{payload}}")
            options = it.get("options")
            if options is not None and (not isinstance(options, list) or not 2 <= len(options) <= 9 or
                                        any(not isinstance(o, str) or not o.strip() for o in options)):
                problems.append(f"{it.get('id')}: options must be a list of 2..9 non-empty strings")
            if it.get("options_text") not in (None, "corrections"):
                problems.append(f"{it.get('id')}: options_text must be 'corrections'")
            approve_only = bool(it.get("approve_only", spec.get("approve_only")))
            if not it.get("on_approve") and not approve_only:
                problems.append(f"{it.get('id')}: no on_approve — ✅ would do nothing; give the action (publish hook), "
                                "or approve_only: true when the agent publishes by hand (the owner is told so)")
            if it.get("on_approve") and it.get("approve_only"):
                problems.append(f"{it.get('id')}: on_approve and approve_only together")
            if it.get("after") and str(it["after"]) not in ids[:ids.index(str(it.get("id", "")))]:
                problems.append(f"{it.get('id')}: after={it['after']} must name an EARLIER item of the batch")
            if not it.get("on_corrections") and it.get("render") == "note":
                problems.append(f"{it.get('id')}: render=note needs on_corrections (note_review.py correct)")
        people = resolve_recipients(spec, ctx.chat_id if ctx.tg else None) if spec.get("recipients") else []
        if people:
            # several people decide only what triggers no action: an action hook is confirmed by the owner alone,
            # and Slack has no second person — so Telegram only
            if channels["mode"] != "telegram-only":
                problems.append(f"recipients (several people decide) need --channels telegram-only, not {channels['mode']}")
            for it in items:
                hooks = [h for h in ("on_approve", "on_reject", "on_timeout") if it.get(h)]
                if hooks or not bool(it.get("approve_only", spec.get("approve_only"))):
                    problems.append(f"{it.get('id')}: recipients (several people decide) are allowed only for items "
                                    f"with options or approve_only: true and no action hook ({', '.join(hooks) or 'not approve_only'})"
                                    " — an action is confirmed by the owner alone")
        problems += prefill_problems(items, people)
        if not items or problems:
            raise ValueError("spec not ready, nothing sent: " + ("; ".join(problems) or "no items"))
        d.mkdir(parents=True, exist_ok=True)
        items = [default_corrector(d, n, it, spec_approve_only=bool(spec.get("approve_only")))
                 for n, it in enumerate(items, 1)]
        judge_mode = "agent" if os.environ.get("SLACK_ASK_JUDGE") == "agent" else spec.get("judge", "model")
        if judge_mode not in ("model", "agent"):
            raise ValueError("judge must be 'model' or 'agent'")
        state = {"name": name, "key": hashlib.sha1(name.encode("utf-8")).hexdigest()[:6], "title": spec.get("title", name),
                 "judge": judge_mode,
                 "created": now_iso(), "owner": ctx.owner, "sending": True,
                 "origin": {"session_id": os.environ.get("CLAUDE_CODE_SESSION_ID"), "cwd": os.getcwd(),
                            "host": socket.gethostname()},
                 "channels": channels, "initial_channels": initial, "slack_text": slack_text,
                 "slack_text_reason": spec.get("slack_text_reason") if slack_text != "copy" else None,
                 "items": [
                     {"n": n, "id": it["id"], "detail": it.get("detail", ""),
                      "now": it.get("now"), "ask": it.get("ask"), "recommendation": it.get("recommendation"),
                      "labels": {**DEFAULT_LABELS, **({"title": it["title"]} if it.get("title") else {}), **it.get("labels", {})},
                      "payload_file": str(Path(it["payload_file"]).resolve()) if it.get("payload_file") else None,
                      "payload": it.get("payload"), "render": it.get("render", "text"),
                      "options": it.get("options"), "options_text": it.get("options_text"),
                      "dispute": bool(it.get("dispute")),
                      "yesno": bool(it.get("yesno")),
                      "notify_only": bool(it.get("notify_only")),
                      "hooks": {h: it[h] for h in HOOKS if it.get(h)}, "vars": it.get("vars", {}),
                      "after": it.get("after"), "approve_only": bool(it.get("approve_only", spec.get("approve_only"))),
                      "timeout": it.get("timeout", spec.get("timeout", DEFAULT_TIMEOUT_S)),
                      "status": "new", "round": 0, "sha256": None, "rounds": [], "hook": None, "queued": [],
                      "result": None, "last": None} for n, it in enumerate(items, 1)]}
        if people:
            state["recipients"] = people
            for it, sitem in zip(items, state["items"]):
                for pre in it.get("prefill") or []:
                    choice = pre.get("choice")
                    sitem.setdefault("answers", {})[str(int(pre["chat_id"]))] = {
                        "action": f"c{int(choice)}" if choice is not None else "choice_text",
                        "text": None if choice is not None else str(pre["text"]).strip(), "comment": None,
                        "by": person_name(ctx, state, pre["chat_id"]), "via": "prefill", "at": now_iso(),
                        "prefilled": True, "argument": str(pre["argument"]).strip() if pre.get("argument") else None}
    if not isinstance(state, Batch):
        state = Batch(state)  # its saves merge with watch's: taps on cards already sent are answered meanwhile
    state.setdefault("channels", channels)
    state.setdefault("judge", os.environ.get("SLACK_ASK_JUDGE", spec.get("judge", "model")))
    state.setdefault("slack_text", slack_text)
    state.setdefault("initial_channels", initial)
    if "slack" in state["initial_channels"] and state["slack_text"] == "copy" and ctx.usr is None \
            and any(not i["rounds"] for i in state["items"]):
        raise CopyUnavailable("the owner's Slack token is missing or rejected — corrections cannot go to a copy. "
                              "Refresh it (laptop: ~/.claude/scripts/rdsh2-creds-sync.sh), or send with "
                              "--slack-text bot-thread --slack-text-reason '<why>' (the owner sees the reason)")
    state["sending"], state["sending_at"] = True, time.time()
    if "slack" in initial and not state.get("slack_channel"):
        state["slack_channel"] = str(ctx.bot.conversations_open(users=ctx.owner)["channel"]["id"])
    if "slack" in initial and state.get("slack_text") == "copy" and ctx.usr and not state.get("copy_channel"):
        try:
            state["copy_channel"] = str(ctx.usr.conversations_open(users=ctx.owner)["channel"]["id"])
        except Exception as err:
            warn(f"no self-DM for text answers: {err}")
    save_batch(state)
    for item in state["items"]:
        if item["rounds"]:
            continue
        item["round"] = 1
        send_round(ctx, state, item, render_payload(item), strict=True)
        state["sending_at"] = time.time()
        journal(state, item, "sent", sha256=item["sha256"], approval_permalink=item["rounds"][-1]["slack"].get("permalink"))
        for a in (item.get("answers") or {}).values():
            if a.get("prefilled"):
                journal(state, item, "answer", via="prefill", by=a["by"], choice_label=answer_label(item, a),
                        argument=a.get("argument"))
        save_batch(state)
    state["sending"] = False
    state["sent_whole_at"] = now_iso()
    save_batch(state)
    return state


def default_corrector(d: Path, n: int, it: dict, spec_approve_only: bool = False) -> dict:
    """The item as sent: without on_corrections it gets DEFAULT_CORRECTOR, which rewrites the payload file in place
    (an inline payload is written to <batch>/payloads/ first). The corrected file is the next round.
    Pure yes/no approve_only questions without an explicit corrector record the owner's text reply as `commented`."""
    if it.get("on_corrections"):
        return it
    if it.get("yesno") and bool(it.get("approve_only", spec_approve_only)) and not it.get("on_approve"):
        return dict(it)
    it = dict(it)
    if not it.get("payload_file"):
        (d / "payloads").mkdir(parents=True, exist_ok=True)
        path = d / "payloads" / f"{n:02d}.md"
        path.write_text(str(it.get("payload", "")), encoding="utf-8")
        it["payload_file"], it["payload"] = str(path), None
    it["on_corrections"] = [sys.executable, str(DEFAULT_CORRECTOR), "--id", "{id}", "--payload", "{source}",
                            "--corrections", "{text_file}"] + (["--check", str(it["check"])] if it.get("check") else [])
    return it


CANCEL_REASON = "снято с голосования"


def cancel_telegram():
    """(call, owner_chat_id) of the bot for `cancel`, or (None, None) without a bot config — Slack is not needed."""
    ao = _ask_owner()
    if not ao.CONFIG.exists():
        warn(f"no Telegram config {ao.CONFIG} — the cards keep their buttons")
        return None, None
    cfg = json.loads(ao.CONFIG.read_text(encoding="utf-8"))
    return ao.http_call(cfg["token"]), cfg["owner_chat_id"]


def batch_cancel(state: dict, ids: list[str] | None, reason: str = CANCEL_REASON, tg=None, chat_id=None) -> list[dict]:
    """Take items off the wait AND off the phones (2026-10-27: «у меня всё ещё есть кнопки у снятых» — cancel changed the
    status only): every Telegram card of the item in every recipient's chat (round card, disagreement cards; the owner
    alone — the old fields) loses its keyboard, and each recipient gets «<id> · снято: <reason>» as a reply to his
    card. Telegram errors (not modified, message to edit not found) are cosmetic."""
    reason = reason or CANCEL_REASON
    ctx = tg_only_ctx(tg, chat_id)  # no Slack client is needed to take buttons away
    done = []
    for item in state["items"]:
        if item["status"] not in OPEN + ("new",) or (ids and item["id"] not in ids):
            continue
        item["status"], item["cancel_reason"] = "cancelled", reason
        close_item_prompts(ctx, state, item, f"снято: {reason}")
        journal(state, item, "cancelled", via="cli", reason=reason)
        done.append(item)
        if not tg or not item["rounds"]:
            continue
        disarm(ctx, item, state, summary=f"✖ Снято: {reason}")
        for person in recipients_of(ctx, state):
            card = latest_card(ctx, item, person["chat_id"])
            reply = {"reply_to_message_id": card, "allow_sending_without_reply": True} if card else {}
            _ask_owner().cosmetic(tg, "sendMessage", chat_id=person["chat_id"], text=f"{item['id']} · снято: {reason}",
                                  disable_web_page_preview=True, **reply)
    save_batch(state)
    return done


def review_line(state: dict, item: dict) -> str:
    """«Антон — «текст или вариант»; Коля — «…»» of an item under the agent's review, recipients in order."""
    answers = item.get("answers") or {}
    people = state.get("recipients") or []
    order = [str(p["chat_id"]) for p in people if str(p["chat_id"]) in answers] or list(answers)
    line = "; ".join(f"{answers[c].get('by') or c} — «{review_text(item, answers[c])}»" for c in order)
    judge = item.get("judge") or {}
    if judge.get("source") == "model" and judge.get("rationale"):
        line += " · сверено моделью: " + one_line(judge.get("rationale", ""), 180)
    return line


def one_line(text: str, limit: int = 180) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def tg_only_ctx(tg, chat_id) -> Ctx:
    """A Ctx with only the Telegram seam (cancel / resolve from the CLI need no Slack client)."""
    ctx = Ctx.__new__(Ctx)
    ctx.tg, ctx.chat_id, ctx.pace, ctx.sleep, ctx.clock, ctx.origin = tg, chat_id, 0.0, time.sleep, time.time, ""
    return ctx


def batch_resolve(state: dict, item_id: str, same: str | None = None, dispute: bool = False, tg=None,
                  chat_id=None, instructed: str | None = None) -> str:
    """The agent's verdict on an item in `review` (own texts that differ in letter): --same "<wording>" — decided,
    `by` all names, via agent-review, everyone gets «✔ Решено: <wording> (names)» and the cards close; --dispute — the
    usual «Расхождение» card to everyone. ValueError when the item is not under review."""
    item = next((i for i in state["items"] if i["id"] == item_id), None)
    if instructed is not None:
        if item is None or state.get("recipients") or item["status"] not in OPEN + ("instructed",):
            raise ValueError(f"{item_id}: --instructed requires an open item in a single-owner batch")
        note = next((n for n in reversed(state.get("to_agent") or []) if n.get("id") == item_id), None)
        wording = instructed.strip() or ((note or {}).get("text") or item.get("result") or "").strip()
        if not wording:
            raise ValueError("--instructed needs a summary or a saved owner instruction")
        item["status"], item["result"] = "instructed", wording
        journal(state, item, "instructed", via="cli", text=wording)
        ctx = tg_only_ctx(tg, chat_id)
        disarm(ctx, item, state, summary=f"✔ По указанию: {wording}")
        if tg and item["rounds"]:
            for person in recipients_of(ctx, state):
                card = latest_card(ctx, item, person["chat_id"])
                reply = {"reply_to_message_id": card, "allow_sending_without_reply": True} if card else {}
                _ask_owner().cosmetic(tg, "sendMessage", chat_id=person["chat_id"],
                                      text=f"{item_id} · записал: {wording}", disable_web_page_preview=True, **reply)
        save_batch(state)
        return f"{item_id} · записал: {wording}"
    if item is None or item["status"] != "review":
        raise ValueError(f"{item_id}: not under review (status {item['status'] if item else 'no such item'}) — "
                         "resolve only answers `wait`'s REVIEW")
    if same is not None and not same.strip():
        raise ValueError("--same needs the wording of the decision")
    ctx = tg_only_ctx(tg, chat_id)
    answers = item.get("answers") or {}
    names = ", ".join(answers[str(p["chat_id"])]["by"] for p in recipients_of(ctx, state) if str(p["chat_id"]) in answers)
    if dispute:
        result = send_dispute(ctx, state, item, "agent-review")
    else:
        item["status"] = "pending"  # decide takes an open item; approve_only: no hook runs
        result = decide(ctx, state, item, "choice_text", "agent-review", text=same.strip(), by=names)
    save_batch(state)
    return result


def batch_show(state: dict) -> str:
    counts: dict[str, int] = {}
    for item in state["items"]:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    lines = [f"batch {state['name']} ({len(state['items'])}): " + " · ".join(f"{STATUS_RU.get(k, k)} {v}" for k, v in counts.items())]
    lines.append(f"history запросов: {state.get('history_requests', 0)}")
    lines.append(f"{'n':>3}  {'id':<12} {'r':>2}  {'status':<15} {'last':<34} result")
    for item in state["items"]:
        last = item.get("last") or {}
        res = item.get("result") or {}
        if not isinstance(res, dict):
            res = {}
        lines.append(f"{item['n']:>3}  {item['id']:<12} {item['round']:>2}  {item['status']:<15} "
                     f"{(last.get('decision', '') + ' ' + last.get('via', '')).strip():<34} "
                     f"{res.get('permalink') or res.get('url') or ''}")
        if item["status"] == "cancelled":
            lines.append(f"       снято: {item.get('cancel_reason') or CANCEL_REASON}")
        if item["status"] == "instructed":
            lines.append(f"       по указанию: {item.get('result') or ''}")
        if item["status"] == "review":
            lines.append(("       на сверке у агента: " if not (item.get("judge") or {}).get("rationale") else
                          "       сверено моделью; на сверке: ") + review_line(state, item))
        elif (item.get("judge") or {}).get("source") == "model":
            lines.append("       сверено моделью: " + one_line(item["judge"].get("rationale", ""), 180))
        people = state.get("recipients") or []
        shown = lambda a, item=item: answer_label(item, a) + \
            (f" «{a['argument']}»" if a.get("argument") else "") + \
            (" (заранее)" if a.get("prefilled") else "")  # noqa: E731
        if len(people) > 1:
            if item.get("by"):
                lines.append(f"       решили: {item['by']} — {item.get('choice_label') or STATUS_RU.get(item['status'], '')}")
            answers = item.get("answers") or {}
            if item["status"] in OPEN or answers:
                lines.append("       ответы: " + "; ".join(
                    f"{p.get('name') or p['chat_id']} — " + (shown(answers[str(p['chat_id'])])
                                                           if str(p["chat_id"]) in answers else "ждём")
                    for p in people))
            for k, dispute in enumerate(item.get("disputes") or [], 1):
                lines.append(f"       расхождение {k} (раунд {dispute.get('round')}): " + "; ".join(
                    f"{a.get('by') or chat} — {shown(a)}; довод: {a.get('argument') or 'без довода'}"
                    for chat, a in dispute["answers"].items()))
        if item.get("comments"):
            lines.append("       комментарии: " + "; ".join(
                f"{c.get('by')}: {c.get('text')}" for c in item["comments"]))
    return "\n".join(lines)


def batch_wait(name: str, ids: list[str] | None, timeout: int, poll: float, clock=time.monotonic,
               sleep=time.sleep) -> int:
    """Wait in the agent's session until the owner decided every chosen item; exit 0 then, 3 on timeout,
    7 when the owner pressed «↩ Агенту» (the instruction is printed once; act, then wait again).

    The bot's `watch` records decisions but wakes nobody: an `approve_only` item approved at 11:03 on 2026-10-23
    sat in the journal while the agent that promised «публикую сам» never learned of it (no hook, no wait). Run this
    in the background right after `send`; it only reads state.json, so it never competes with `watch`."""
    deadline = clock() + timeout if timeout else None
    ack = batch_dir(name) / "agent_ack.json"
    while True:
        wait_heartbeat(name)
        state = load_batch(name)
        seen = int(json.loads(ack.read_text(encoding="utf-8"))["seen"]) if ack.exists() else 0
        fresh = (state.get("to_agent") or [])[seen:]
        if fresh:
            ack.write_text(json.dumps({"seen": seen + len(fresh)}), encoding="utf-8")
            for note in fresh:
                print(f"TO AGENT {note['id']}" + (f" ({note['by']})" if note.get("by") else "") + f": {note['text']}")
            print("→ действуй по указанию, затем снова `slack-ask batch wait` за остальным")
            return 7
        chosen = [i for i in state["items"] if not ids or i["id"] in ids]
        if not chosen:
            print(f"slack-ask batch wait: no item {ids} in batch {name}", file=sys.stderr)
            return 2
        if state.get("owner_auth_failed") and any(i["status"] in OPEN for i in chosen):
            print(f"slack-ask batch wait: пользовательский Slack-токен отклонён в пачке {name}; чтение треда копии невозможно",
                  file=sys.stderr)
            return 7
        review = [i for i in chosen if i["status"] == "review"]
        if review:  # own texts differ in letter: the agent compares the meaning — every wait until resolved
            for i in review:
                print(f"REVIEW {i['id']}: {review_line(state, i)}")
                print(f"→ `slack-ask batch resolve --batch {name} --item {i['id']} --same \"<итог>\"` "
                      "(тот же смысл — итог своими словами) или `--dispute` (разный — карточка «Расхождение» всем)")
            return 7
        if all(i["status"] not in OPEN + BUSY + WAITING + ("new",) for i in chosen):
            for i in chosen:
                last = i.get("last") or {}
                res = i.get("result") or {}
                if not isinstance(res, dict):
                    res = {}
                act = " → действие за агентом (approve_only)" if i.get("approve_only") and i["status"] == "done" else ""
                by = f" · решили: {i['by']}" + (f" — {i['choice_label']}" if i.get("choice_label") else "") \
                    if i.get("by") else ""
                judge = i.get("judge") or {}
                judged = (" · сверено моделью: " + one_line(judge.get("rationale", ""), 180)
                          if judge.get("source") == "model" and judge.get("rationale") else "")
                detail = (f"по указанию: {i.get('result')}" if i["status"] == "instructed"
                          else f"комментарий: {res.get('text')}" if i["status"] == "commented" and res.get("text")
                          else (res.get("permalink") or ""))
                arguments = i.get("answers") or {}
                picks = "; ".join(f"{a.get('by')}: {answer_label(i, a)}; довод: {a.get('argument') or 'без довода'}"
                                   for a in arguments.values())
                history = " | ".join(f"попытка {n}: " + "; ".join(
                    f"{a.get('by')}: {answer_label(i, a)}; довод: {a.get('argument') or 'без довода'}"
                    for a in (d.get("answers") or {}).values()) for n, d in enumerate(i.get("disputes") or [], 1))
                comments = "; ".join(f"{c.get('by')}: {c.get('text')}" for c in i.get("comments") or [])
                print(f"{i['id']} {i['status']} {last.get('decision', '')} {last.get('via', '')} "
                      f"{detail}".rstrip() + by + judged + act +
                      (f" · ответы: {picks}" if picks else "") +
                      (f" · история: {history}" if history else "") +
                      (f" · комментарии: {comments}" if comments else ""))
            return 0
        if deadline is not None and clock() >= deadline:
            print(batch_show(state))
            return 3
        sleep(poll)


def make_ctx(a, telegram: bool = True) -> Ctx:
    bot_env = read_env(Path(os.environ.get("SLACK_BOT_ENV", Path.home() / ".config/lp-agent/slack-bot.env")))
    token = os.environ.get("SLACK_BOT_TOKEN") or bot_env.get("SLACK_BOT_TOKEN")
    if not token:
        raise SystemExit("slack-ask batch: нет SLACK_BOT_TOKEN (~/.config/lp-agent/slack-bot.env)")
    ao = _ask_owner()
    tg = chat_id = None
    if telegram and ao.CONFIG.exists():
        cfg = json.loads(ao.CONFIG.read_text(encoding="utf-8"))
        tg, chat_id = ao.http_call(cfg["token"]), cfg["owner_chat_id"]
    elif telegram:
        warn(f"no Telegram config {ao.CONFIG} — Slack only")
    bot = WebClient(token=token)
    def notify_invalid_bot():
        if tg:
            tg("sendMessage", chat_id=chat_id,
               text="slack-ask: токен бота Slack недействителен; watch остановлен (код 5).")
    auth_test_bot(bot, token, BATCH_HOME / "bot-auth-state.json", notify_invalid_bot)
    env_path = HERE / ".env"
    usr_env = read_env(env_path)
    usr = user_client(usr_env)
    owner_auth_rejected = False
    if usr is not None and getattr(a, "slack_text", None) in (None, "copy"):
        try:
            usr.auth_test()
        except Exception as first_err:
            if not is_auth_error(first_err):
                raise
            usr = user_client(read_env(env_path))  # one reload, as the owner may have rotated the token
            try:
                if usr is not None:
                    usr.auth_test()
                else:
                    raise RuntimeError("owner Slack token disappeared after reload")
            except Exception as second_err:
                if not is_auth_error(second_err):
                    raise
                usr = None
                owner_auth_rejected = True
    try:  # 30 items at once meet Slack's rate limits: wait them out instead of failing the send
        from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler
        for client in (bot, usr):
            if client is not None:
                client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=5))
    except ImportError:
        pass
    ctx = Ctx(bot, usr, tg, chat_id, owner=a.owner, origin=a.origin, pace=0.4)
    ctx.reload_owner_client = lambda: user_client(read_env(env_path))
    def notify_owner_auth():
        text = "slack-ask: пользовательский Slack-токен отклонён повторно; правки переведены в bot-thread."
        if ctx.tg:
            tg_send(ctx, text=text)
        else:
            dm = str(ctx.bot.conversations_open(users=a.owner)["channel"]["id"])
            ctx.bot.chat_postMessage(channel=dm, text=text)
    current_owner_token = usr_env.get("SLACK_XOXC", "")
    ctx.auth_notifier = lambda: auth_notification_once(
        current_owner_token, BATCH_HOME / "owner-auth-state.json", notify_owner_auth) if current_owner_token else None
    if owner_auth_rejected and current_owner_token:
        if auth_notification_once(current_owner_token, BATCH_HOME / "owner-auth-state.json", notify_owner_auth):
            warn("owner Slack auth rejected after one reload; text edits switched to bot-thread")
    return ctx


def batch_cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="slack-ask batch", description="many items, each its own ✅/❌/правки")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send", help="create the batch and show every item (re-run resumes)")
    s.add_argument("spec", type=Path)
    s.add_argument("--batch", required=True)
    w = sub.add_parser("watch", help="the one poller of the bot; ends when nothing waits")
    w.add_argument("--batch", action="append", help="only these batches (default: every live one, new ones too)")
    w.add_argument("--timeout", type=int, default=0, help="seconds; 0 = until every item is answered")
    w.add_argument("--slack-poll", type=int, default=20)
    w.add_argument("--tg-poll", type=int, default=10)
    s.add_argument("--channels", choices=sorted(CHANNEL_MODES))
    s.add_argument("--timeout", type=int, help="default item expiry in seconds")
    s.add_argument("--slack-text", choices=sorted(SLACK_TEXT_MODES))
    s.add_argument("--slack-text-reason", help="required with --slack-text other than copy; shown to the owner")
    s.add_argument("--recipients-from", type=Path, metavar="PATH",
                   help=".owner-decisions.json of the project ({channel, owner_name, recipients}): several people "
                        "decide in Telegram (telegram-only, options/approve_only items only)")
    for p in (s, w):
        p.add_argument("--no-telegram", action="store_true")
        p.add_argument("--owner", default=os.environ.get("SLACK_ASK_OWNER", "U034A9RE6RF"))
        p.add_argument("--origin", default=os.environ.get("SLACK_ASK_ORIGIN", socket.gethostname()))
    sh = sub.add_parser("show")
    sh.add_argument("--batch", required=True)
    sh.add_argument("--json", action="store_true")
    rp = sub.add_parser("reply", help="send an agent clarification to the current round in Telegram")
    rp.add_argument("--batch", required=True)
    rp.add_argument("--item", required=True)
    reply_source = rp.add_mutually_exclusive_group(required=True)
    reply_source.add_argument("--text")
    reply_source.add_argument("--file", type=Path)
    rp.add_argument("--no-telegram", action="store_true")
    wt = sub.add_parser("wait", help="block until the owner decided the items (run in background after send)")
    wt.add_argument("--batch", required=True)
    wt.add_argument("--item", action="append", help="item id (default: every item)")
    wt.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S, help="seconds; 0 = no limit; exit 3 on timeout")
    wt.add_argument("--poll", type=float, default=15)
    c = sub.add_parser("cancel", help="take items off the wait and off the phones: buttons removed for every recipient, reason said (state is merged under lock — a running watch may stay)")
    c.add_argument("--batch", required=True)
    c.add_argument("--item", action="append", help="item id (default: every open item)")
    c.add_argument("--reason", default=CANCEL_REASON, help=f"said to every recipient (default: «{CANCEL_REASON}»)")
    c.add_argument("--no-telegram", action="store_true", help="state only: the cards keep their buttons")
    r = sub.add_parser("resolve", help="the agent's verdict on a review or an owner instruction")
    r.add_argument("--batch", required=True)
    r.add_argument("--item", required=True)
    verdict_arg = r.add_mutually_exclusive_group(required=True)
    verdict_arg.add_argument("--same", metavar="WORDING", help="one meaning: decided with this wording, by everyone")
    verdict_arg.add_argument("--dispute", action="store_true", help="different meaning: «Расхождение» card to everyone")
    verdict_arg.add_argument("--instructed", nargs="?", const="", metavar="SUMMARY",
                             help="record the owner's instruction as the terminal decision")
    r.add_argument("--no-telegram", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "reply":
        try:
            state = load_batch(a.batch)
            message = a.text if a.text is not None else a.file.read_text(encoding="utf-8")
            tg, chat_id = (None, None) if a.no_telegram else cancel_telegram()
            batch_reply(state, a.item, message, tg=tg, chat_id=chat_id)
        except (OSError, ValueError) as err:
            print(f"slack-ask batch reply: {err}", file=sys.stderr)
            return 2
        return 0
    if a.cmd == "resolve":
        state = load_batch(a.batch)
        tg, chat_id = (None, None) if a.no_telegram else cancel_telegram()
        try:
            print(batch_resolve(state, a.item, same=a.same, dispute=a.dispute, tg=tg, chat_id=chat_id,
                                instructed=a.instructed))
        except ValueError as err:
            print(f"slack-ask batch resolve: {err}", file=sys.stderr)
            return 2
        print(batch_show(state))
        return 0
    if a.cmd == "show":
        state = load_batch(a.batch)
        print(json.dumps(state, ensure_ascii=False, indent=1) if a.json else batch_show(state))
        return 0
    if a.cmd == "wait":
        try:
            return batch_wait(a.batch, a.item, a.timeout, a.poll)
        except (OSError, ValueError) as err:
            print(f"slack-ask batch wait: {err}", file=sys.stderr)
            return 2
    if a.cmd == "cancel":
        state = load_batch(a.batch)
        tg, chat_id = (None, None) if a.no_telegram else cancel_telegram()
        batch_cancel(state, a.item, a.reason, tg=tg, chat_id=chat_id)
        print(batch_show(state))
        return 0
    try:
        ctx = make_ctx(a, telegram=not a.no_telegram)
    except BotAuthFailure as err:
        warn(str(err))
        return 5
    if a.cmd == "send":
        try:
            spec = json.loads(a.spec.read_text(encoding="utf-8"))
            if a.channels:
                spec["channels"] = a.channels
            if a.timeout is not None:
                spec["timeout"] = a.timeout
            if a.slack_text:
                spec["slack_text"] = a.slack_text
            if a.slack_text_reason:
                spec["slack_text_reason"] = a.slack_text_reason
            if a.recipients_from:
                try:
                    project = json.loads(a.recipients_from.read_text(encoding="utf-8"))
                except (OSError, ValueError) as err:
                    raise ValueError(f"--recipients-from {a.recipients_from}: {err}") from err
                spec["recipients"] = project.get("recipients") or []
                if project.get("owner_name"):
                    spec["owner_name"] = project["owner_name"]
            state = batch_send(ctx, spec, a.batch)
        except ValueError as err:
            print(f"slack-ask batch: {err}", file=sys.stderr)
            return 6
        except CopyUnavailable as err:
            print(f"slack-ask batch: NOT SENT further — {err}", file=sys.stderr)
            return 7
        print(batch_show(state))
        print(f"state: {batch_dir(a.batch) / 'state.json'}\nnext: nothing — on rdsh2 systemd slack-ask-watch.timer "
              "picks the batch up within a minute")
        if any(i.get("approve_only") for i in state["items"]):
            print(f"⚠ approve_only: ✅ ничего не запустит — агент действует сам. Узнать о решении: в фоне "
                  f"`slack-ask batch wait --batch {a.batch}` (exit 0 — решено, 3 — таймаут)")
        return 0
    return batch_watch(ctx, a.batch, a.timeout, a.slack_poll, a.tg_poll)


if __name__ == "__main__":
    if sys.argv[1:2] == ["verify"]:
        sys.exit(verify(sys.argv[2:]))
    if sys.argv[1:2] == ["batch"]:
        sys.exit(batch_cli(sys.argv[2:]))
    sys.exit(main())
