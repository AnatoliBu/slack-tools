#!/usr/bin/env python3
"""Tests for send_blocks.find_chromium — обнаружение браузера для mermaid→PNG.

Регресс (2026-07): `slack md` перестал рендерить ```mermaid, хотя на диске лежало ТРИ
пригодных браузера. У find_chromium было 4 паттерна, и три из них на машине с машинной
(не per-user) установкой Chrome не срабатывали никогда:
  · ms-playwright/chromium-*/chrome-win/     — playwright переименовал в chrome-win64
  · LOCALAPPDATA/Google/Chrome/Application/  — Chrome стоял в Program Files
  · C:/c/Users/*/…                           — quirk-артефакта не было
Единственный рабочий (~/.cache/puppeteer) исчез вместе с каталогом → ноль кандидатов,
mmdc падал «Could not find Chrome», и это читалось как «диаграмма битая».

Тесты синтезируют фейковые деревья в tmp и подменяют HOME/LOCALAPPDATA/ProgramFiles —
поэтому проходят и на Linux-CI. No pytest dep: `python test_find_chromium.py`.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import send_blocks as sb  # noqa: E402


def _same(a, b):
    """Сравнение путей без завязки на разделитель/регистр диска."""
    return a is not None and b is not None and \
        os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


class _FakeFs:
    """Подменяет HOME/LOCALAPPDATA/ProgramFiles на tmp-каталог и создаёт в нём
    указанные относительные пути (как реальные файлы), чтобы glob их видел."""

    def __init__(self, *rel_paths):
        self.rel = rel_paths

    def __enter__(self):
        self.tmp = tempfile.mkdtemp(prefix="fakefs_")
        self.saved = {k: os.environ.get(k) for k in
                      ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)",
                       "PUPPETEER_EXECUTABLE_PATH", "HOME", "USERPROFILE")}
        os.environ["LOCALAPPDATA"] = os.path.join(self.tmp, "Local")
        os.environ["ProgramFiles"] = os.path.join(self.tmp, "PF")
        os.environ["ProgramFiles(x86)"] = os.path.join(self.tmp, "PF86")
        os.environ["HOME"] = os.environ["USERPROFILE"] = os.path.join(self.tmp, "home")
        os.environ.pop("PUPPETEER_EXECUTABLE_PATH", None)
        self.made = []
        for r in self.rel:
            # native-join по сегментам: os.path.join(tmp, "a/b") на Windows даёт
            # смешанные разделители ('C:\tmp\a/b'), а glob отдаёт бэкслэши → сравнение строк
            # ложно падало. Собираем нативно и сверяем через _same().
            p = os.path.join(self.tmp, *r.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            Path(p).write_text("fake-browser", encoding="utf-8")
            self.made.append(p)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


# ── версионная сортировка: раньше выбирался НЕ самый новый билд ──
def test_natural_key_orders_versions_numerically():
    got = sorted(["win64-99.0.1", "win64-131.0.6778", "win64-100.0.2"], key=sb._natural_key)
    assert got[-1] == "win64-131.0.6778", got
    # плоский лексикографический sorted() выбрал бы '99' — тот самый латентный баг
    assert sorted(["win64-99.0.1", "win64-131.0.6778"])[-1] == "win64-99.0.1"


# ── покрытие паттернов ──
def test_candidates_cover_both_playwright_layouts():
    pats = "\n".join(sb.chromium_candidates())
    assert "chrome-win64" in pats, "новый layout playwright/puppeteer не покрыт"
    assert "chrome-win" in pats, "старый layout playwright не покрыт"
    assert "chromium_headless_shell" in pats, "headless-shell playwright не покрыт"


def test_candidates_cover_machine_wide_and_per_user_chrome():
    pats = sb.chromium_candidates()
    joined = "\n".join(pats).replace("\\", "/")
    assert "ms-playwright" in joined
    assert any("Google/Chrome/Application" in p.replace("\\", "/") for p in pats)
    # машинная установка (Program Files) — её отсутствие и было одной из трёх дыр
    assert any(p.replace("\\", "/").startswith(
        os.environ.get("ProgramFiles", "C:/Program Files").replace("\\", "/")) for p in pats)
    assert "Edge" in joined, "Edge-fallback не покрыт"


# ── регрессы: каждая из трёх дыр ──
def test_finds_playwright_new_layout():
    """chrome-win64 (новый playwright) — старый код промахивался."""
    with _FakeFs("Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe") as fs:
        assert _same(sb.find_chromium(), fs.made[0]), sb.find_chromium()


def test_finds_playwright_old_layout():
    """chrome-win (старый playwright) — обратная совместимость."""
    with _FakeFs("Local/ms-playwright/chromium-1100/chrome-win/chrome.exe") as fs:
        assert _same(sb.find_chromium(), fs.made[0]), sb.find_chromium()


def test_finds_machine_wide_program_files_chrome():
    """Chrome в Program Files — старый код смотрел только в LocalAppData."""
    with _FakeFs("PF/Google/Chrome/Application/chrome.exe") as fs:
        assert _same(sb.find_chromium(), fs.made[0]), sb.find_chromium()


def test_finds_playwright_headless_shell():
    with _FakeFs("Local/ms-playwright/chromium_headless_shell-1228/"
                 "chrome-headless-shell-win64/chrome-headless-shell.exe") as fs:
        assert _same(sb.find_chromium(), fs.made[0]), sb.find_chromium()


def test_finds_edge_as_last_resort():
    with _FakeFs("PF86/Microsoft/Edge/Application/msedge.exe") as fs:
        assert _same(sb.find_chromium(), fs.made[0]), sb.find_chromium()


# ── приоритеты и override ──
def test_puppeteer_cache_wins_over_fallbacks():
    """Канонический puppeteer-кэш предпочитается системному Chrome."""
    with _FakeFs("home/.cache/puppeteer/chrome/win64-131.0.6778/chrome-win64/chrome.exe",
                 "PF/Google/Chrome/Application/chrome.exe") as fs:
        assert _same(sb.find_chromium(), fs.made[0]), sb.find_chromium()


def test_newest_puppeteer_build_picked():
    with _FakeFs("home/.cache/puppeteer/chrome/win64-99.0.1/chrome-win64/chrome.exe",
                 "home/.cache/puppeteer/chrome/win64-131.0.6778/chrome-win64/chrome.exe") as fs:
        assert _same(sb.find_chromium(), fs.made[1]), "выбран не самый новый билд"


def test_env_override_wins():
    with _FakeFs("PF/Google/Chrome/Application/chrome.exe",
                 "custom/my-chrome.exe") as fs:
        os.environ["PUPPETEER_EXECUTABLE_PATH"] = fs.made[1]
        assert _same(sb.find_chromium(), fs.made[1]), sb.find_chromium()


def test_env_override_ignored_if_path_missing():
    """Битый override не должен маскировать рабочий автопоиск."""
    with _FakeFs("PF/Google/Chrome/Application/chrome.exe") as fs:
        os.environ["PUPPETEER_EXECUTABLE_PATH"] = os.path.join(fs.tmp, "nope", "ghost.exe")
        assert _same(sb.find_chromium(), fs.made[0]), sb.find_chromium()


def test_returns_none_when_nothing_installed():
    with _FakeFs():
        assert sb.find_chromium() is None


# ── fail loud: сообщение должно содержать рецепт, а не только «render failed» ──
def test_render_error_is_actionable_without_browser():
    saved_find, saved_bin = sb.find_chromium, sb._mmdc_bin
    sb.find_chromium = lambda: None
    sb._mmdc_bin = lambda: os.path.join(tempfile.gettempdir(), "no-such-mmdc-binary")
    try:
        sb.render_mermaid("flowchart TB\n  A --> B\n")
        raise AssertionError("ожидали RuntimeError")
    except RuntimeError as e:
        msg = str(e)
        assert "браузер для puppeteer НЕ НАЙДЕН" in msg, msg[:200]
        assert "PUPPETEER_EXECUTABLE_PATH" in msg
        assert "@puppeteer/browsers install" in msg
        assert "chrome-win64" in msg, "в ошибке нет списка искомых путей"
    finally:
        sb.find_chromium, sb._mmdc_bin = saved_find, saved_bin


def _run():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
