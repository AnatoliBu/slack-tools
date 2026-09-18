#!/usr/bin/env bash
# Refresh SLACK_XOXC/SLACK_XOXD in .env from LastPass, without touching a browser.
# Wired via SLACK_TOKEN_REFRESH_CMD in .env — slack_cli.py's _refresh_tokens() runs this
# BEFORE falling back to extract_tokens.py (CDP browser flow).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$ROOT/.env"

_lp() { LPASS_ALLOW_SECRET_OUTPUT=1 lpass show --password "$1" 2>/dev/null | tr -d '\r\n'; }

lpass sync now >/dev/null 2>&1 || true

xoxc=$(_lp 'LP/slack-xoxc')
xoxd=$(_lp 'LP/slack-xoxd')

if [ -z "$xoxc" ] || [ -z "$xoxd" ]; then
    echo "[refresh_tokens_lpass] LP/slack-xoxc или LP/slack-xoxd пусты в LastPass — не могу обновить .env." >&2
    exit 1
fi

tmp="$ENV_FILE.tmp.$$"
{
    grep -v '^SLACK_XOXC=\|^SLACK_XOXD=' "$ENV_FILE" 2>/dev/null || true
    printf 'SLACK_XOXC=%s\n' "$xoxc"
    printf 'SLACK_XOXD=%s\n' "$xoxd"
} > "$tmp"
mv -f "$tmp" "$ENV_FILE"

echo "[refresh_tokens_lpass] .env обновлён из LastPass (LP/slack-xoxc, LP/slack-xoxd)." >&2
