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
from i18n import _

HELPER = "/usr/local/sbin/monitor-bot-action"
OFFSITE_STATUS = Path("/var/lib/monitor-bot-offsite/status.json")  # пишет помощник, когда ПК забрал копии
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
    return {"path": _('бот'), "own": True, "max_age_h": 26 if cfg.get("backup_time") else None, "exists": True,
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
            return None, _('файл больше 45 МБ, Telegram его не примет')
        if "no backups" in out:
            return None, _('в папке нет копий')
        return None, out or _('помощник не ответил')
    try:
        return Path(json.loads(out)["file"]), ""
    except (ValueError, KeyError):
        return None, out


# ---------- копии на ПК ----------

def offsite_status(path: Path | None = None) -> dict | None:
    """Когда ПК последний раз забрал копии: {"ts", "files", "bytes", "client"}. None — ни разу."""
    try:
        data = json.loads(Path(path or OFFSITE_STATUS).read_text(encoding="utf-8"))
        return data if isinstance(data.get("ts"), (int, float)) else None
    except (OSError, ValueError, AttributeError):
        return None


def offsite_text(cfg: dict, now: float, status_fn=offsite_status) -> str:
    from checks import fmt_duration
    st = status_fn()
    if st is None:
        return _('💻 На ПК: копии ещё ни разу не забирались (см. README, «Копии на ПК»)')
    age = now - st["ts"]
    limit = cfg.get("offsite_max_age_h")
    bad = limit and age > limit * 3600
    return (_('{v} На ПК: забраны {fmt_duration} назад, {len} файл(а), {fmt}',
              v='❌' if bad else '✅', fmt_duration=fmt_duration(age), len=len(st.get('files') or []), fmt=fmt(st.get('bytes') or 0)))


# ---------- проверка, что копии восстанавливаются ----------

def verify_own(data_dir: Path) -> dict:
    """Распаковать последнюю копию бота во временную папку и проверить config.yaml и metrics.db."""
    files = own_files(data_dir)
    if not files:
        return {"label": "monitor-bot", "path": _('бот'), "ok": False, "error": _('копий нет')}
    name, size, __ = files[0]
    res = {"label": "monitor-bot", "path": _('бот'), "name": name, "size": size}
    try:
        with tempfile.TemporaryDirectory(dir=own_dir(data_dir)) as tmp, \
                tarfile.open(own_dir(data_dir) / name, "r:gz") as tar:
            names = tar.getnames()
            if "config.yaml" not in names:
                return {**res, "ok": False, "error": _('в копии нет config.yaml')}
            import yaml
            cfg = yaml.safe_load(tar.extractfile("config.yaml").read())
            if not isinstance(cfg, dict) or not cfg.get("token"):
                return {**res, "ok": False, "error": _('config.yaml в копии без токена')}
            notes = []
            if "data/metrics.db" in names:
                db = Path(tmp) / "metrics.db"
                with tar.extractfile("data/metrics.db") as src, open(db, "wb") as dst:
                    while chunk := src.read(1 << 20):
                        dst.write(chunk)
                c = sqlite3.connect(db)
                try:
                    check = c.execute("PRAGMA integrity_check").fetchone()[0]
                    rows = c.execute("SELECT COUNT(*) FROM m").fetchone()[0]
                finally:
                    c.close()
                if check != "ok":
                    return {**res, "ok": False, "error": _('metrics.db повреждена: {check}', check=check)}
                notes.append(_('история: {rows} записей', rows=rows))
            return {**res, "ok": True, "members": len(names), "error": None, "note": ", ".join(notes)}
    except (OSError, EOFError, tarfile.TarError, sqlite3.Error, ValueError) as e:
        return {**res, "ok": False, "error": f"{type(e).__name__}: {e}"[:200]}


def verify_all(data_dir: Path, helper_fn=helper) -> tuple[list[dict], str | None]:
    """Проверить последние копии. Бота — сам, глубоко; остальные — через помощник. (результаты, ошибка помощника)."""
    res = [verify_own(data_dir)]
    ok, out = helper_fn("backup-verify", timeout=900)
    err = None
    try:
        others = json.loads(out) if ok else None
    except ValueError:
        others = None
    if others is None:
        err = out or _('помощник не ответил')
    else:
        res += [r for r in others if r.get("label") != "monitor-bot"]
    return res, err


def verify_text(results: list[dict], err: str | None, when: str | None = None) -> str:
    import html
    lines = [_('🧪 <b>Проверка резервных копий</b>') + (f" ({when})" if when else "")]
    for r in results:
        title = html.escape(str(r.get("path")))
        if r.get("ok"):
            extra = f", {html.escape(r['note'])}" if r.get("note") else ""
            lines.append(_('✅ {title}: {escape} читается целиком ({members} файл(ов), {fmt}{extra})',
                           title=title, escape=html.escape(str(r.get('name'))), members=r.get('members', 0), fmt=fmt(r.get('size') or 0), extra=extra))
        else:
            lines.append(f"❌ {title}: {html.escape(str(r.get('error')))}")
    if err:
        lines.append(_('⚠️ Чужие копии не проверены: {escape}', escape=html.escape(err[:200])))
    return "\n".join(lines)


def verify_failed(results: list[dict]) -> bool:
    return any(not r.get("ok") for r in results)


# ---------- алерты ----------

def age_h(st: dict, now: float) -> float | None:
    return (now - st["newest"][2]) / 3600 if st.get("newest") else None


def evaluate(cfg: dict, state, now: float, data_dir: Path | None, watched_fn=watched_status,
             offsite_fn=offsite_status) -> dict[str, str | None]:
    """Ключи bak:<папка>: копий нет или последняя слишком старая; bak:pc — ПК давно не забирал копии."""
    res: dict[str, str | None] = {}
    if not cfg.get("backup_alerts", True):
        return res
    limit = cfg.get("offsite_max_age_h")
    off = offsite_fn() if limit else None
    if off is not None:  # пока ПК ни разу не забирал копии, не шумим
        age = (now - off["ts"]) / 3600
        res["bak:pc"] = (_('💻 ПК не забирал резервные копии уже {age:.0f} ч (норма до {limit:.0f} ч). Включён ли '
                           'компьютер и работает ли задача «monitor-bot-pull»?',
                           age=age, limit=limit)) if age > limit else None
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
        seen = getattr(state, "bak_first_seen", None)
        first = seen.setdefault(st["path"], now) if isinstance(seen, dict) else None
        if not st.get("exists"):
            res[key] = _('💾 Папки с бэкапами {path} нет', path=st['path'])
        elif age is None:
            # папку только что добавили под наблюдение: первая копия появится по своему расписанию
            fresh = first is not None and now - first < max(24.0, float(limit)) * 3600
            res[key] = None if fresh else _('💾 В {path} нет ни одной резервной копии', path=st['path'])
        elif age > limit:
            res[key] = _('💾 Последней резервной копии в {path} уже {age:.0f} ч (норма до {limit:.0f} ч)',
                         path=st['path'], age=age, limit=limit)
        else:
            res[key] = None
        new, prev = st.get("newest"), st.get("previous")
        if new and prev and prev[1] > 1024 and new[1] < prev[1] * SIZE_DROP:
            if state.bak_small.get(st["path"]) != new[0]:
                state.bak_small[st["path"]] = new[0]
                state.bak_events.append(_('⚠️ Новая копия в {path} намного меньше прошлой: {fmt} против {fmt2}. '
                                          'Проверьте, всё ли в неё попало.',
                                          path=st['path'], fmt=fmt(new[1]), fmt2=fmt(prev[1])))
    return res


def fmt(n: float) -> str:
    from checks import fmt_bytes
    return fmt_bytes(n)


def status_text(cfg: dict, data_dir: Path, now: float | None = None, watched_fn=watched_status,
                offsite_fn=offsite_status) -> str:
    import html
    from checks import fmt_duration
    now = now or time.time()
    lines = [_('💾 <b>Резервные копии</b>')]

    def describe(title: str, st: dict) -> str:
        age = age_h(st, now)
        if not st.get("exists"):
            return _('❌ {title}: папки нет', title=title)
        if age is None:
            return _('❌ {title}: копий нет', title=title) if st.get("max_age_h") else _('▫️ {title}: копий пока нет',
                                                                                        title=title)
        bad = st.get("max_age_h") and age > st["max_age_h"]
        return (_('{v} {title}: последняя {fmt_duration} назад, {fmt}, всего {count} шт. ({fmt2})',
                  v='❌' if bad else '✅', title=title, fmt_duration=fmt_duration(age * 3600), fmt=fmt(st['newest'][1]), count=st['count'], fmt2=fmt(st['total'])))

    t = cfg.get("backup_time")
    lines.append(describe(_('Бот (настройки, состояние, история)'), own_status(cfg, data_dir))
                 + (_('\n'
                      '   Каждый день в {t}, храню {v} шт.',
                      t=t, v=cfg.get('backup_keep') or 14) if t else _('\n   По расписанию: выкл')))
    lines.append(offsite_text(cfg, now, offsite_fn))
    watched = watched_fn(now)
    if watched is None:
        lines.append(_('Чужие бэкапы: помощник не настроен (см. README, раздел «Резервные копии»)'))
    for st in watched or []:
        lines.append(describe(html.escape(st["path"]), st))
    if cfg.get("backup_send_daily"):
        lines.append(_('📤 Каждый день присылаю копии сюда, в Telegram'))
    return "\n".join(lines)
