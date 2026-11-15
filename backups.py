"""Резервные копии: копия самого бота и контроль свежести чужих бэкапов (например, weather-bot).

Копию бота (config.yaml, settings.json, state.json, actions.log, metrics.db) бот делает сам, без root,
в <data_dir>/backups. Папки других бэкапов перечислены в /etc/monitor-bot/backup-watch; они обычно
доступны только root, поэтому их состояние бот узнаёт через помощник monitor-bot-action.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

HELPER = "/usr/local/sbin/monitor-bot-action"
OWN_RE = re.compile(r"^monitor-bot-\d{4}-\d{2}-\d{2}_\d{6}\.tar\.gz$")
OWN_FILES = ("settings.json", "state.json", "actions.log")
STATUS_EVERY = 1800  # чужие папки опрашиваем раз в 30 минут
SIZE_DROP = 0.5      # новая копия меньше половины предыдущей — подозрительно

_cache: dict = {"at": 0.0, "watched": None}


def own_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "backups"


# ---------- своя копия ----------

def make_backup(config_path: Path, data_dir: Path, keep: int = 14, now: float | None = None) -> tuple[Path, int, int]:
    """Создать копию. Возвращает (файл, размер, сколько старых удалено)."""
    now = now or time.time()
    d = own_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    name = time.strftime("monitor-bot-%Y-%m-%d_%H%M%S.tar.gz", time.localtime(now))
    dest = d / name
    part = d / (name + ".part")
    with tempfile.TemporaryDirectory(dir=d) as tmp:
        db = Path(data_dir) / "metrics.db"
        if db.exists():
            src = sqlite3.connect(db, timeout=30)
            dst = sqlite3.connect(Path(tmp) / "metrics.db")
            try:
                src.backup(dst)  # согласованная копия без остановки бота
            finally:
                src.close()
                dst.close()
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f, tarfile.open(fileobj=f, mode="w:gz") as tar:
            if Path(config_path).is_file():
                tar.add(config_path, arcname="config.yaml")
            for n in OWN_FILES:
                p = Path(data_dir) / n
                if p.is_file():
                    tar.add(p, arcname=f"data/{n}")
            if (Path(tmp) / "metrics.db").exists():
                tar.add(Path(tmp) / "metrics.db", arcname="data/metrics.db")
    os.replace(part, dest)
    removed = 0
    for f in own_files(data_dir)[keep:]:
        (d / f[0]).unlink(missing_ok=True)
        removed += 1
    return dest, dest.stat().st_size, removed


def own_files(data_dir: Path) -> list[tuple[str, int, float]]:
    """[(имя, размер, mtime)] своих копий, новые первыми."""
    d = own_dir(data_dir)
    if not d.is_dir():
        return []
    files = [(p.name, p.stat().st_size, p.stat().st_mtime) for p in d.iterdir()
             if p.is_file() and OWN_RE.match(p.name)]
    return sorted(files, key=lambda x: x[2], reverse=True)


def own_status(cfg: dict, data_dir: Path) -> dict:
    files = own_files(data_dir)
    return {"path": "бот", "own": True, "max_age_h": 26 if cfg.get("backup_time") else None, "exists": True,
            "count": len(files), "total": sum(f[1] for f in files),
            "newest": list(files[0]) if files else None, "previous": list(files[1]) if len(files) > 1 else None}


# ---------- чужие папки через помощник ----------

def helper(*args: str, timeout: int = 60, runner=subprocess.run) -> tuple[bool, str]:
    try:
        r = runner(["sudo", "-n", HELPER, *args], capture_output=True, text=True, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    return r.returncode == 0, (r.stdout or r.stderr or "").strip()


def watched_status(now: float | None = None, force: bool = False, helper_fn=helper) -> list[dict] | None:
    """Состояние папок из backup-watch. None — помощник недоступен или не обновлён."""
    now = now or time.time()
    if force or _cache["watched"] is None or now - _cache["at"] >= STATUS_EVERY:
        ok, out = helper_fn("backup-status")
        data = None
        if ok:
            try:
                data = json.loads(out)
            except ValueError:
                data = None
        _cache["watched"] = data
        _cache["at"] = now
    return _cache["watched"]


def fetch(index: int, helper_fn=helper) -> tuple[Path | None, str]:
    """Положить последнюю копию N-й папки в /run/monitor-bot-outbox. (файл, ошибка)."""
    ok, out = helper_fn("backup-fetch", str(index), timeout=300)
    if not ok:
        if "too big" in out:
            return None, "файл больше 45 МБ, Telegram его не примет"
        if "no backups" in out:
            return None, "в папке нет копий"
        return None, out or "помощник не ответил"
    try:
        return Path(json.loads(out)["file"]), ""
    except (ValueError, KeyError):
        return None, out


# ---------- алерты ----------

def age_h(st: dict, now: float) -> float | None:
    return (now - st["newest"][2]) / 3600 if st.get("newest") else None


def evaluate(cfg: dict, state, now: float, data_dir: Path | None, watched_fn=watched_status) -> dict[str, str | None]:
    """Ключи bak:<папка>: копий нет или последняя слишком старая."""
    res: dict[str, str | None] = {}
    if not cfg.get("backup_alerts", True):
        return res
    statuses = []
    if data_dir is not None and cfg.get("backup_time"):
        statuses.append(own_status(cfg, data_dir))
    statuses += watched_fn(now) or []
    for st in statuses:
        key = f"bak:{st['path']}"
        limit = st.get("max_age_h")
        if not limit:
            continue
        age = age_h(st, now)
        if not st.get("exists"):
            res[key] = f"💾 Папки с бэкапами {st['path']} нет"
        elif age is None:
            res[key] = f"💾 В {st['path']} нет ни одной резервной копии"
        elif age > limit:
            res[key] = f"💾 Последней резервной копии в {st['path']} уже {age:.0f} ч (норма до {limit:.0f} ч)"
        else:
            res[key] = None
        new, prev = st.get("newest"), st.get("previous")
        if new and prev and prev[1] > 1024 and new[1] < prev[1] * SIZE_DROP:
            if state.bak_small.get(st["path"]) != new[0]:
                state.bak_small[st["path"]] = new[0]
                state.bak_events.append(f"⚠️ Новая копия в {st['path']} намного меньше прошлой: "
                                        f"{fmt(new[1])} против {fmt(prev[1])}. Проверьте, всё ли в неё попало.")
    return res


def fmt(n: float) -> str:
    from checks import fmt_bytes
    return fmt_bytes(n)


def status_text(cfg: dict, data_dir: Path, now: float | None = None, watched_fn=watched_status) -> str:
    import html
    from checks import fmt_duration
    now = now or time.time()
    lines = ["💾 <b>Резервные копии</b>"]

    def describe(title: str, st: dict) -> str:
        age = age_h(st, now)
        if not st.get("exists"):
            return f"❌ {title}: папки нет"
        if age is None:
            return f"❌ {title}: копий нет" if st.get("max_age_h") else f"▫️ {title}: копий пока нет"
        bad = st.get("max_age_h") and age > st["max_age_h"]
        return (f"{'❌' if bad else '✅'} {title}: последняя {fmt_duration(age * 3600)} назад, "
                f"{fmt(st['newest'][1])}, всего {st['count']} шт. ({fmt(st['total'])})")

    t = cfg.get("backup_time")
    lines.append(describe("Бот (настройки, состояние, история)", own_status(cfg, data_dir))
                 + (f"\n   Каждый день в {t}, храню {cfg.get('backup_keep') or 14} шт." if t else "\n   По расписанию: выкл"))
    watched = watched_fn(now)
    if watched is None:
        lines.append("Чужие бэкапы: помощник не настроен (см. README, раздел «Резервные копии»)")
    for st in watched or []:
        lines.append(describe(html.escape(st["path"]), st))
    if cfg.get("backup_send_daily"):
        lines.append("📤 Каждый день присылаю копии сюда, в Telegram")
    return "\n".join(lines)
