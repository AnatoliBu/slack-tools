#!/bin/sh
# Тесты slack-ask только в песочнице: HOME во временном каталоге, tmux/claude — заглушки (exit 97).
# Реальные ask_owner.py / correct_payload.py читаются только как код (ASK_OWNER_PY, SLACK_ASK_CORRECTOR).
set -eu
REAL_HOME="$HOME"
PY="$(command -v python3)"
SB="$(mktemp -d)"
mkdir -p "$SB/bin" "$SB/home"
for t in tmux claude; do printf '#!/bin/sh\nexit 97\n' > "$SB/bin/$t"; chmod +x "$SB/bin/$t"; done
USERSITE="$("$PY" -c 'import site;print(site.getusersitepackages())')"
cd "$(dirname "$0")"
env HOME="$SB/home" PATH="$SB/bin:$PATH" PYTHONPATH="$USERSITE${PYTHONPATH:+:$PYTHONPATH}" \
    ASK_OWNER_PY="$REAL_HOME/.claude/skills/owner-decisions/scripts/ask_owner.py" \
    SLACK_ASK_CORRECTOR="$REAL_HOME/.claude/skills/owner-decisions/scripts/correct_payload.py" \
    "$PY" -m pytest -q "${@:-test_slack_ask.py}"
