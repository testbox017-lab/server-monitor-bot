"""Сбор метрик сервера и логика алертов. Работает на Linux и Windows."""
from __future__ import annotations

import html
import os
import platform
import shutil
import subprocess
import time
from dataclasses import dataclass, field

import psutil

IS_WINDOWS = platform.system() == "Windows"


# ---------- Метрики ----------

def fmt_bytes(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ПБ"


def fmt_duration(sec: float) -> str:
    sec = int(sec)
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, _ = divmod(sec, 60)
    parts = []
    if d:
        parts.append(f"{d} д")
    if h or d:
        parts.append(f"{h} ч")
    parts.append(f"{m} мин")
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
        f"⏱ Аптайм: {fmt_duration(s['uptime'])}",
        f"🧠 CPU: {s['cpu']:.0f}% ({s['cpu_count']} ядер)",
    ]
    if s["load"]:
        lines.append("📈 Load avg: " + " / ".join(f"{x:.2f}" for x in s["load"]))
    r = s["ram"]
    lines.append(f"💾 RAM: {r.percent:.0f}% ({fmt_bytes(r.used)} из {fmt_bytes(r.total)})")
    if s["swap"].total:
        lines.append(f"🔁 Swap: {s['swap'].percent:.0f}%")
    for path, d in s["disks"].items():
        lines.append(f"🗄 Диск {path}: {d.percent:.0f}% (свободно {fmt_bytes(d.free)})")
    for name, t in list(s["temps"].items())[:4]:
        lines.append(f"🌡 {name}: {t:.0f}°C")
    lines.append(f"🌐 Сеть: ↓ {fmt_bytes(s['net'].bytes_recv)} ↑ {fmt_bytes(s['net'].bytes_sent)}")
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
    out = [f"🔝 <b>Топ-{n} процессов по {title}</b>", "<pre>  CPU%  RAM%    PID  Имя"]
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


def container_states() -> dict[str, str] | None:
    """{имя контейнера: статус} для всех контейнеров Docker, None если docker недоступен."""
    if not shutil.which("docker"):
        return None
    try:
        r = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}"],
                           capture_output=True, text=True, timeout=15)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    out = {}
    for line in r.stdout.splitlines():
        if "\t" in line:
            name, state = line.split("\t", 1)
            out[name] = state
    return out


def container_state(name: str, states: dict[str, str] | None) -> str:
    if states is None:
        return "docker недоступен"
    return states.get(name, "not found")


def services_text(cfg: dict) -> str:
    lines = ["⚙️ <b>Службы и процессы</b>"]
    for svc in cfg.get("services", []) or []:
        st = service_state(svc)
        icon = "✅" if st == "running" else "❌"
        lines.append(f"{icon} служба {html.escape(svc)}: {html.escape(st)}")
    for proc in cfg.get("processes", []) or []:
        n = process_running(proc)
        icon = "✅" if n else "❌"
        lines.append(f"{icon} процесс {html.escape(proc)}: {'запущен (' + str(n) + ')' if n else 'не найден'}")
    if cfg.get("containers"):
        states = container_states()
        for c in cfg["containers"]:
            st = container_state(c, states)
            icon = "✅" if st == "running" else "❌"
            lines.append(f"{icon} контейнер {html.escape(c)}: {html.escape(st)}")
    if len(lines) == 1:
        lines.append("Список пуст. Добавьте services / processes в config.yaml.")
    return "\n".join(lines)


# ---------- Алерты ----------

@dataclass
class AlertState:
    since: dict[str, float] = field(default_factory=dict)   # когда условие впервые стало плохим
    active: set[str] = field(default_factory=set)            # по каким ключам уже отправили алерт


def evaluate(cfg: dict) -> dict[str, str | None]:
    """Возвращает {ключ: описание проблемы или None, если всё в порядке}."""
    th = cfg.get("thresholds", {})
    s = collect(cfg)
    res: dict[str, str | None] = {}

    def check(key, value, limit, label):
        if limit is None:
            return
        res[key] = f"{label}: {value:.0f}% (порог {limit}%)" if value >= limit else None

    check("cpu", s["cpu"], th.get("cpu"), "🔥 Высокая загрузка CPU")
    check("ram", s["ram"].percent, th.get("ram"), "💾 Мало свободной памяти, RAM")
    if s["swap"].total:
        check("swap", s["swap"].percent, th.get("swap"), "🔁 Активный swap")
    for path, d in s["disks"].items():
        check(f"disk:{path}", d.percent, th.get("disk"), f"🗄 Заканчивается место на {path}")
    if th.get("temp") is not None:
        for name, t in s["temps"].items():
            res[f"temp:{name}"] = (f"🌡 Перегрев {name}: {t:.0f}°C (порог {th['temp']}°C)"
                                   if t >= th["temp"] else None)
    for svc in cfg.get("services", []) or []:
        st = service_state(svc)
        res[f"svc:{svc}"] = None if st == "running" else f"❌ Служба {svc} не работает ({st})"
    for proc in cfg.get("processes", []) or []:
        res[f"proc:{proc}"] = None if process_running(proc) else f"❌ Процесс {proc} не найден"
    if cfg.get("containers"):
        states = container_states()
        for c in cfg["containers"]:
            st = container_state(c, states)
            res[f"ctr:{c}"] = None if st == "running" else f"❌ Контейнер {c} не работает ({st})"
    return res


def instant_keys(key: str) -> bool:
    """Падение службы/процесса/контейнера сообщаем сразу, ресурсы только после duration."""
    return key.startswith(("svc:", "proc:", "ctr:"))


def step(cfg: dict, state: AlertState, now: float | None = None) -> list[str]:
    """Одна итерация проверки. Возвращает сообщения для отправки."""
    now = now or time.time()
    duration = cfg.get("thresholds", {}).get("duration_sec", 300)
    messages = []
    for key, problem in evaluate(cfg).items():
        if problem:
            state.since.setdefault(key, now)
            long_enough = instant_keys(key) or now - state.since[key] >= duration
            if long_enough and key not in state.active:
                state.active.add(key)
                messages.append(f"🚨 {problem}")
        else:
            state.since.pop(key, None)
            if key in state.active:
                state.active.discard(key)
                messages.append(f"✅ Восстановлено: {key.split(':', 1)[-1]}")
    return messages
