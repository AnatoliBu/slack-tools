# Block Kit & formatting

Native Block Kit blocks **work with a user token** (`xoxc`/`xoxd`) via
`chat.postMessage`. Confirmed to render: `markdown`, `table`, `data_table`, `card`,
`carousel`. `alert` is modal-only (returns `Unsupported block type` in a message).

## Two syntaxes — which one depends on the command

| Command | Payload | Bold | Italic |
|---|---|---|---|
| `send` / `reply` / `edit` | raw text → Slack **mrkdwn** | `*bold*` | `_italic_` |
| `md` / `blocks` (`markdown` block) | Block Kit **Markdown** (ordinary) | `**bold**` | `*italic*` |

⚠ They are opposites for `*x*`. Measured 2026-08-07: a `markdown` block containing
`*single* vs **double**` comes back from the API as `style.italic` / `style.bold` —
mrkdwn-style bold silently degrades to italic, with **no error and exit 0**. `slack md`
warns on stderr when the source looks like mrkdwn (single `*…*` spans and no `**…**`)
and can convert it: `slack md '#chan' @report.md --mrkdwn` (`*x*` → `**x**`, code
fences / inline code / `* ` bullets untouched).

## mrkdwn (`send` / `reply` / `edit`)

Slack uses its own `mrkdwn`, not standard Markdown:

```
<URL|alt text>      link with text   (single angle brackets!)
<URL>               bare link
<@U012ABC>          user mention
<#C012ABC|name>     channel mention
*bold*  _italic_  ~strike~  `code`
```

Does **not** work: `[text](URL)` (Markdown links), `<<URL|text>>` (double brackets
break the parser — a space inside the alt text truncates the URL).

`slack edit` posts with `parse="none"`, so `<…>` are preserved, not escaped.

## table / data_table

```bash
# TSV, first line = header. --data → interactive data_table (filter + fullscreen)
printf 'Client\tState\tResult\n37085796\t2\t401\n' | slack table '#chan' - --align l,c,r
slack table '#chan' @rows.tsv --data --caption "Summary"
```

- Limits: ≤100 rows, ≤20 columns. Cells are plain text (no mrkdwn inside).
- `--align l,c,r` → per-column left/center/right. **Only `table` accepts column
  alignment**; Slack rejects `column_settings` on `data_table`.
- `read` renders these back to aligned text, so posted tables stay readable.
- **No empty cells anywhere** — a zero-length cell is rejected (`must be more than 0
  characters`), in the header *and* in data rows (measured 2026-08-07). A single space
  `" "` is accepted, so use `" "`, `-` or `—` for an intentionally blank cell.

### The input is validated locally first (exit 2, nothing sent)

Slack answers every malformed payload with the single word `invalid_blocks`, so the CLI
decides everything decidable from the input itself and prints the cause + fix:

| Code | Fires when |
|---|---|
| `EMPTY_INPUT` | nothing on stdin / empty file |
| `SINGLE_ROW` | header without data rows (incl. the `rows=1 cols=1` unread-`@file` symptom) |
| `LITERAL_TAB` | every row is one cell containing a literal `	` |
| `UNEVEN_ROWS` | a row's column count ≠ header's — names the rows, the counts and the likely cause |
| `EMPTY_CELL` | a zero-length cell — names `row N col M` |
| `LIMIT_EXCEEDED` | >100 rows or >20 columns — names the actual size |
| `FILE_NOT_READ` | `@file` that does not exist (was silently posted as literal text before) |

The classic producer bug it catches: `printf 'ID	P
%s
' 'a	b'` — **printf does not
expand escapes inside `%s` arguments**, only in the format string and in `%b`, so the
data rows arrive as one cell with literal `	` while the header has real TABs. Check any
producer with `<cmd> | cat -A` (a real TAB shows as `^I`).

## markdown + mermaid → PNG

```bash
slack md '#chan' @report.md                 # flat: segments posted in sequence
slack md '#chan' @report.md --title "*TL;DR*"  # announce + thread the whole doc under it
slack md '#chan' @report.md --thread <URL>  # into an existing thread
slack md '#chan' @report.md --dry-run --json   # segment plan, no send/render
```

Each ` ```mermaid ` fence is rendered to a PNG and posted inline as a separator
(`[md] → [PNG] → [md]`). `markdown` blocks over ~12000 chars are auto-chunked.

Requires `mmdc` (`@mermaid-js/mermaid-cli`) + a Chromium for Puppeteer. The renderer
auto-discovers a browser (`~/.cache/puppeteer`, Playwright, etc.). Install one with:

```bash
npx @puppeteer/browsers install chrome@stable --path "C:/Users/<you>/.cache/puppeteer"
```

(use forward slashes on Windows).

## raw blocks

```bash
echo '[{"type":"markdown","text":"**hi**"}]' | slack blocks '#chan' - --text fallback
```

Pass a JSON array (`@file` / `-` / inline). `--text` sets the notification fallback.

## Pitfalls

- **Prefer `slack table` over hand-built ASCII tables** in a code fence — long cells
  wrap and break alignment; native tables render exactly.
- **`@file` + MSYS/Git-Bash**: an `@file` that cannot be opened used to fall through as
  literal text (symptom: `rows=1 cols=1`). `table`/`md`/`blocks` now fail loud with
  `FILE_NOT_READ` instead — pipe via stdin (`cat f.tsv | slack table '#chan' -`) or pass
  a drive-letter path (`@C:/path/f.tsv`).
- **`invalid_blocks` is never printed alone**: `response_metadata.messages` (e.g.
  `[ERROR] uneven_table_rows_not_allowed [json-pointer:/blocks/0/]`) now goes to stderr
  on every error path, not only under `--json`.
- **Message ordering**: `files_upload_v2` shares a file asynchronously, so `slack md`
  waits for the share (polls `files.info`) before posting the next message — otherwise
  a later message can overtake the image.
