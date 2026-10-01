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
slack send <target> <text|@file|-> [--thread URL|ts] [--no-split] [--schedule ISO8601] [--no-notify]
slack reply <URL> <text|@file|->  [--no-split] [--schedule ISO8601] [--no-notify]
slack edit  <URL> <text|@file|->
slack delete <URL>
slack react   <URL> :emoji:
slack unreact <URL> :emoji:
slack forward <src> <dest> [--ts TS] [--comment "..."] [--thread URL|ts] [--no-notify]
```

- `<text>` may be inline, `@file` (read file contents), or `-` (stdin).
- Messages over ~3900 chars auto-split into multiple posts (code fences preserved);
  `--no-split` disables.
- Posting to a channel sends you a DM notification with the permalink; suppress with
  `--no-notify`, `SLACK_NOTIFY_CHANNELS=0`, or `~/.config/slack-cli/config.toml`.
- `--schedule` posts later via `chat.scheduleMessage` instead of `chat.postMessage`.
  Accepts ISO-8601 (`2026-07-20T09:00`, `…+03:00`, `…Z`) or a bare unix ts. Output is
  `scheduled_message_id=…` + `post_at=…` (no permalink yet, no self-notify). Manage the
  queue with [`scheduled`](#scheduling).
- `forward` re-shares a message: it fetches the source's permalink (`chat.getPermalink`)
  and posts it into `dest` with `unfurl` on, so Slack renders a preview (the Web API has
  **no** native "share message"). `src` is an archive URL, or a target plus `--ts`;
  `--comment` is prepended above the link.

## Scheduling

```bash
slack scheduled [target] [--cancel SCHEDULED_MESSAGE_ID] [--limit N]
```

- With no `--cancel`, lists pending scheduled messages (`chat.scheduledMessages.list`)
  as `<id> <post-at> <channel> <preview>`; pass a `target` to filter by channel.
- `--cancel <id>` deletes one (`chat.deleteScheduledMessage`); it needs the `target`
  (the channel the message was scheduled to).

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
  thread.
- **`read` renders `blocks`, not the `text` fallback.** A Block Kit message keeps its
  body in `blocks`; `text` is a fallback Slack never displays, and printing it collapsed
  a long post to its first line (measured on a live thread: 54 chars instead of 2454).
  `rich_text` styles come out as mrkdwn (`*bold*`, `_italic_`, `` `code` ``, `~strike~`),
  links as `<url|label>`, lists as `•`/`1.`, `rich_text_preformatted` inside ``` fences
  (styles are *not* applied there), `rich_text_quote` as `> `. `table`/`data_table` are
  still rendered as aligned text. `text` is printed only when there are no blocks — the
  one exception is a table, because `slack table --caption` stores the caption *only* in
  `text`.
- `read --json` prints **exactly one** JSON document on stdout — `json.loads` needs no
  pre-processing. Every diagnostic (including the token-refresh subprocess) goes to
  stderr. `slack listen` is the deliberate exception: it is an NDJSON stream, one
  compact document per line.
- `read` warns on stderr when output hit `--limit` (possible truncation).
- `search` supports combining `--from` and `--in`; an empty result usually means the
  user simply did not post there, not a bug.

## Channels & members

```bash
slack members <target> [--limit N]     # <user-id> <name> per line (id→name resolved)
slack join    <target>                 # conversations.join
slack leave   <target>                 # conversations.leave
slack pin     <URL>                    # pins.add
slack unpin   <URL>                    # pins.remove
slack stats   <target> [--limit 500] [--top 10]
```

- `stats` aggregates `conversations.history` (up to `--limit` messages): top senders,
  messages per day, and the share of messages with files / reactions. `--json` emits the
  full breakdown; the default is terse `sender`/`day` rows plus summary counters.

## Live polling (no RTM)

```bash
slack wait   <target> [--timeout 60] [--interval 3] [--after-ts TS] [--limit N]
             [--from U…|B…] [--while-contains MARKER]
slack listen <target> [--interval 5] [--after-ts TS] [--limit N]
```

- **Browser (`xoxc`) tokens cannot use RTM / Socket Mode** — both commands poll
  `conversations.history`, they do not stream.
- `wait` blocks until a new message appears (prints it, exit 0) or `--timeout` elapses
  (**exit 3**). `--after-ts` sets the baseline; default is the channel's current latest.
- `wait <message URL>` waits **in that thread** (`conversations.replies`); the default baseline
  is the thread's latest reply. `--from` keeps one author (user `U…` or bot `B…` id).
- `--while-contains MARKER` keeps waiting while the newest matching message still contains
  MARKER (text **or** blocks). For bots that edit one message in place — Dr. House shows
  `:processing:` until the result (case 2026-10-01):
  `slack wait <thread URL> --after-ts <call ts> --from U0B2YLD53QU --while-contains ":processing:" --timeout 1800 --interval 30`.
  On timeout stderr says whether nothing arrived or the message is still in progress.

## Thread subscriptions (notifications for agents)

```bash
slack subscribe     <thread URL> [--label PLAT-4731]   # latest reply = seen
slack subscriptions                                     # label, url, seen mark
slack unsubscribe   <URL|label> [...]
slack watch [URL|label ...] [--timeout 6900] [--interval 60] [--follow] [--include-self] [--json]
```

- `watch` polls every subscribed thread (`conversations.replies`) and **exits 0 on the first new
  replies**, printing them under `=== <label>  <url>`. The exit is the notification: an agent runs
  `slack watch` as a background command and is woken when it ends, then runs it again.
- Every reply read advances the thread's seen mark (stored in `~/.cache/slack-cli/subscriptions.json`),
  so subscriptions survive sessions and the next `watch` continues where the last one stopped.
- Own messages advance the mark but do not notify (`--include-self` to change). `--follow` keeps
  printing instead of exiting. Exit 2 — nothing subscribed / unknown target; exit 3 — timeout.
- ⚠ Edits of an existing message are **not** seen (no new `ts`). For a bot that finishes by
  editing its message in place, use `slack wait <URL> --from <id> --while-contains <marker>`.

## People and channels

```bash
slack contacts [--with-dm | --without-dm] [--bots] [--filter S] [--refresh] [--json]
slack invite   <channel> <@name|name|U…-id> [...]
```

- `contacts` — every workspace member with `dm=<D-id>` if a 1:1 DM exists, else `no-dm`
  (`conversations.list types=im` × the users cache). Bots and deleted users are hidden;
  `--bots` shows bots. `--refresh` re-reads `users.list` (needed once so the cache learns `is_bot`).
- `invite` — `conversations.invite`: adds people **or bots** (Dr. House is `U0B2YLD53QU`) to a
  channel. Audited like other writes.
- `listen` polls forever and prints each new message as one NDJSON line
  (`{ts, channel, user, user_name, text}`); Ctrl-C to stop.

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
