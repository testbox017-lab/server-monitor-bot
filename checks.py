"""Сбор метрик сервера и логика алертов. Работает на Linux и Windows."""
from __future__ import annotations

import html
import json
import os
import platform
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import psutil

import smart
from i18n import _

IS_WINDOWS = platform.system() == "Windows"
_save_lock = threading.RLock()
LAST_SAMPLE: dict | None = None  # последний снимок из evaluate(), бот пишет его в историю


# ---------- Метрики ----------

def fmt_bytes(n: float) -> str:
    for unit in (_('Б'), _('КБ'), _('МБ'), _('ГБ'), _('ТБ')):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return _('{n:.1f} ПБ', n=n)


def fmt_duration(sec: float) -> str:
    sec = int(sec)
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, __ = divmod(sec, 60)
    parts = []
    if d:
        parts.append(_('{d} д', d=d))
    if h or d:
        parts.append(_('{h} ч', h=h))
    parts.append(_('{m} мин', m=m))
    return " ".join(parts)


def disk_paths(cfg_disks: list[str] | None) -> list[str]:
    if cfg_disks:
        return cfg_disks
    paths = []
    for p in psutil.disk_partitions(all=False):
        if IS_WINDOWS and ("cdrom" in p.opts or not p.fstype):
            continue
        if p.fstype in ("squashfs", "tmpfs", "devtmpfs", "overlay"):
            continue
        paths.append(p.mountpoint)
    return sorted(set(paths))


def collect(cfg: dict) -> dict:
    """Снимок текущего состояния сервера."""
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    disks = {}
    for path in disk_paths(cfg.get("disks")):
        try:
            disks[path] = psutil.disk_usage(path)
        except (PermissionError, FileNotFoundError, OSError):
            continue
    try:
        load = psutil.getloadavg()
    except (AttributeError, OSError):
        load = None
    temps = {}
    if hasattr(psutil, "sensors_temperatures"):
        try:
            for name, entries in (psutil.sensors_temperatures() or {}).items():
                for e in entries:
                    if e.current:
                        temps[f"{name}/{e.label or 'temp'}"] = e.current
        except Exception:
            pass
    net = psutil.net_io_counters()
    return {
        "host": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "cpu": psutil.cpu_percent(interval=1),
        "cpu_count": psutil.cpu_count(),
        "load": load,
        "ram": vm,
        "swap": sw,
        "disks": disks,
        "temps": temps,
        "net": net,
        "uptime": time.time() - psutil.boot_time(),
    }


def status_text(cfg: dict) -> str:
    s = collect(cfg)
    lines = [
        f"🖥 <b>{s['host']}</b> ({s['os']})",
        _('⏱ Аптайм: {fmt_duration}', fmt_duration=fmt_duration(s['uptime'])),
        _('🧠 CPU: {cpu:.0f}% ({cpu_count} ядер)', cpu=s['cpu'], cpu_count=s['cpu_count']),
    ]
    if s["load"]:
        lines.append("📈 Load avg: " + " / ".join(f"{x:.2f}" for x in s["load"]))
    r = s["ram"]
    lines.append(_('💾 RAM: {percent:.0f}% ({fmt_bytes} из {fmt_bytes2})',
                   percent=r.percent, fmt_bytes=fmt_bytes(r.used), fmt_bytes2=fmt_bytes(r.total)))
    if s["swap"].total:
        lines.append(f"🔁 Swap: {s['swap'].percent:.0f}%")
    for path, d in s["disks"].items():
        lines.append(_('🗄 Диск {path}: {percent:.0f}% (свободно {fmt_bytes})',
                       path=path, percent=d.percent, fmt_bytes=fmt_bytes(d.free)))
    for name, t in list(s["temps"].items())[:4]:
        lines.append(f"🌡 {name}: {t:.0f}°C")
    lines.append(_('🌐 Сеть: ↓ {fmt_bytes} ↑ {fmt_bytes2}',
                   fmt_bytes=fmt_bytes(s['net'].bytes_recv), fmt_bytes2=fmt_bytes(s['net'].bytes_sent)))
    stats = container_stats()
    if stats:
        lines.append(_('🐳 Контейнеры:'))
        for name, cpu, mem in stats:
            lines.append(f"  • {html.escape(name)}: CPU {html.escape(cpu)}, RAM {html.escape(mem)}")
    return "\n".join(lines)


def top_processes(n: int = 10, by: str = "cpu") -> str:
    procs = list(psutil.process_iter(["pid", "name", "memory_percent"]))
    for p in procs:  # первый вызов cpu_percent всегда 0, поэтому замеряем за интервал
        try:
            p.cpu_percent(None)
        except psutil.Error:
            pass
    time.sleep(1)
    rows = []
    for p in procs:
        try:
            rows.append((p.cpu_percent(None), p.info["memory_percent"] or 0, p.info["pid"], p.info["name"]))
        except psutil.Error:
            continue
    key = 0 if by == "cpu" else 1
    rows.sort(key=lambda r: r[key], reverse=True)
    title = "CPU" if by == "cpu" else "RAM"
    out = [_('🔝 <b>Топ-{n} процессов по {title}</b>', n=n, title=title), _('<pre>  CPU%  RAM%    PID  Имя')]
    for cpu, mem, pid, name in rows[:n]:
        out.append(f"{cpu:6.1f} {mem:5.1f} {pid:6d}  {html.escape((name or '?')[:28])}")
    out.append("</pre>")
    return "\n".join(out)


# ---------- Процессы и службы ----------

def process_running(pattern: str) -> int:
    """Количество процессов, у которых имя или командная строка содержит pattern."""
    pat = pattern.lower()
    count = 0
    me = os.getpid()
    for p in psutil.process_iter(["name", "cmdline"]):
        if p.pid == me:
            continue
        try:
            name = (p.info["name"] or "").lower()
            cmd = " ".join(p.info["cmdline"] or []).lower()
        except psutil.Error:
            continue
        if pat == name or pat in cmd or name == pat + ".exe":
            count += 1
    return count


def service_state(name: str) -> str:
    """'running' / 'stopped' / 'unknown' и т.п."""
    if IS_WINDOWS:
        try:
            return psutil.win_service_get(name).status()
        except Exception:
            return "not found"
    if shutil.which("systemctl"):
        r = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True)
        out = r.stdout.strip() or "unknown"
        return "running" if out == "active" else out
    return "unknown"


def parse_ps(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if "\t" in line:
            name, state = line.split("\t", 1)
            out[name] = state
    return out


def container_states() -> dict[str, str] | None:
    """{имя контейнера: статус} для всех контейнеров Docker, None если docker недоступен."""
    cached = _poll_fresh()
    if cached is not None:
        return dict(cached["states"])
    r = docker("ps", "-a", "--format", PS_FMT)
    if r is None or r.returncode != 0:
        return None
    return parse_ps(r.stdout)


# Один вызов помощника «docker-poll» на проверку вместо отдельных docker ps и docker inspect:
# каждый вызов через sudo — это несколько строк в системном журнале, а sudo-rs их не умеет отключать.
POLL_TTL = 30
_poll: dict = {"ts": 0.0, "states": {}, "info": {}, "unsupported_until": 0.0}


def _poll_fresh() -> dict | None:
    return _poll if time.time() - _poll["ts"] < POLL_TTL else None


def docker_poll() -> bool:
    """Обновить кеш состояния всех контейнеров одним вызовом помощника. False — помощник старый или
    docker недоступен (тогда работают обычные вызовы)."""
    _poll["ts"] = 0.0
    if not docker_via_helper() or time.time() < _poll["unsupported_until"]:
        return False
    try:
        r = subprocess.run(["sudo", "-n", HELPER, "docker-poll"], capture_output=True, text=True, timeout=45,
                           encoding="utf-8", errors="replace")
    except Exception:
        return False
    if r.returncode == 2 and "unknown command" in r.stdout:
        _poll["unsupported_until"] = time.time() + DOCKER_DIRECT_RETRY
        return False
    if r.returncode != 0:
        return False
    try:
        data = json.loads(r.stdout)
        states, info = parse_ps(data["ps"]), parse_inspect(data["inspect"])
    except (ValueError, KeyError, TypeError):
        return False
    _poll.update(ts=time.time(), states=states, info=info)
    return True


def container_state(name: str, states: dict[str, str] | None) -> str:
    if states is None:
        return _('docker недоступен')
    return states.get(name, "not found")


# Форматы вывода docker. Помощник monitor-bot-action разрешает ровно эти строки.
PS_FMT = "{{.Names}}\t{{.State}}"
INSPECT_FMT = ("{{.Name}}\t{{.RestartCount}}\t{{.State.StartedAt}}\t{{if .State.Health}}{{.State.Health.Status}}{{end}}"
               "\t{{if .State.Health}}{{json .State.Health.Log}}{{end}}")
STATS_FMT = "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"
HELPER = "/usr/local/sbin/monitor-bot-action"
DOCKER_DIRECT_RETRY = 600
_docker_mode = {"direct_until": 0.0}


def docker_via_helper() -> bool:
    """Linux, бот не root и помощник установлен: docker вызываем через sudo-помощник,
    чтобы пользователю бота не нужна была группа docker (она даёт root)."""
    return (not IS_WINDOWS and hasattr(os, "geteuid") and os.geteuid() != 0 and os.path.exists(HELPER)
            and time.time() >= _docker_mode["direct_until"])


def docker(*args: str, timeout: int = 15) -> subprocess.CompletedProcess | None:
    if docker_via_helper():
        try:
            r = subprocess.run(["sudo", "-n", HELPER, "docker", *args], capture_output=True, text=True,
                               timeout=timeout + 5, encoding="utf-8", errors="replace")
        except Exception:
            return None
        if r.returncode != 2 or "unknown command" not in r.stdout:
            return r
        # Старый помощник без команды docker: пока работаем напрямую (если бот ещё в группе docker)
        _docker_mode["direct_until"] = time.time() + DOCKER_DIRECT_RETRY
    if not shutil.which("docker"):
        return None
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
    except Exception:
        return None


def container_info(names: list[str]) -> dict[str, dict]:
    """{имя: {"restarts": int, "started": str, "health": str, "reason": str}} через docker inspect.
    reason — что написала последняя проверка healthcheck (если она есть)."""
    if not names:
        return {}
    names = [n for n in names if valid_container(n)]
    if not names:
        return {}
    cached = _poll_fresh()
    if cached is not None:  # в кеше все существующие контейнеры; отсутствующих нет и в docker inspect
        return {n: dict(cached["info"][n]) for n in names if n in cached["info"]}
    r = docker("inspect", "--format", INSPECT_FMT, *names)
    if r is None:
        return {}
    return parse_inspect(r.stdout)


def parse_inspect(text: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for line in text.splitlines():  # для несуществующих имён docker пишет ошибку в stderr, остальные выводит
        parts = line.split("\t")
        if len(parts) >= 4:
            try:
                restarts = int(parts[1])
            except ValueError:
                restarts = 0
            reason = ""
            try:
                log = json.loads(parts[4]) if len(parts) > 4 and parts[4] else []
                if log:
                    reason = " ".join(str(log[-1].get("Output") or "").split())[:200]
            except (ValueError, AttributeError, TypeError):
                pass
            out[parts[0].lstrip("/")] = {"restarts": restarts, "started": parts[2], "health": parts[3],
                                         "reason": reason}
    return out


def container_stats() -> list[tuple[str, str, str]]:
    """[(имя, CPU%, память)] для работающих контейнеров."""
    r = docker("stats", "--no-stream", "--format", STATS_FMT, timeout=20)
    if r is None or r.returncode != 0:
        return []
    rows = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            rows.append((parts[0], parts[1], parts[2].split(" / ")[0]))
    return sorted(rows)


CONTAINER_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")


def valid_container(name: str) -> bool:
    """Имя контейнера Docker: буквы, цифры, _ . -, не с «-» (иначе docker примет его за параметр)."""
    return bool(name) and len(name) <= 128 and name[0].isalnum() and set(name) <= CONTAINER_CHARS


def container_logs(name: str, tail: int = 30, since: int | None = None) -> str | None:
    if not valid_container(name):
        return None
    args = ["logs", "--tail", str(tail)] if since is None else ["logs", "--since", str(int(since))]
    r = docker(*args, name, timeout=20)
    if r is None or r.returncode != 0:
        return None
    return (r.stdout or "") + (r.stderr or "")  # docker отдаёт stderr контейнера в stderr


def logs_text(name: str, tail: int = 30) -> str:
    logs = container_logs(name, tail)
    if logs is None:
        return _('📜 Не удалось получить логи {escape} (нет контейнера или доступа к docker)', escape=html.escape(name))
    logs = logs.strip() or _('(пусто)')
    if len(logs) > 3500:
        logs = "…" + logs[-3500:]
    return _('📜 <b>{escape}</b>, последние {tail} строк\n'
             '<pre>{escape2}</pre>',
             escape=html.escape(name), tail=tail, escape2=html.escape(logs))


def watched_containers(cfg: dict, state: "AlertState | None" = None) -> list[str]:
    """Контейнеры из настроек плюс (в авто-режиме) все, которые бот когда-либо видел работающими."""
    names = list(cfg.get("containers") or [])
    if cfg.get("containers_auto") and state is not None:
        for n in state.seen_containers:
            if n not in names:
                names.append(n)
    return names


def services_text(cfg: dict, state: "AlertState | None" = None) -> str:
    lines = [_('⚙️ <b>Службы и процессы</b>')]
    for svc in cfg.get("services", []) or []:
        st = service_state(svc)
        icon = "✅" if st == "running" else "❌"
        lines.append(_('{icon} служба {escape}: {escape2}',
                       icon=icon, escape=html.escape(svc), escape2=html.escape(st)))
    for proc in cfg.get("processes", []) or []:
        n = process_running(proc)
        icon = "✅" if n else "❌"
        lines.append(_('{icon} процесс {escape}: {v}',
                       icon=icon, escape=html.escape(proc), v=_('запущен ({n})', n=n) if n else _('не найден')))
    containers = watched_containers(cfg, state)
    if containers:
        states = container_states()
        info = container_info(containers)
        for c in containers:
            st = container_state(c, states)
            i = info.get(c, {})
            extra = []
            if i.get("health"):
                extra.append(i["health"])
            if i.get("restarts"):
                extra.append(_('перезапусков: {restarts}', restarts=i['restarts']))
            ok = st == "running" and i.get("health") != "unhealthy"
            tail = f" ({', '.join(extra)})" if extra else ""
            lines.append(_('{v} контейнер {escape}: {escape2}{escape3}',
                           v='✅' if ok else '❌', escape=html.escape(c), escape2=html.escape(st), escape3=html.escape(tail)))
    if len(lines) == 1:
        lines.append(_('Список пуст. Добавьте services / processes в config.yaml.'))
    if state is not None and cfg.get("probes"):
        import probes
        lines += ["", probes.probes_text(cfg, state)]
    return "\n".join(lines)


def running_services() -> list[str]:
    """Имена работающих служб systemd (для меню «Добавить»)."""
    if IS_WINDOWS or not shutil.which("systemctl"):
        return []
    r = subprocess.run(["systemctl", "list-units", "--type=service", "--state=running",
                        "--no-legend", "--plain", "--no-pager"], capture_output=True, text=True)
    names = []
    for line in r.stdout.splitlines():
        unit = line.split()[0] if line.split() else ""
        if unit.endswith(".service") and "@" not in unit:
            names.append(unit[:-len(".service")])
    return sorted(names)


def oom_kills() -> int | None:
    """Счётчик убитых OOM killer процессов с момента загрузки (Linux, /proc/vmstat)."""
    try:
        for line in Path("/proc/vmstat").read_text().splitlines():
            if line.startswith("oom_kill "):
                return int(line.split()[1])
    except OSError:
        pass
    return None


def reboot_required() -> bool:
    return Path("/var/run/reboot-required").exists()


# ---------- Время ----------

def tz(cfg: dict) -> ZoneInfo:
    try:
        return ZoneInfo(cfg.get("tz") or "Europe/Kyiv")
    except Exception:
        return ZoneInfo("UTC")


def _hm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def in_quiet_hours(cfg: dict, now: float) -> bool:
    q = cfg.get("quiet_hours")
    if not q:
        return False
    t = datetime.fromtimestamp(now, tz(cfg)).time()
    start, end = _hm(q["start"]), _hm(q["end"])
    return start <= t < end if start < end else (t >= start or t < end)


def next_morning(cfg: dict, now: float) -> float:
    """Ближайший конец тихих часов (или 08:00, если они не заданы)."""
    q = cfg.get("quiet_hours") or {"end": "08:00"}
    local = datetime.fromtimestamp(now, tz(cfg))
    target = local.replace(hour=_hm(q["end"]).hour, minute=_hm(q["end"]).minute, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target.timestamp()


def fmt_local(cfg: dict, ts: float) -> str:
    return datetime.fromtimestamp(ts, tz(cfg)).strftime("%H:%M")


# ---------- Алерты ----------

CRITICAL_PREFIXES = ("svc:", "proc:", "ctr:", "web:")
MAINT_MARK = "🔧 "  # инцидент начался во время обслуживания (плановые работы)


def is_critical(key: str) -> bool:
    """Критичные алерты приходят и в тихие часы."""
    return key.startswith(CRITICAL_PREFIXES)


@dataclass
class AlertState:
    since: dict[str, float] = field(default_factory=dict)      # когда условие впервые стало плохим
    active: list[str] = field(default_factory=list)            # по каким ключам отправлен алерт
    last_sent: dict[str, float] = field(default_factory=dict)  # когда последний раз писали по ключу
    problems: dict[str, str] = field(default_factory=dict)     # текст проблемы по активному ключу
    mute_until: float = 0.0
    boot_time: float = 0.0
    oom_kills: int | None = None
    log: list[list] = field(default_factory=list)              # [ts, key] отправленных алертов, 48 ч
    seen_containers: list[str] = field(default_factory=list)   # для авто-режима
    ctr_started: dict[str, str] = field(default_factory=dict)  # StartedAt по контейнеру
    ctr_restarts: dict[str, int] = field(default_factory=dict)
    log_checked: dict[str, int] = field(default_factory=dict)  # до какого времени логи просмотрены
    log_alert_at: dict[str, float] = field(default_factory=dict)
    ssh_checked: int = 0
    known_ips: list[str] = field(default_factory=list)
    ssh_failed: list[list] = field(default_factory=list)       # [ts, ip, n] за 10 мин
    ssh_fail_alert_at: float = 0.0
    known_ports: list[str] = field(default_factory=list)
    updates_checked: float = 0.0
    updates_alert_at: float = 0.0
    heartbeat_ok: bool | None = None
    probe_fails: dict[str, int] = field(default_factory=dict)  # неудач подряд по цели
    probe_last: dict[str, list] = field(default_factory=dict)  # [ok, текст, мс, ts]
    ssl_checked: dict[str, float] = field(default_factory=dict)
    ssl_days: dict[str, float] = field(default_factory=dict)
    ssl_alerted: dict[str, int] = field(default_factory=dict)  # последний порог, о котором писали
    acked: list[str] = field(default_factory=list)             # «Принял»: без напоминаний до восстановления
    snoozed: dict[str, float] = field(default_factory=dict)    # «Этот на 1 ч»: ключ → до какого времени молчим
    ram_trend_at: float = 0.0
    bak_small: dict[str, str] = field(default_factory=dict)    # о какой маленькой копии уже писали
    bak_events: list[str] = field(default_factory=list)
    update_reported: float | None = None                       # о каком обновлении уже сообщили
    verify_last: dict = field(default_factory=dict)            # последняя проверка копий: ts, results, err
    autobans: dict[str, float] = field(default_factory=dict)   # автобан: IP → до какого времени (0 = навсегда)
    autoban_skip: dict[str, float] = field(default_factory=dict)  # IP, которые помощник не дал заблокировать
    maint_until: float = 0.0                                   # режим обслуживания: молчим и помечаем инциденты
    known_ip_at: dict[str, float] = field(default_factory=dict)   # IP → время последнего успешного входа по SSH
    seen_missing: dict[str, float] = field(default_factory=dict)  # авто-контейнер → с какого времени его нет
    deploy_at: float = 0.0                                     # последний вход по SSH ключом деплоя
    net_fail_at: float = 0.0                                   # когда у бота последний раз пропадала связь
    fail_ips: dict[str, list] = field(default_factory=dict)    # IP → [неудачных попыток за сутки, последняя]
    ssh_daily: dict[str, dict] = field(default_factory=dict)   # «2026-11-16» → {IP: попыток} за 8 дней
    ban_history: dict[str, list] = field(default_factory=dict)  # IP или подсеть → [сколько раз банили, когда]
    bak_first_seen: dict[str, float] = field(default_factory=dict)  # папка бэкапов → когда бот впервые её увидел

    path: Path | None = field(default=None, repr=False, compare=False)
    load_error: str = field(default="", repr=False, compare=False)  # state.json был повреждён (текст для бота)

    @classmethod
    def load(cls, path: Path) -> "AlertState":
        st = cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
            for k, v in data.items():
                if k in cls.__dataclass_fields__ and k not in ("path", "load_error"):
                    setattr(st, k, v)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            # Не начинаем молча с нуля: битый файл сохраняем рядом, бот напишет об этом при запуске
            bad = path.with_name(path.name + ".bad")
            try:
                os.replace(path, bad)
            except OSError:
                bad = path
            st.load_error = _('{name} был повреждён ({name2}), сохранён как {name3}',
                              name=path.name, name2=type(e).__name__, name3=bad.name)
        st.path = path
        now = time.time()
        for ip in st.known_ips:  # состояние из прошлой версии: времени входа ещё нет
            st.known_ip_at.setdefault(ip, now)
        return st

    def save(self) -> None:
        if not self.path:
            return
        # Сохраняют из нескольких потоков (проверка в фоне и кнопки), поэтому под общей блокировкой
        with _save_lock:
            for attempt in range(3):
                try:
                    data = {k: v for k, v in asdict(self).items() if k not in ("path", "load_error")}
                    text = json.dumps(data, ensure_ascii=False)
                    break
                except RuntimeError:  # словарь меняли в другом потоке прямо во время копирования
                    if attempt == 2:
                        raise
                    time.sleep(0.05)
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self.path)

    def muted(self, now: float) -> bool:
        return now < self.mute_until

    def in_maintenance(self, now: float) -> bool:
        return now < self.maint_until


def key_label(key: str) -> str:
    if key.startswith("anom:"):
        return _('необычная нагрузка ') + smart.NAMES.get(key[5:], key[5:])
    if key == "bak:pc":
        return _('копии на ПК')
    return key.split(":", 1)[-1]


def evaluate(cfg: dict, state: "AlertState | None" = None) -> dict[str, str | None]:
    """Возвращает {ключ: описание проблемы или None, если всё в порядке}."""
    global LAST_SAMPLE
    th = cfg.get("thresholds", {})
    s = collect(cfg)
    LAST_SAMPLE = s
    res: dict[str, str | None] = {}

    def check(key, value, limit, label):
        if limit is None:
            return
        res[key] = _('{label}: {value:.0f}% (порог {limit}%)',
                     label=label, value=value, limit=limit) if value >= limit else None

    check("cpu", s["cpu"], th.get("cpu"), _('🔥 Высокая загрузка CPU'))
    check("ram", s["ram"].percent, th.get("ram"), _('💾 Мало свободной памяти, RAM'))
    if s["swap"].total:
        check("swap", s["swap"].percent, th.get("swap"), _('🔁 Активный swap'))
    for path, d in s["disks"].items():
        check(f"disk:{path}", d.percent, th.get("disk"), _('🗄 Заканчивается место на {path}', path=path))
    if th.get("inode") is not None:
        import network
        for path, pct in network.inode_usage(list(s["disks"])).items():
            check(f"inode:{path}", pct, th["inode"], _('🗂 Заканчиваются inode (файлы) на {path}', path=path))
    if th.get("temp") is not None:
        for name, t in s["temps"].items():
            res[f"temp:{name}"] = (_('🌡 Перегрев {name}: {t:.0f}°C (порог {temp}°C)', name=name, t=t, temp=th['temp'])
                                   if t >= th["temp"] else None)
    for svc in cfg.get("services", []) or []:
        st = service_state(svc)
        res[f"svc:{svc}"] = None if st == "running" else _('❌ Служба {svc} не работает ({st})', svc=svc, st=st)
    for proc in cfg.get("processes", []) or []:
        res[f"proc:{proc}"] = None if process_running(proc) else _('❌ Процесс {proc} не найден', proc=proc)
    containers = watched_containers(cfg, state)
    if containers:
        states = container_states()
        info = container_info(containers)
        for c in containers:
            st = container_state(c, states)
            health = info.get(c, {}).get("health")
            if st != "running":
                res[f"ctr:{c}"] = _('❌ Контейнер {c} не работает ({st})', c=c, st=st)
            elif health == "unhealthy":
                why = info.get(c, {}).get("reason")
                res[f"ctr:{c}"] = (_('❌ Контейнер {c} работает, но завис или не в порядке (healthcheck)', c=c)
                                   + (f": {why}" if why else ""))
            else:
                res[f"ctr:{c}"] = None
    res["reboot_required"] = _('♻️ Серверу нужна перезагрузка после обновлений') if reboot_required() else None
    if state is not None:
        import backups
        try:
            res.update(backups.evaluate(cfg, state, time.time(), state.path.parent if state.path else None))
        except Exception:
            import logging
            logging.getLogger("monitor_bot").exception('Ошибка проверки бэкапов')
    if state is not None and cfg.get("probes"):
        import probes
        try:
            res.update(probes.evaluate(cfg, state, time.time()))
        except Exception:  # внешние проверки не должны ломать основную
            import logging
            logging.getLogger("monitor_bot").exception('Ошибка внешних проверок')
    return res


def instant_keys(key: str) -> bool:
    """Падение службы/процесса/контейнера сообщаем сразу, ресурсы только после duration."""
    return key.startswith(CRITICAL_PREFIXES) or key == "reboot_required"


@dataclass
class Alert:
    text: str
    key: str | None = None   # ключ проблемы, для кнопок под сообщением
    kind: str = "alert"      # alert / remind / recovered / event
    send: str = "loud"       # loud — как обычно, quiet — без звука, journal — только в журнал инцидентов


def step(cfg: dict, state: AlertState, now: float | None = None,
         results: dict[str, str | None] | None = None, hist=None, inc=None) -> list[Alert]:
    """Одна итерация проверки. Возвращает сообщения для отправки и сохраняет состояние."""
    now = now or time.time()
    th = cfg.get("thresholds", {})
    duration = th.get("duration_sec", 300)
    remind = (cfg.get("remind_min") or 0) * 60
    maint = state.in_maintenance(now)
    muted = state.muted(now) or maint
    quiet = in_quiet_hours(cfg, now)
    out: list[Alert] = []
    real = results is None
    forgotten: list[str] = []
    if real:
        if cfg.get("containers") or cfg.get("containers_auto"):
            docker_poll()
        forgotten = update_seen_containers(state, cfg, now)
    results = evaluate(cfg, state) if real else dict(results)
    if real and hist is not None:
        try:
            results.update(smart.anomaly_results(cfg, LAST_SAMPLE, hist, now, tz(cfg)))
        except Exception:
            import logging
            logging.getLogger("monitor_bot").exception('Ошибка расчёта нормы')
    smart.clean(state, now)
    recovered: set[str] = set()

    for key, problem in results.items():
        if problem:
            state.since.setdefault(key, now)
            need = smart.anomaly_duration(duration) if key.startswith("anom:") else duration
            if not (instant_keys(key) or now - state.since[key] >= need):
                continue
            _journal(inc, "open", key, (MAINT_MARK if maint else "") + problem, state.since[key])  # даже в тихие часы
            snoozed = state.snoozed.get(key, 0) > now
            suppressed = muted or snoozed or (quiet and not is_critical(key))
            if suppressed:
                continue  # отправим, когда тишина закончится, если проблема ещё есть
            if key not in state.active:
                state.active.append(key)
                state.last_sent[key] = now
                state.problems[key] = problem
                state.log.append([now, key])
                out.append(Alert(f"🚨 {problem}", key, "alert"))
            elif remind and key not in state.acked and now - state.last_sent.get(key, now) >= remind:
                state.last_sent[key] = now
                state.problems[key] = problem
                mins = int((now - state.since[key]) // 60)
                out.append(Alert(_('⏰ Всё ещё не решено ({mins} мин): {problem}',
                                   mins=mins, problem=problem), key, "remind"))
        else:
            state.since.pop(key, None)
            _journal(inc, "close", key, now)
            if key in state.active:
                state.active.remove(key)
                state.last_sent.pop(key, None)
                state.problems.pop(key, None)
                recovered.add(key)
                if key in state.acked:
                    state.acked.remove(key)
                if not muted:
                    out.append(Alert(_('✅ Восстановлено: {key_label}', key_label=key_label(key)), key, "recovered"))

    # Ключи, которых больше нет в проверке (убрали из настроек) — забываем молча
    if inc is not None:
        for key in inc.open_keys() - set(results):
            _journal(inc, "close", key, now)
    for key in list(state.active):
        if key not in results:
            state.active.remove(key)
            state.problems.pop(key, None)
            state.last_sent.pop(key, None)
    for key in list(state.since):
        if key not in results:
            state.since.pop(key)
    state.acked = [k for k in state.acked if k in state.active]

    # OOM killer — событие, не состояние
    n = oom_kills()
    if n is not None:
        if state.oom_kills is not None and n > state.oom_kills and not muted:
            out.append(Alert(_('💥 OOM killer убил процесс(ы): {v}. Не хватило памяти (swap на сервере нет).',
                               v=n - state.oom_kills), None, "event"))
            state.log.append([now, "oom"])
        state.oom_kills = n

    if real:
        # Сначала SSH: вход ключом деплоя должен быть известен до того, как увидим перезапуск контейнера
        import security
        try:
            out += security.events(cfg, state, now, muted, quiet)
        except Exception:  # безопасность не должна ломать основную проверку
            import logging
            logging.getLogger("monitor_bot").exception('Ошибка проверки безопасности')
        out += docker_events(cfg, state, now, recovered, muted, quiet)
        import probes
        try:
            out += probes.ssl_events(cfg, state, now, muted, quiet)
        except Exception:
            import logging
            logging.getLogger("monitor_bot").exception('Ошибка проверки SSL')
        if forgotten and cfg.get("containers_auto") and not muted:
            out.append(Alert(_('🗑 Контейнеры удалены больше суток назад, больше не слежу: ') + ", ".join(forgotten),
                             None, "event"))
        if state.bak_events and not muted:
            out += [Alert(t, None, "event") for t in state.bak_events]
        state.bak_events = []
        if hist is not None:
            try:
                out += smart.ram_trend_events(cfg, state, hist, now, muted, quiet)
            except Exception:
                import logging
                logging.getLogger("monitor_bot").exception('Ошибка прогноза памяти')

    for a in out:
        if a.kind == "event":
            _journal(inc, "event", a.key, a.text, now)
    state.log = [x for x in state.log if now - x[0] < 48 * 3600]
    state.save()
    return out


def _journal(inc, op: str, *args) -> None:
    """Запись в журнал инцидентов не должна ломать проверку."""
    if inc is None:
        return
    try:
        getattr(inc, op)(*args)
    except Exception:
        import logging
        logging.getLogger("monitor_bot").exception('Журнал инцидентов: %s', op)


SEEN_FORGET_SEC = 24 * 3600  # авто-контейнер, которого нет столько времени, больше не отслеживаем


def update_seen_containers(state: AlertState, cfg: dict | None = None, now: float | None = None) -> list[str]:
    """Запомнить работающие контейнеры (для авто-режима) и забыть удалённые больше суток назад.
    Контейнеры из списка containers не забываются никогда. Возвращает забытые имена."""
    states = container_states()
    if states is None:
        return []  # docker недоступен — ничего не трогаем
    now = now or time.time()
    for name, st in states.items():
        if st == "running" and name not in state.seen_containers:
            state.seen_containers.append(name)
    fixed = set((cfg or {}).get("containers") or [])
    forgotten = []
    for name in list(state.seen_containers):
        if name in states or name in fixed:
            state.seen_missing.pop(name, None)
            continue
        first = state.seen_missing.setdefault(name, now)
        if now - first >= SEEN_FORGET_SEC:
            state.seen_containers.remove(name)
            state.seen_missing.pop(name, None)
            forgotten.append(name)
    return forgotten


DEFAULT_LOG_PATTERNS = ["Traceback", "ERROR", "CRITICAL", "Exception", "FATAL"]


def docker_events(cfg: dict, state: AlertState, now: float, recovered: set[str],
                  muted: bool, quiet: bool, info: dict | None = None,
                  logs_fn=None) -> list[Alert]:
    """Перезапуски контейнеров и ошибки в их логах."""
    out: list[Alert] = []
    names = watched_containers(cfg, state)
    if not names:
        return out
    info = container_info(names) if info is None else info
    logs_fn = logs_fn or (lambda n, since: container_logs(n, since=since))

    for name in names:
        i = info.get(name)
        if not i:
            continue
        prev_started = state.ctr_started.get(name)
        prev_restarts = state.ctr_restarts.get(name)
        restarted = prev_started is not None and i["started"] != prev_started
        state.ctr_started[name] = i["started"]
        state.ctr_restarts[name] = i["restarts"]
        if restarted:
            started = docker_time(i["started"])
            if started is not None and state.boot_time and 0 <= started - state.boot_time < BOOT_WINDOW:
                # контейнер поднялся вместе с сервером: об этом уже сообщение «Сервер перезагружался»
                state.log.append([now, f"boot:{name}"])
                continue
            if state.deploy_at and now - state.deploy_at < DEPLOY_WINDOW:
                # вход ключом деплоя и сразу перезапуск — это выкладка новой версии, а не сбой
                out.append(Alert(_('🚀 Деплой: {name} перезапущен с новой версией',
                                   name=name), f"deploy:{name}", "event",
                                 "journal"))
                state.log.append([now, f"deploy:{name}"])
                continue
        # перезапуск, о котором уже сообщили как «упал → восстановлено», второй раз не дублируем
        if restarted and f"ctr:{name}" not in recovered and not muted:
            extra = ""
            if prev_restarts is not None and i["restarts"] > prev_restarts:
                extra = _(' (автоперезапуск Docker, всего {restarts})', restarts=i['restarts'])
            out.append(Alert(_('🔁 Контейнер {name} перезапустился{extra}',
                               name=name, extra=extra), f"log:{name}", "event"))
            state.log.append([now, f"restart:{name}"])

    if not cfg.get("log_alerts", True):
        return out
    patterns = list(cfg.get("log_patterns") or DEFAULT_LOG_PATTERNS)  # с учётом регистра: ERROR, не «error»
    every = (cfg.get("log_alert_min") or 15) * 60
    check_every = int(cfg.get("log_check_min") or 5) * 60  # каждый просмотр логов — вызов помощника через sudo
    for name in names:
        if name not in info:
            continue
        since = state.log_checked.get(name)
        if since is not None and now - since < check_every - 5:
            continue  # ещё рано: в следующий раз прочитаем всё с момента since, ничего не потеряется
        state.log_checked[name] = int(now)
        if since is None:
            continue  # первый запуск: старые ошибки не присылаем
        if muted or quiet or now - state.log_alert_at.get(name, 0) < every:
            continue
        logs = logs_fn(name, since)
        if not logs:
            continue
        hits = [ln.strip() for ln in logs.splitlines() if any(p in ln for p in patterns)]
        if hits:
            state.log_alert_at[name] = now
            sample = "\n".join(h[:200] for h in hits[:3])
            net = network_errors(logs)
            net_at = state.net_fail_at if now - (state.net_fail_at or 0) < NET_WINDOW else 0
            if net or net_at:
                # сбой связи: один инцидент «🌐», без звука — контейнер сам повторит, когда связь вернётся
                when = _(' (у бота связь пропадала в {fmt_local})', fmt_local=fmt_local(cfg, net_at)) if net_at else ""
                state.log.append([now, f"net:{name}"])
                out.append(Alert(_('🌐 Сетевые ошибки в логах {name}: {len} строк{when}. Похоже на сбой связи, а не '
                                   'на ошибку в коде.\n'
                                   '{sample}',
                                   name=name, len=len(hits), when=when, sample=sample), f"log:{name}", "event",
                                 "quiet"))
                continue
            state.log.append([now, f"logerr:{name}"])
            out.append(Alert(_('⚠️ Ошибки в логах {name}: {len} строк\n'
                               '{sample}',
                               name=name, len=len(hits), sample=sample), f"log:{name}", "event"))
    return out


BOOT_WINDOW = 300    # контейнер, стартовавший в первые 5 минут после загрузки сервера, поднялся вместе с ним
DEPLOY_WINDOW = 600  # перезапуск в течение 10 минут после входа ключом деплоя — это деплой
NET_WINDOW = 900     # ошибки в логах в течение 15 минут после сбоя связи у самого бота
NET_SIGNS = ("NetworkError", "TimedOut", "ConnectError", "ConnectTimeout", "ReadTimeout", "ReadError",
             "RemoteProtocolError", "ConnectionError", "ConnectionResetError", "ConnectionRefusedError",
             "Temporary failure in name resolution", "Name or service not known", "Network is unreachable",
             "Connection reset by peer", "Bad Gateway", "Gateway Timeout", "Service Unavailable")


def network_errors(logs: str) -> bool:
    """В логах ошибки, похожие на сбой сети (Telegram/HTTP недоступен), а не на ошибку в коде."""
    return any(s in logs for s in NET_SIGNS)


def docker_time(s: str) -> float | None:
    """2026-11-15T17:19:51.273824495Z → unix time. Docker пишет наносекунды, datetime их не понимает."""
    try:
        head, __, frac = s.rstrip("Z").partition(".")
        ts = datetime.strptime(head, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=ZoneInfo("UTC")).timestamp()
        return ts + (float("0." + frac[:6]) if frac[:6].isdigit() else 0.0)
    except (ValueError, AttributeError):
        return None


def check_reboot(state: AlertState) -> str | None:
    """Вызывается при старте бота: сообщение, если сервер перезагружался с прошлого запуска."""
    boot = psutil.boot_time()
    msg = None
    if state.boot_time and abs(boot - state.boot_time) > 60:
        msg = _('🔄 Сервер перезагружался, аптайм {fmt_duration}', fmt_duration=fmt_duration(time.time() - boot))
        state.oom_kills = None  # счётчик обнулился вместе с ядром
    state.boot_time = boot
    state.save()
    return msg


def summary_text(cfg: dict, state: AlertState, now: float | None = None, hist=None) -> str:
    now = now or time.time()
    day = [x for x in state.log if now - x[0] < 24 * 3600]
    lines = [_('📋 <b>Сводка за сутки</b>'), status_text(cfg), ""]
    p = hist.peaks(86400, now) if hist else None
    if p:
        lines.append(_('Пик CPU: {cpu:.0f}% в {fmt_local}, в среднем {cpu_avg:.0f}%',
                       cpu=p['cpu'][0], fmt_local=fmt_local(cfg, p['cpu'][1]), cpu_avg=p['cpu_avg']))
        lines.append(_('Пик RAM: {ram:.0f}% в {fmt_local}', ram=p['ram'][0], fmt_local=fmt_local(cfg, p['ram'][1])))
        if p["disk_from"] is not None:
            lines.append(_('Диск: {disk_from:.1f}% → {disk_to:.1f}%', disk_from=p['disk_from'], disk_to=p['disk_to']))
        lines.append(_('Трафик за сутки: ↓ {fmt_bytes} ↑ {fmt_bytes2}',
                       fmt_bytes=fmt_bytes(p['rx']), fmt_bytes2=fmt_bytes(p['tx'])))
        days = hist.disk_forecast(now)
        if days is not None and days < 365:
            lines.append(_('⚠️ При текущем темпе диск заполнится примерно через {days:.0f} дн.', days=days))
    restarts = [k for __, k in day if k.startswith("restart:")]
    if restarts:
        lines.append(_('Перезапусков контейнеров: {len}', len=len(restarts)))
    lines.append(_('Алертов за 24 ч: {len}', len=len(day)))
    if day:
        counts: dict[str, int] = {}
        for __, k in day:
            counts[k] = counts.get(k, 0) + 1
        for k, c in sorted(counts.items(), key=lambda x: -x[1])[:8]:
            lines.append(f"  • {html.escape(key_label(k))}: {c}")
    if state.active:
        lines.append(_('Сейчас активны: ') + ", ".join(html.escape(key_label(k)) for k in state.active))
    return "\n".join(lines)
