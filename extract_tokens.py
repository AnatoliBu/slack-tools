"""
Slack token extractor via Chrome CDP.

Launches a dedicated Chrome instance with its own profile,
connects via CDP, extracts xoxc + xoxd tokens from Slack.

The profile persists Slack login between runs — no need to
re-authenticate each time (unless session expires in Slack).

Requirements:
  - pip install websockets

Usage:
    python extract_tokens.py https://your-workspace.slack.com
    # or set SLACK_WORKSPACE_URL and run without an argument
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent

# CDP config — dedicated port that won't conflict with anything
CDP_PORT = 9333
CDP_BASE = f"http://127.0.0.1:{CDP_PORT}"

# Chrome binary
CHROME_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

# Profile candidates for extracting tokens.
# Each is (user-data-dir, profile-directory, description).
# Tried in order; first one that isn't locked wins.
PROFILES = [
    # Dedicated profile for this script — always first choice
    (str(SCRIPT_DIR / "chrome-profile"), "Default", "slack-tools dedicated"),
    # Main Chrome profile (usually locked by running Chrome)
    (str(Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data"), "Default", "Chrome main"),
]

chrome_process = None


def cdp_get(path: str):
    """HTTP GET to CDP endpoint."""
    try:
        return json.loads(urllib.request.urlopen(f"{CDP_BASE}{path}", timeout=5).read())
    except Exception:
        return None


def is_profile_locked(user_data_dir: str) -> bool:
    """Check if a Chrome profile is locked by another process."""
    lockfile = Path(user_data_dir) / "lockfile"
    if not lockfile.exists():
        return False
    try:
        # Try to open exclusively — if fails, it's locked
        lockfile.open("r+b").close()
        return False
    except (PermissionError, OSError):
        return True


def launch_chrome(user_data_dir: str, profile_dir: str) -> bool:
    """Launch Chrome with CDP on our dedicated port."""
    global chrome_process
    cmd = [
        CHROME_EXE,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={user_data_dir}",
        f"--profile-directory={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    print(f"  Запускаю Chrome (порт {CDP_PORT})...")
    chrome_process = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    for _ in range(20):
        time.sleep(1)
        if cdp_get("/json/version"):
            return True
        # Check if process died
        if chrome_process.poll() is not None:
            return False
    return False


def check_cdp() -> dict:
    """Find or launch Chrome with CDP. Try profiles in order."""
    # Maybe CDP is already up on our port
    version = cdp_get("/json/version")
    if version:
        print(f"  Chrome уже доступен: {version['Browser']}")
        return version

    # Try each profile
    for user_data, profile, desc in PROFILES:
        if not Path(user_data).exists() and desc != "slack-tools dedicated":
            continue
        if is_profile_locked(user_data):
            print(f"  [{desc}] профиль залочен, пропускаю")
            continue
        print(f"  [{desc}] пробую {user_data[:50]}...")
        # Create dir for dedicated profile
        Path(user_data).mkdir(parents=True, exist_ok=True)
        if launch_chrome(user_data, profile):
            version = cdp_get("/json/version")
            if version:
                print(f"  Chrome {version['Browser']} — CDP OK")
                return version
        print(f"  [{desc}] не удалось запустить")

    print("  ОШИБКА: не удалось запустить Chrome ни с одним профилем.")
    print("  Закрой все Chrome и попробуй снова.")
    sys.exit(1)


async def extract_xoxd(tab_ws: str) -> str | None:
    """Extract xoxd cookie via CDP Network.getCookies (sees httpOnly)."""
    import websockets
    async with websockets.connect(tab_ws, max_size=10 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "id": 1,
            "method": "Network.getCookies",
            "params": {"urls": [
                "https://slack.com",
                "https://app.slack.com",
            ]}
        }))
        resp = json.loads(await ws.recv())
        for c in resp.get("result", {}).get("cookies", []):
            if c["name"] == "d" and c["value"].startswith("xoxd-"):
                return c["value"]
    return None


async def extract_xoxc(tab_ws: str) -> str | None:
    """Extract xoxc token from Slack tab's localStorage."""
    import websockets
    async with websockets.connect(tab_ws, max_size=10 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": """
            (() => {
                try {
                    const lc = JSON.parse(localStorage.getItem('localConfig_v2') || '{}');
                    for (const t of Object.values(lc.teams || {})) {
                        if (t.token?.startsWith('xoxc-')) return t.token;
                    }
                    if (window.boot_data?.api_token) return window.boot_data.api_token;
                    if (window.TS?.boot_data?.api_token) return window.TS.boot_data.api_token;
                    for (let i = 0; i < localStorage.length; i++) {
                        const v = localStorage.getItem(localStorage.key(i));
                        if (v?.startsWith('xoxc-')) return v;
                    }
                } catch(e) {}
                return null;
            })()
        """}}))
        resp = json.loads(await ws.recv())
        return resp.get("result", {}).get("result", {}).get("value")


def find_slack_tab(workspace_url: str) -> dict | None:
    """Find an existing Slack tab."""
    tabs = cdp_get("/json") or []
    for t in tabs:
        if t.get("type") == "page" and "slack.com" in t.get("url", ""):
            return t
    return None


async def open_slack_tab(browser_ws: str, workspace_url: str) -> str | None:
    """Open a new tab with Slack."""
    import websockets
    async with websockets.connect(browser_ws, max_size=10 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "id": 1,
            "method": "Target.createTarget",
            "params": {"url": workspace_url}
        }))
        resp = json.loads(await ws.recv())
        return resp.get("result", {}).get("targetId")


def extract_tokens(workspace_url: str) -> dict[str, str]:
    """Main extraction flow."""
    version = check_cdp()
    browser_ws = version["webSocketDebuggerUrl"]
    tokens = {}

    # 1. Find or open Slack tab
    slack_tab = find_slack_tab(workspace_url)
    if slack_tab:
        print(f"  Slack tab найден: {slack_tab['url'][:60]}")
    else:
        print(f"  Открываю {workspace_url}...")
        asyncio.run(open_slack_tab(browser_ws, workspace_url))
        for _ in range(30):
            time.sleep(2)
            slack_tab = find_slack_tab(workspace_url)
            if slack_tab:
                break
        if not slack_tab:
            print("  ОШИБКА: Slack tab не открылся.")
            print("  Если первый запуск — залогинься в открывшемся Chrome и перезапусти скрипт.")
            sys.exit(1)
        print(f"  Slack загружен: {slack_tab['url'][:60]}")
        time.sleep(5)

    # 2. Extract xoxc — poll until user logs in (up to 3 minutes)
    print("  Извлекаю xoxc...", end="", flush=True)
    xoxc = None
    max_wait = 900  # 15 minutes for user to log in
    waited = 0
    while waited < max_wait:
        slack_tab = find_slack_tab(workspace_url)
        if slack_tab and "app.slack.com/client" in slack_tab.get("url", ""):
            # Slack загружен — пробуем достать токен (WebSocket)
            xoxc = asyncio.run(extract_xoxc(slack_tab["webSocketDebuggerUrl"]))
            if xoxc:
                break
        # Ещё не залогинен — ждём, проверяем URL (дешёвый HTTP GET) каждые 5с
        print(".", end="", flush=True)
        time.sleep(5)
        waited += 5
        if waited % 30 == 0:
            url_hint = slack_tab["url"][:50] if slack_tab else "нет таба"
            print(f"\n  Жду логин... ({waited}с) [{url_hint}]", end="", flush=True)

    if not xoxc:
        print(f"\n  ОШИБКА: xoxc не найден за {max_wait}с. Залогинься и перезапусти скрипт.")
        sys.exit(1)
    print(f"\n  xoxc: {xoxc[:35]}...")
    tokens["xoxc"] = xoxc

    # 3. Extract xoxd (httpOnly cookie via CDP)
    print("  Извлекаю xoxd...", end="", flush=True)
    slack_tab = find_slack_tab(workspace_url)
    if not slack_tab:
        print("\n  ОШИБКА: Slack tab пропал.")
        sys.exit(1)
    xoxd = asyncio.run(extract_xoxd(slack_tab["webSocketDebuggerUrl"]))
    if not xoxd:
        print("\n  ОШИБКА: xoxd cookie не найден.")
        sys.exit(1)
    print(f"\n  xoxd: {xoxd[:35]}...")
    tokens["xoxd"] = xoxd

    return tokens


def configure_slackcli(workspace_url: str, tokens: dict[str, str]):
    """Register tokens with slackcli."""
    slackcli = SCRIPT_DIR / "bin" / "slackcli.exe"
    slackcli_str = str(slackcli) if slackcli.exists() else "slackcli"

    # CDP returns URL-encoded xoxd; slackcli needs decoded
    xoxd_decoded = urllib.parse.unquote(tokens["xoxd"])

    cmd = [
        slackcli_str, "auth", "login-browser",
        f"--xoxd={xoxd_decoded}",
        f"--xoxc={tokens['xoxc']}",
        f"--workspace-url={workspace_url}",
    ]
    print("\n  Регистрирую в slackcli...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print("  OK — slackcli настроен")
    else:
        print(f"  slackcli ошибка: {result.stderr or result.stdout}")


def save_env(tokens: dict[str, str]):
    """Save tokens to .env file."""
    env_file = SCRIPT_DIR / ".env"
    with open(env_file, "w") as f:
        f.write(f'SLACK_XOXC="{tokens["xoxc"]}"\n')
        f.write(f'SLACK_XOXD="{tokens["xoxd"]}"\n')
    print(f"  Токены сохранены в {env_file}")


def main():
    parser = argparse.ArgumentParser(
        description="Extract Slack browser tokens (xoxc/xoxd) into .env"
    )
    parser.add_argument(
        "workspace_url",
        nargs="?",
        default=os.environ.get("SLACK_WORKSPACE_URL"),
        help="Slack workspace URL, e.g. https://your-workspace.slack.com "
             "(default: $SLACK_WORKSPACE_URL)",
    )
    parser.add_argument("--slackcli", action="store_true",
                        help="also register tokens with the optional `slackcli` binary")
    parser.add_argument("--no-close", action="store_true", help="Don't close Chrome after extraction")
    args = parser.parse_args()

    if not args.workspace_url:
        parser.error("workspace URL required — pass it as an argument or set $SLACK_WORKSPACE_URL")
    url = args.workspace_url.rstrip("/")

    tokens = extract_tokens(url)
    save_env(tokens)

    if args.slackcli:
        configure_slackcli(url, tokens)

    # Close Chrome unless asked not to
    if chrome_process and not args.no_close:
        print("  Закрываю Chrome...")
        chrome_process.terminate()

    print("\n  Готово!")


if __name__ == "__main__":
    main()
