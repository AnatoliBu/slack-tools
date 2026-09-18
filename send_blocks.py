#!/usr/bin/env python3
"""
send_blocks.py - Block Kit helpers + sender (user-token xoxc/xoxd). 2026-06.

Used as a LIBRARY by slack_cli.py subcommands `table` / `md` / `blocks`:
  validate_tsv(tsv, kind, src)                 -> rows | raise TableInputError
  parse_blocks_json(text, src)                 -> [block] | raise BlocksInputError
  mrkdwn_warning(text) / mrkdwn_to_markdown(t) -> *x*(mrkdwn bold) vs **x**(Markdown bold)
  build_table_block(tsv, kind, align, caption) -> table | data_table block
  chunk_markdown(text, lim=11000)              -> [ markdown block, ... ]
  split_md_mermaid(md)                         -> [ ("md", text) | ("mermaid", code), ... ]
  find_chromium()                              -> chrome.exe path for puppeteer (or None)
  render_mermaid(code, theme, bg)              -> path to rendered PNG (via mmdc)

Also runnable standalone (own urllib sender; no PNG upload — use `slack md` for that):
  send_blocks.py --channel C... --table @data.tsv [--data] [--caption ..] [--align l,c,r]
  send_blocks.py --channel C... --blocks @blocks.json

Confirmed renders with USER token via chat.postMessage:
  markdown, table, data_table, card, carousel  -> OK
  alert                                         -> modal-only (Unsupported in messages)
  table cells: use raw_text / rich_text (raw_number needs a special shape — skip)

Env: SLACK_XOXC, SLACK_XOXD (auto-loaded from ~/PythonProjects/slack-tools/.env)
Exit codes (standalone): 0=ok, 1=API error, 2=input rejected locally (nothing sent)
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    rc = getattr(_s, "reconfigure", None)
    if rc:
        try:
            rc(encoding="utf-8")
        except OSError:
            pass

ENV_FILE = Path.home() / "PythonProjects" / "slack-tools" / ".env"
MD_LIMIT = 11000  # markdown block text limit (Slack ~12000, reserve)
TABLE_MAX_ROWS = 100  # Block Kit hard limit for table/data_table
TABLE_MAX_COLS = 20
_ALIGN = {"l": "left", "c": "center", "r": "right",
          "left": "left", "center": "center", "right": "right"}


# --------------------------------------------------------------------------
# Input validation (local, BEFORE any Slack call)
# --------------------------------------------------------------------------
# Slack answers every malformed Block Kit payload with the same word:
# `invalid_blocks`. The real reason sits in response_metadata.messages and used
# to be dropped by the CLI, so "uneven rows", ">20 columns", "empty table",
# "empty header cell" and "broken JSON" were indistinguishable — three blind
# sends per diagnosis (measured 2026-08-07). Everything below is decidable from
# the input alone, so it is decided here, with the fix recipe in the message.


class InputError(ValueError):
    """Locally-detected bad input — nothing was sent. CLI maps it to exit 2.

    `.code` is the stable machine-readable reason; `str(e)` is the human/agent
    text: one headline plus `hint:` lines that say what to do next.
    Subclasses ValueError so pre-existing `except ValueError` callers still work.
    """

    def __init__(self, code, message, hints=()):
        self.code = code
        self.headline = message
        # de-dup, order-preserving: per-row diagnostics repeat verbatim when the same
        # cause hit several rows, and three identical paragraphs read as noise
        self.hints = list(dict.fromkeys(h for h in hints if h))
        super().__init__(message + "".join(f"\n  hint: {h}" for h in self.hints))


class TableInputError(InputError):
    """Bad TSV / table shape."""


class BlocksInputError(InputError):
    """Bad raw-blocks JSON."""


_PRINTF_HINT = (
    "printf does NOT expand escapes inside %s arguments — only inside the format "
    "string and inside %b. Fix: printf 'a\\tb\\n' ... (tabs in the format), or "
    "printf '%b\\n' 'a\\tb', or feed a real file"
)
_CAT_A_HINT = "verify the separators: <your-cmd> | cat -A   (a real TAB shows as ^I)"


def _row_reason(line, got, want):
    """Actionable cause for one row whose column count is off — phrased WITHOUT the
    row number, so rows sharing a cause can be grouped into a single hint."""
    if not line.strip():
        return "blank line — it becomes a 1-cell row; drop empty lines"
    if "\\t" in line:
        return ('literal "\\t" inside (backslash + t, not a TAB) — the TSV was probably '
                f"built with printf '%s': {_PRINTF_HINT}")
    if "\t" not in line:
        sep = next((s for s in (";", "|", ",") if s in line), None)
        if sep:
            return (f"no TAB at all but {sep!r} present — this command needs "
                    f"TAB-separated values, not {sep}-separated")
        if "  " in line:
            return ("no TAB at all, only runs of spaces — spaces are not a separator, "
                    "use real TABs")
        return "no TAB at all — this command needs TAB-separated values"
    if line.endswith("\t") and got == want + 1:
        return (f"trailing TAB → an extra empty cell ({got} vs header's {want}); "
                "strip it")
    return f"{got} column(s) vs header's {want}"


def _group_row_hints(bad, want, max_groups=3):
    """[(row_no, line, got)] -> hints, one per distinct cause: `row(s) 2, 3: <cause>`."""
    groups = {}
    for idx, line, got in bad:
        groups.setdefault(_row_reason(line, got, want), []).append(idx)
    hints = []
    for reason, rows in list(groups.items())[:max_groups]:
        shown = ", ".join(str(r) for r in rows[:6])
        more = f" (+{len(rows) - 6} more)" if len(rows) > 6 else ""
        hints.append(f"row{'s' if len(rows) > 1 else ''} {shown}{more}: {reason}")
    return hints


def validate_tsv(tsv_text, kind="table", src=""):
    """TSV text -> list[list[str]] of cells, or raise TableInputError.

    Checks, in order: empty input, header-only input, literal-`\\t` input,
    uneven rows, empty header cell, Block Kit 100x20 limit.
    `src` is the original CLI argument (e.g. `@rows.tsv`) — used only to sharpen
    the message when an `@file` silently fell through as a literal.
    """
    lines = tsv_text.rstrip("\n").splitlines()
    if not lines or not tsv_text.strip():
        raise TableInputError(
            "EMPTY_INPUT", "table input is empty — nothing to post",
            ["did the producing command print anything? " + _CAT_A_HINT,
             "reading a file? pipe it: `cat rows.tsv | slack table <target> -`, "
             "or pass a drive-letter path: `@C:/path/rows.tsv`"])

    rows = [line.split("\t") for line in lines]
    nrows = len(rows)
    ncols = max(len(r) for r in rows)
    literal_tab = any("\\t" in line for line in lines)

    if nrows == 1:
        hints = ["a table needs a header row AND at least one data row; "
                 "got a single line"]
        if ncols == 1:
            looks_like_file = lines[0].startswith("@") or src.startswith("@")
            hints.append(
                "rows=1 cols=1 on non-empty input is the classic symptom of an `@file` "
                "that was never read: on MSYS/Git-Bash the literal argument becomes one "
                "cell. Pipe instead: `cat f.tsv | slack table <target> -`, or use "
                "`@C:/path/f.tsv`" if looks_like_file else
                "the single line has no TAB in it — " + _CAT_A_HINT)
        if literal_tab:
            hints.append(f'the line contains a literal "\\t" — {_PRINTF_HINT}')
        raise TableInputError(
            "SINGLE_ROW",
            f"table input has only 1 row ({ncols} column(s)) — header without data",
            hints)

    if literal_tab and ncols == 1:
        raise TableInputError(
            "LITERAL_TAB",
            f'every one of the {nrows} rows is a single cell containing a literal "\\t" '
            "(backslash + t) — the TAB separators were never expanded",
            [_PRINTF_HINT, _CAT_A_HINT])

    want = len(rows[0])
    bad = [(i + 1, lines[i], len(r)) for i, r in enumerate(rows) if len(r) != want]
    if bad:
        shown = ", ".join(f"row {i}={got}" for i, _, got in bad[:5])
        more = f" (+{len(bad) - 5} more)" if len(bad) > 5 else ""
        raise TableInputError(
            "UNEVEN_ROWS",
            f"uneven table rows: header (row 1) has {want} column(s), but {len(bad)} "
            f"row(s) differ — {shown}{more}. Slack rejects this as "
            "`invalid_blocks` / `uneven_table_rows_not_allowed`",
            _group_row_hints(bad, want) + [_CAT_A_HINT])

    # Zero-length cell -> `invalid_blocks` / "must be more than 0 characters".
    # Measured against the live API 2026-08-07 (probe: header cell, body cell,
    # whole body row, space-only cell): rejection applies to EVERY row, not just
    # the header, and a single SPACE is accepted — so the test is `== ""`, not
    # `.strip()`, otherwise a legitimate " " placeholder would be false-flagged.
    empty = [(r + 1, c + 1) for r, cells in enumerate(rows)
             for c, cell in enumerate(cells) if cell == ""]
    if empty:
        where = ", ".join(f"row {r} col {c}" for r, c in empty[:5])
        more = f" (+{len(empty) - 5} more)" if len(empty) > 5 else ""
        r0, c0 = empty[0]
        hints = ["Slack answers `invalid_blocks` with "
                 f"`[ERROR] must be more than 0 characters [json-pointer:/blocks/0/rows/"
                 f"{r0 - 1}/{c0 - 1}/text]` — measured 2026-08-07, header AND data rows alike",
                 'a single SPACE is accepted: use " ", "-" or "—" for an intentionally '
                 "blank cell"]
        if r0 == 1:
            hints.append(f"row 1 is the header: give column {c0} a name "
                         "(e.g. `Вердикт`, `Status`, `#`) or drop that column everywhere")
        if lines[r0 - 1].endswith("\t"):
            hints.append(f"row {r0} ends with a TAB — a trailing TAB creates an empty "
                         "last cell; strip it")
        raise TableInputError(
            "EMPTY_CELL",
            f"{len(empty)} empty cell(s) — {where}{more}. Block Kit rejects a "
            "zero-length cell",
            hints)

    if nrows > TABLE_MAX_ROWS or ncols > TABLE_MAX_COLS:
        raise TableInputError(
            "LIMIT_EXCEEDED",
            f"table is {nrows}x{ncols} (rows x cols, header included) — Block Kit "
            f"allows at most {TABLE_MAX_ROWS} rows x {TABLE_MAX_COLS} columns; the API "
            "would answer `invalid_blocks`",
            ([f"{nrows} rows > {TABLE_MAX_ROWS}: split into several messages "
              f"(e.g. `head -n {TABLE_MAX_ROWS}`) or post the file: "
              "`slack upload <target> rows.tsv`"] if nrows > TABLE_MAX_ROWS else [])
            + ([f"{ncols} columns > {TABLE_MAX_COLS}: drop columns, or post as "
                "markdown/file instead"] if ncols > TABLE_MAX_COLS else []))
    return rows


def parse_blocks_json(text, src=""):
    """Raw-blocks JSON text -> list[dict], or raise BlocksInputError.

    Same contract as validate_tsv: everything decidable locally is decided here,
    so `slack blocks` never turns a typo into a bare `invalid_blocks`.
    """
    if not text or not text.strip():
        raise BlocksInputError(
            "EMPTY_INPUT", "blocks input is empty — nothing to post",
            ["pass a JSON array: `slack blocks <target> '[{\"type\":\"markdown\","
             "\"text\":\"hi\"}]'`, `@blocks.json` or `-` (stdin)"])
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        preview = text.strip().replace("\n", "\\n")[:120]
        hints = [f"parser said: {e.msg} at line {e.lineno} col {e.colno}",
                 f"input started with: {preview}"]
        if text.strip().startswith("@") or src.startswith("@"):
            hints.append("the argument still looks like `@file` — the file was NOT read "
                         "(missing path?). Pipe it instead: `cat blocks.json | slack "
                         "blocks <target> -`, or use `@C:/path/blocks.json`")
        raise BlocksInputError("INVALID_JSON", "blocks input is not valid JSON", hints)
    blocks = data if isinstance(data, list) else [data]
    if not blocks:
        raise BlocksInputError("EMPTY_INPUT", "blocks input is an empty JSON array",
                               ["at least one block object is required"])
    for i, b in enumerate(blocks):
        if not isinstance(b, dict):
            raise BlocksInputError(
                "BLOCK_NOT_OBJECT",
                f"blocks[{i}] is {type(b).__name__}, expected a JSON object",
                ["a blocks payload is an array of block objects: "
                 '[{"type": "markdown", "text": "..."}, ...]'])
        if not b.get("type"):
            raise BlocksInputError(
                "BLOCK_NO_TYPE", f"blocks[{i}] has no \"type\" field",
                [f"keys present: {', '.join(sorted(b)) or '(none)'}",
                 "every Block Kit block needs a type (markdown, table, data_table, "
                 "card, carousel, section, ...)"])
    return blocks


# --------------------------------------------------------------------------
# Block builders
# --------------------------------------------------------------------------


def build_table_block(tsv_text, kind="table", align="", caption="", src=""):
    """kind: 'table' (compact) | 'data_table' (interactive: filter+fullscreen).
    First TSV line = header. Cells -> raw_text (plain). Tab-separated.
    Raises TableInputError (ValueError) on any locally-detectable defect."""
    cells_rows = validate_tsv(tsv_text, kind, src)
    rows = [[{"type": "raw_text", "text": c} for c in cells] for cells in cells_rows]
    ncols = max(len(r) for r in rows)
    block = {"type": kind, "rows": rows}
    if kind == "data_table":
        block["caption"] = caption or "Table"
    # column_settings (per-column align) — only `table` accepts it; Slack rejects
    # it on `data_table` ("invalid additional property: column_settings").
    if align and kind == "table":
        cs = [{} for _ in range(ncols)]
        for i, a in enumerate(align.split(",")):
            a = a.strip().lower()
            if i < ncols and a in _ALIGN:
                cs[i] = {"align": _ALIGN[a]}
        block["column_settings"] = cs
    return block, (len(rows), ncols)


# --------------------------------------------------------------------------
# mrkdwn vs Markdown (the two syntaxes are NOT the same, and the CLI mixes both)
# --------------------------------------------------------------------------
# `send`/`reply`/`edit` post raw text -> Slack **mrkdwn**: *x* = BOLD, _x_ = italic.
# `md`/`blocks` post a Block Kit `markdown` block -> ordinary **Markdown**:
# *x* = ITALIC, **x** = bold.
# Measured 2026-08-07 by posting `*single* vs **double**` in a markdown block and
# reading the stored message back: Slack normalized it to
#   {"text": "single", "style": {"italic": true}} / {"text": "double", "style": {"bold": true}}
# i.e. mrkdwn-style bold silently collapses to italic — no error, exit 0, and the
# whole visual hierarchy of a report flattens. Hence: detect and warn (and offer
# --mrkdwn to convert), never guess silently.
_CODE_RE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)
# single *span* that is NOT part of **span**, not a `* ` bullet (no space after *)
_MRKDWN_BOLD_RE = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_MD_BOLD_RE = re.compile(r"\*\*(?!\s)[^*\n]+?\*\*")


def _code_split(text):
    """Yield (chunk, is_code) — code fences / inline code must never be rewritten."""
    pos = 0
    for m in _CODE_RE.finditer(text):
        if m.start() > pos:
            yield text[pos:m.start()], False
        yield m.group(0), True
        pos = m.end()
    yield text[pos:], False


def mrkdwn_bold_spans(text):
    """`*spans*` outside code that look like Slack-mrkdwn bold (= Markdown italic)."""
    return [m.group(0) for chunk, is_code in _code_split(text) if not is_code
            for m in _MRKDWN_BOLD_RE.finditer(chunk)]


def has_markdown_bold(text):
    return any(_MD_BOLD_RE.search(chunk) for chunk, is_code in _code_split(text)
               if not is_code)


# --- note-cli: снять YAML-шапку перед постингом -----------------------------------------------
#
# ЗАЧЕМ. Заметка реестра (note-cli: `_docs/bugs/BUG-NNN-*.md` и соседи) — обычный markdown, и её
# постят в Slack КАК ЕСТЬ, без ручной вёрстки. Но YAML-шапка адресована гейтам, а не читателю:
# `schema_version`, `fingerprint`, `body_hash`, `sessions`, `aliases`, `updated_at` — в посте это
# 24 строки мусора перед первой полезной (замер 2026-09-18 на BUG-219), за которыми теряется сам
# дефект. Поэтому она снимается.
#
# ⛔ И ровно на этом роль тула КОНЧАЕТСЯ — шапку поста он НЕ сочиняет (решение владельца
# 2026-09-18, дословно: «надо в изначальном файле чтобы было, а слак тулс просто постил»).
# Заметная строка вида `📌 · BUG-NNN · P1 · server` пишется В САМОЙ ЗАМЕТКЕ первой строкой тела:
# так файл самодостаточен и одинаково читается в git, в Obsidian и в Slack, а тул остаётся тупым
# транспортом. Прежняя редакция здесь собирала шапку из frontmatter — отвергнута: она существовала
# только в момент постинга, и тот же файл в репозитории выглядел иначе, чем у получателя.
#
# Текст без frontmatter или с YAML без note-`id` возвращается как был, без изменений.

_NOTE_ID_RE = re.compile(r"^(BUG|TASK|S|OPENQ|TEST|REQ)-", re.I)

def _fm_scalars(head):
    """Скаляры верхнего уровня из YAML-шапки. Ручной скан: нам нужны четыре поля,
    тащить PyYAML в CLI ради них — цена запуска на каждый пост."""
    out = {}
    for line in head.splitlines():
        m = re.match(r"^([a-z_]+)\s*:\s*(.+?)\s*$", line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip("'\"")
    return out


def note_to_post(md):
    """Заметка note-cli -> (шапка + тело) без YAML. Не заметка -> текст как есть.

    Возвращает (текст, note_id|None) — второй элемент нужен вызывающему, чтобы сказать в
    stderr, что шапка свёрнута, и это не выглядело потерей данных.
    """
    if not md.startswith("---"):
        return md, None
    end = md.find("\n---", 3)
    if end == -1:
        return md, None
    meta = _fm_scalars(md[3:end])
    note_id = meta.get("id", "")
    if not note_id or not _NOTE_ID_RE.match(note_id):
        return md, None

    return md[end + 4:].lstrip("\n"), note_id


def mrkdwn_to_markdown(text):
    """*x* -> **x** outside code spans. Leaves existing **x**, `* ` bullets, code."""
    return "".join(chunk if is_code else _MRKDWN_BOLD_RE.sub(r"**\1**", chunk)
                   for chunk, is_code in _code_split(text))


def mrkdwn_warning(text, convert_flag="--mrkdwn"):
    """Warning string when the text is mrkdwn-flavoured but is going into a
    Block Kit `markdown` block; None when it looks like real Markdown."""
    spans = mrkdwn_bold_spans(text)
    if not spans or has_markdown_bold(text):
        return None
    sample = ", ".join(f"`{s}`" for s in spans[:3])
    more = f" (+{len(spans) - 3} more)" if len(spans) > 3 else ""
    fix = (f"rerun with `{convert_flag}` to convert *x* -> **x**, or write **x** "
           "in the source" if convert_flag else "write **x** for bold in the source")
    return (f"{len(spans)} single-star span(s) — {sample}{more} — will render as "
            "ITALIC, not bold: a Block Kit `markdown` block speaks ordinary Markdown "
            "(bold = **x**), while `slack send/reply/edit` speak Slack mrkdwn "
            "(bold = *x*). Measured 2026-08-07: Slack stores *x* as style.italic.\n"
            f"  hint: {fix}")


_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*(\S.*)?$")


def _fence_open(line, cur):
    """Состояние fence после строки `line`: (marker, info) или None.

    `cur` — состояние ДО строки. Закрывает только маркер того же символа и не
    короче открывающего, без info-строки (правило CommonMark).
    """
    m = _FENCE_RE.match(line.rstrip("\n"))
    if not m:
        return cur
    marker, info = m.group(1), (m.group(2) or "").strip()
    if cur is None:
        return (marker, info)
    open_marker, _ = cur
    if marker[0] == open_marker[0] and len(marker) >= len(open_marker) and not info:
        return None
    return cur


def chunk_markdown(text, lim=MD_LIMIT):
    """Split text into <=lim chunks at line boundaries -> markdown blocks.

    ⚠ Разрез НЕ должен рвать открытый ```code``` fence: Slack рендерит каждый chunk
    отдельным markdown-блоком, поэтому незакрытый fence в хвосте первого блока теряет
    моноширинное оформление, а продолжение во втором блоке едет как обычный текст
    (замер 2026-09-18 на длинной заметке note-cli). Рвём — закрываем fence в текущем
    chunk и открываем заново тем же маркером и info-строкой в следующем.
    """
    chunks, cur, fence = [], "", None
    for line in text.splitlines(keepends=True):
        nxt = _fence_open(line, fence)
        if cur and len(cur) + len(line) > lim:
            if fence:
                marker, info = fence
                cur += ("" if cur.endswith("\n") else "\n") + marker + "\n"
                chunks.append(cur)
                cur = f"{marker}{info}\n" + line
            else:
                chunks.append(cur)
                cur = line
        else:
            cur += line
        fence = nxt
    if cur.strip():
        if fence:
            cur += ("" if cur.endswith("\n") else "\n") + fence[0] + "\n"
        chunks.append(cur)
    return [{"type": "markdown", "text": c.rstrip("\n") or " "} for c in chunks]


def split_md_mermaid(md):
    """Split markdown into ordered segments around ```mermaid fenced blocks."""
    parts, last = [], 0
    pat = re.compile(r"```mermaid[ \t]*\n(.*?)\n?```", re.DOTALL)
    for m in pat.finditer(md):
        pre = md[last:m.start()]
        if pre.strip():
            parts.append(("md", pre))
        parts.append(("mermaid", m.group(1)))
        last = m.end()
    tail = md[last:]
    if tail.strip():
        parts.append(("md", tail))
    return parts or [("md", md)]


# --------------------------------------------------------------------------
# Mermaid -> PNG (mmdc + puppeteer/chromium)
# --------------------------------------------------------------------------


def _natural_key(s):
    """Version-aware sort key: 'win64-99' < 'win64-131' (плоский sorted() даёт обратное,
    т.к. '9' > '1' лексикографически → выбирался НЕ самый новый билд)."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def chromium_candidates():
    """Glob-паттерны браузеров, которые может драйвить puppeteer — от предпочтительных к
    fallback. Вынесено отдельно, чтобы тест мог проверить покрытие без реальной ФС.

    ⚠ Историческая грабля (2026-07): было 4 паттерна, и ТРИ из них на машине с машинным
    (не per-user) Chrome не срабатывали никогда, а единственный рабочий — puppeteer-кэш —
    исчез вместе с каталогом → `slack md` перестал рендерить mermaid, хотя на диске лежало
    три пригодных браузера. Поэтому здесь СУПЕРСЕТ: оба layout'а playwright
    (`chrome-win64` — новый, `chrome-win` — старый), headless-shell'ы, Program Files (x64+x86)
    и Edge. Ищем ШИРОКО: молчаливое «браузер не найден» дороже лишнего glob'а."""
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    j = os.path.join
    return [
        # 1. канонический puppeteer-кэш (то, что ставит `npx puppeteer browsers install`)
        j(home, ".cache", "puppeteer", "chrome", "*", "chrome-win64", "chrome.exe"),
        j(home, ".cache", "puppeteer", "chrome", "*", "chrome-win", "chrome.exe"),
        j(home, ".cache", "puppeteer", "chrome-headless-shell", "*", "*", "chrome-headless-shell.exe"),
        # 2. install-time quirk: `--path /c/Users/...` кладёт под C:\c\Users\...
        "C:/c/Users/*/.cache/puppeteer/chrome/*/chrome-win64/chrome.exe",
        # 3. playwright — ОБА layout'а (новый chrome-win64, старый chrome-win) + headless shell
        j(local, "ms-playwright", "chromium-*", "chrome-win64", "chrome.exe"),
        j(local, "ms-playwright", "chromium-*", "chrome-win", "chrome.exe"),
        j(local, "ms-playwright", "chromium_headless_shell-*", "*", "chrome-headless-shell.exe"),
        # 4. системный Chrome: машинная установка (Program Files) и per-user (LocalAppData)
        j(pf, "Google", "Chrome", "Application", "chrome.exe"),
        j(pf86, "Google", "Chrome", "Application", "chrome.exe"),
        j(local, "Google", "Chrome", "Application", "chrome.exe"),
        # 5. Edge — тот же chromium, годится как последний fallback
        j(pf86, "Microsoft", "Edge", "Application", "msedge.exe"),
        j(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
    ]


def find_chromium():
    """Locate a chromium chrome.exe puppeteer can drive. Returns Windows path or None.
    PUPPETEER_EXECUTABLE_PATH перекрывает автопоиск (тот же контракт, что у
    ~/.claude/scripts/lint-mermaid.py — валидатора mermaid в PostToolUse-хуке)."""
    env = os.environ.get("PUPPETEER_EXECUTABLE_PATH")
    if env and os.path.exists(env):
        return env
    for p in chromium_candidates():
        hits = sorted(glob.glob(p), key=_natural_key)
        if hits:
            return hits[-1]
    return None


def _mmdc_bin():
    return (shutil.which("mmdc") or shutil.which("mmdc.cmd")
            or os.path.expanduser("~/AppData/Roaming/npm/mmdc.cmd"))


def render_mermaid(code, theme="default", bg="white"):
    """Render a mermaid diagram string to a PNG file. Returns the PNG path."""
    d = tempfile.mkdtemp(prefix="mmd_")
    mmd = os.path.join(d, "d.mmd")
    png = os.path.join(d, "d.png")
    cfg = os.path.join(d, "puppeteer.json")
    Path(mmd).write_text(code, encoding="utf-8")
    chrome = find_chromium()
    conf = {"args": ["--no-sandbox"], **({"executablePath": chrome} if chrome else {})}
    Path(cfg).write_text(json.dumps(conf), encoding="utf-8")
    cmd = f'"{_mmdc_bin()}" -i "{mmd}" -o "{png}" -p "{cfg}" -t {theme} -b {bg} -s 2'
    # encoding/errors ОБЯЗАТЕЛЬНЫ: text=True на Windows декодирует вывод как UTF-8, а cmd.exe
    # печатает локализованные ошибки в OEM/CP1251 → UnicodeDecodeError в reader-треде subprocess
    # и потеря текста ошибки ровно тогда, когда он нужнее всего (путь диагностики фейла).
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if not os.path.exists(png):
        tail = (r.stderr or r.stdout or "")[-400:]
        if not chrome:
            # Fail LOUD и с рецептом: без executablePath mmdc уходит в пустой puppeteer-кэш
            # и падает «Could not find Chrome» — раньше это читалось как «диаграмма битая».
            raise RuntimeError(
                "mermaid render failed: браузер для puppeteer НЕ НАЙДЕН.\n"
                "  Искали (glob):\n    " + "\n    ".join(chromium_candidates()) + "\n"
                "  Починка — любое из:\n"
                "    1) PUPPETEER_EXECUTABLE_PATH=<путь к chrome.exe|msedge.exe>\n"
                '    2) npx @puppeteer/browsers install chrome@stable --path "'
                + os.path.join(os.path.expanduser("~"), ".cache", "puppeteer").replace("\\", "/")
                + '"\n'
                f"  mmdc сказал:\n{tail}")
        raise RuntimeError(f"mermaid render failed (chromium={chrome}):\n{tail}")
    return png


# --------------------------------------------------------------------------
# Standalone sender (urllib; no file upload)
# --------------------------------------------------------------------------


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


def _read_src(arg):
    if arg == "-":
        return sys.stdin.read()
    if arg.startswith("@"):
        return Path(arg[1:]).read_text(encoding="utf-8")
    return arg


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--channel", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--blocks", help="JSON array (inline / @file / '-')")
    g.add_argument("--table", dest="table", help="TSV -> table block")
    g.add_argument("--data-table", dest="data_table", help="TSV -> interactive data_table")
    ap.add_argument("--caption", default="")
    ap.add_argument("--align", default="")
    ap.add_argument("--text", default="")
    ap.add_argument("--thread")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        if args.blocks is not None:
            blocks = parse_blocks_json(_read_src(args.blocks), args.blocks)
        elif args.table is not None:
            blk, _ = build_table_block(_read_src(args.table), "table", args.align,
                                       args.caption, args.table)
            blocks = [blk]
        else:
            blk, _ = build_table_block(_read_src(args.data_table), "data_table",
                                       args.align, args.caption, args.data_table)
            blocks = [blk]
    except InputError as e:
        # exit 2 = input validation, nothing sent (same matrix as post_long.py)
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    payload = {"channel": args.channel, "text": args.text or "(blocks)", "blocks": blocks}
    if args.thread:
        payload["thread_ts"] = args.thread
    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    load_env()
    r = post(payload)
    if not r.get("ok"):
        print(f"ERROR: {r.get('error')}", file=sys.stderr)
        for m in r.get("response_metadata", {}).get("messages", []):
            print(f"  {m}", file=sys.stderr)
        return 1
    print(f"ok ts={r.get('ts')}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
