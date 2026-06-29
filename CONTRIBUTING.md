# Contributing

Thanks for your interest! This is a small, focused CLI — contributions that keep it
simple and agent-friendly are very welcome.

## Dev setup

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in tokens, or use extract_tokens.py
```

## Tests

Tests are standalone (no pytest, no network, no tokens) and run in CI on every push:

```bash
python test_win_path.py       # path normalization
python test_read_render.py    # table render, --json placement, multi-file upload
```

Please add a test when you fix a bug or add behavior. Keep tests offline — anything
that needs a live Slack call belongs in a manual probe (see `test_blocks.py`), not CI.

## Guidelines

- Match the existing style: terse `key=value` output, `--json` for raw, URL-first input.
- No personal/workspace specifics in code — read config from env/`.env`.
- Keep commands self-documenting (`-h`), and update `docs/` when behavior changes.

## PRs

Small, focused PRs with a clear description. Run the tests locally first. By
contributing you agree your work is licensed under the project's [MIT License](LICENSE).
