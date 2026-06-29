# Agent guide

Patterns for driving this CLI from an LLM agent (and good practice for humans too).

## I/O contract

- **URL-first**: paste any Slack archive URL into `read`/`reply`/`edit`/`delete`/
  `react`/`reactions`/`whoami-msg` — channel, ts and thread are parsed automatically.
  No need to track channel ids separately.
- **Output**: default `key=value` lines (stable, greppable). Add `--json` (before or
  after the subcommand) for the raw API response.
- **Paths**: MSYS/WSL forms (`/c/Users/..`, `/mnt/c/..`, `/tmp/..`) are normalized to
  native Windows paths — feed them as-is to `upload`, `download -o`, `files --download`
  and `@file` args.
- **No silent truncation**: `read`/`files` print a `[warn]` on stderr when output hit
  `--limit`. Raise `--limit` to see the rest instead of assuming you saw everything.

## Posting discipline

- **Post files with their comment**, never text-then-file: Slack can't attach a file
  to an existing message, so the two-step approach produces two messages / thread noise.
  `slack upload <target> file --comment "..."` is one clean post. Multiple files →
  list them in one call.
- **Fix via edit, not correction spam**: if a sent message is wrong, `slack edit <URL>`
  in place. Only `delete` if it's entirely junk. Do not post a chain of
  "report → correction → correction #2" — gather context first, then send one
  self-contained message.
- **Auto-split is on**: messages over ~3900 chars split automatically (code fences
  preserved). The `text` field of `chat.postMessage`/`chat.update` rejects >4000 chars,
  so let the CLI split rather than truncating.
- **Code-fence discipline**: every ` ``` ` must be closed. An unclosed fence renders
  the rest of the message as one code block.

## Search semantics

- `--from` and `--in` combine correctly, with or without a text term. An empty result
  for `--from @X --in #chan` usually means X didn't post there — not a broken search.
- `read #chan | grep keyword` matches message **text**, not author. To check authorship,
  grep the `--- @user` header line or `search "<exact phrase>"`.

## Authentication & refresh

The CLI uses browser session tokens (`xoxc` + `xoxd`) and acts **as your user**. Both
are required — `xoxc` alone gives `invalid_auth`. The `xoxd` cookie is kept
URL-encoded (decoding it breaks the `Cookie: d=…` header).

Tokens live in `.env` (gitignored) or the environment. On `401`/expiry the CLI tries,
in order:

1. `$SLACK_TOKEN_REFRESH_CMD` — your own command that rewrites `.env` with fresh tokens
   (wire any secret store or browser flow here). For hands-off refresh, use
   [cookie-jar](https://github.com/AnatoliBu/cookie-jar): a browser extension keeps the
   tokens synced into a store, and `refresh_env.py` pulls them into `.env` on demand.
2. `extract_tokens.py` — the bundled Chrome/CDP extractor (uses `$SLACK_WORKSPACE_URL`).
3. otherwise a clear error.

See the project [README](../README.md) for setup.
