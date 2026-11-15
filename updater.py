"""Версия бота и обновление с GitHub. Само обновление делает помощник monitor-bot-action (root)."""
from __future__ import annotations

import json
import re
import time
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
RESULT_FILE = Path("/run/monitor-bot-update.json")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DEFAULT_REPO = "testbox017-lab/server-monitor-bot"


def current(app_dir: Path = APP_DIR) -> dict:
    """{"sha": ..., "installed": ts} из файла VERSION, который пишет помощник. {} — версия неизвестна."""
    try:
        data = json.loads((app_dir / "VERSION").read_text(encoding="utf-8"))
        return data if SHA_RE.match(str(data.get("sha", ""))) else {}
    except (OSError, ValueError):
        return {}


def short(sha: str | None) -> str:
    return sha[:7] if sha else "неизвестна"


def api(path: str, timeout: int = 15) -> dict:
    req = urllib.request.Request(f"https://api.github.com/{path}",
                                 headers={"User-Agent": "monitor-bot", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def latest(repo: str, branch: str = "main", api_fn=api) -> dict:
    """Последний коммит ветки: {"sha", "message", "date"}."""
    d = api_fn(f"repos/{repo}/commits/{branch}")
    return {"sha": d["sha"], "message": d["commit"]["message"].splitlines()[0][:200],
            "date": d["commit"]["committer"]["date"]}


def changes(repo: str, base: str, head: str, api_fn=api) -> list[str]:
    """Заголовки коммитов между base и head (без merge-коммитов), до 10 штук."""
    d = api_fn(f"repos/{repo}/compare/{base}...{head}")
    titles = [c["commit"]["message"].splitlines()[0][:150] for c in d.get("commits", [])
              if len(c.get("parents", [])) < 2]
    return titles[-10:]


def last_result(path: Path = RESULT_FILE) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def result_text(res: dict) -> str:
    sha = short(res.get("sha"))
    notes = "".join(f"\n⚠️ {n}" for n in res.get("notes") or [])
    if res.get("ok"):
        return f"✅ Бот обновлён до {sha} (было {short(res.get('from'))}).{notes}"
    if res.get("stage") == "rolled_back":
        return f"↩️ Обновление до {sha} не удалось: {res.get('error')}. Работает прежняя версия.{notes}"
    tests = f"\nТесты: {res['tests']}" if res.get("tests") and not res.get("ok") else ""
    return f"❌ Обновление до {sha} не удалось: {res.get('error') or res.get('stage')}.{tests}{notes}"


def age_text(ts: float | None, now: float | None = None) -> str:
    if not ts:
        return ""
    days = int(((now or time.time()) - ts) // 86400)
    return "сегодня" if days == 0 else f"{days} дн. назад"
