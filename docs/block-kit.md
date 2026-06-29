# Block Kit & formatting

Native Block Kit blocks **work with a user token** (`xoxc`/`xoxd`) via
`chat.postMessage`. Confirmed to render: `markdown`, `table`, `data_table`, `card`,
`carousel`. `alert` is modal-only (returns `Unsupported block type` in a message).

## mrkdwn (not Markdown!)

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
- **`@file` + MSYS/Git-Bash**: native Python on Windows can't open the `/c/..` form
  transparently for inline-vs-file ambiguity — if a table comes out `rows=1 cols=1`,
  pipe via stdin instead: `cat f.tsv | slack table '#chan' -`.
- **Message ordering**: `files_upload_v2` shares a file asynchronously, so `slack md`
  waits for the share (polls `files.info`) before posting the next message — otherwise
  a later message can overtake the image.
