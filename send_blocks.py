#!/usr/bin/env python3
"""
send_blocks.py - Block Kit helpers + sender (user-token xoxc/xoxd). 2026-06.

Used as a LIBRARY by slack_cli.py subcommands `table` / `md` / `blocks`:
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
_ALIGN = {"l": "left", "c": "center", "r": "right",
          "left": "left", "center": "center", "right": "right"}

# --------------------------------------------------------------------------
# Block builders
# --------------------------------------------------------------------------


def build_table_block(tsv_text, kind="table", align="", caption=""):
    """kind: 'table' (compact) | 'data_table' (interactive: filter+fullscreen).
    First TSV line = header. Cells -> raw_text (plain). Tab-separated."""
    rows = []
    for line in tsv_text.rstrip("\n").splitlines():
        cells = [{"type": "raw_text", "text": c} for c in line.split("\t")]
        if cells:
            rows.append(cells)
    if not rows:
        raise ValueError("empty table input")
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


def chunk_markdown(text, lim=MD_LIMIT):
    """Split text into <=lim chunks at line boundaries -> markdown blocks."""
    chunks, cur = [], ""
    for line in text.splitlines(keepends=True):
        if cur and len(cur) + len(line) > lim:
            chunks.append(cur)
            cur = line
        else:
            cur += line
    if cur.strip():
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

    if args.blocks is not None:
        blocks = json.loads(_read_src(args.blocks))
        blocks = blocks if isinstance(blocks, list) else [blocks]
    elif args.table is not None:
        blk, _ = build_table_block(_read_src(args.table), "table", args.align, args.caption)
        blocks = [blk]
    else:
        blk, _ = build_table_block(_read_src(args.data_table), "data_table", args.align, args.caption)
        blocks = [blk]

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
