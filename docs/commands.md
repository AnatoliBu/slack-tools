# Command reference

Invoke as `slack <subcommand>` (alias `slack_cli.py`). The global `--json` flag is
accepted **before or after** the subcommand and switches output to raw Slack API
JSON. Default output is terse `key=value` lines on stdout; human hints go to stderr.

## Target resolution

Any command that takes a `target` accepts:

| Form | Meaning |
|------|---------|
| `#name` | channel by name |
| `@name` | user → opens/uses a DM |
| `C…` / `D…` / `G…` | channel / DM / group id |
| `#C…` / `@C…` | id with a stray prefix (prefix is stripped) |
| `<archive URL>` | `https://<ws>.slack.com/archives/C…/p…` — channel + ts (+ thread) parsed for you |
| `-` | last channel/thread from local state cache |

## Posting

```bash
slack send <target> <text|@file|-> [--thread URL|ts] [--no-split] [--no-notify]
slack reply <URL> <text|@file|->  [--no-split] [--no-notify]
slack edit  <URL> <text|@file|->
slack delete <URL>
slack react   <URL> :emoji:
slack unreact <URL> :emoji:
```

- `<text>` may be inline, `@file` (read file contents), or `-` (stdin).
- Messages over ~3900 chars auto-split into multiple posts (code fences preserved);
  `--no-split` disables.
- Posting to a channel sends you a DM notification with the permalink; suppress with
  `--no-notify`, `SLACK_NOTIFY_CHANNELS=0`, or `~/.config/slack-cli/config.toml`.

## Files

```bash
slack upload <target> <file>... [--comment "..."] [--thread URL|ts]
slack download <F-id|url_private_download> [-o PATH]
slack files <target> [--limit N] [--thread] [--no-thread] [--download DIR]
```

- `upload` takes one or many files → a single post with N attachments and a shared
  `--comment`. **Slack cannot attach a file to an already-sent message** — always post
  the file *with* its comment instead of text-then-file.
- `files` lists `<F-id> <size> <type> <title> <msg_url>`. Files usually live in thread
  *replies*, so pass `--thread` to expand the whole thread; `--download DIR` saves all.
- Local paths in MSYS/WSL form (`/c/Users/..`, `/mnt/c/..`, `/tmp/..`) are normalized.

## Reading & search

```bash
slack read <target> [--limit 20] [--thread]      # message / thread / channel history
slack search <query> [--in #chan] [--from @user] [--after YYYY-MM-DD] [--before …] [--limit 20]
slack mentions [--in #chan] [--after …] [--before …] [--limit 20]
slack unread [--types public_channel,private_channel,im,mpim] [--preview]
slack reactions <URL>                            # who reacted with what
slack whoami-msg <URL>                           # author of a message
```

- For a Slack URL, `read` shows that single message; `--thread` expands the whole
  thread. Block Kit `table`/`data_table` are rendered as aligned text.
- `read` warns on stderr when output hit `--limit` (possible truncation).
- `search` supports combining `--from` and `--in`; an empty result usually means the
  user simply did not post there, not a bug.

## Block Kit

```bash
slack table  <target> <TSV:@file|-|inline> [--data] [--caption "..."] [--align l,c,r] [--thread]
slack md     <target> <md:@file|-|inline> [--title "..."] [--theme …] [--bg …] [--thread] [--dry-run]
slack blocks <target> <JSON:@file|-|inline> [--text fallback] [--thread]
```

See [block-kit.md](block-kit.md) for details, limits and pitfalls.

## Discovery / meta

```bash
slack me                                         # your user id / team
slack channels [--filter substr] [--refresh]     # cached channel list
slack users    [--filter substr] [--refresh]     # cached user list
```

Run any subcommand with `-h` for the authoritative flag list.
