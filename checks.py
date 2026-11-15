"""Сбор метрик сервера и логика алертов. Работает на Linux и Windows."""
from __future__ import annotations

import html
import json
import os
import platform
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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
    stats = container_stats()
    if stats:
        lines.append("🐳 Контейнеры:")
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


def docker(*args: str, timeout: int = 15) -> subprocess.CompletedProcess | None:
    if not shutil.which("docker"):
        return None
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
    except Exception:
        return None


def container_info(names: list[str]) -> dict[str, dict]:
    """{имя: {"restarts": int, "started": str, "health": str}} через docker inspect."""
    if not names:
        return {}
    fmt = "{{.Name}}\t{{.RestartCount}}\t{{.State.StartedAt}}\t{{if .State.Health}}{{.State.Health.Status}}{{end}}"
    r = docker("inspect", "--format", fmt, *names)
    out: dict[str, dict] = {}
    if r is None:
        return out
    for line in r.stdout.splitlines():  # для несуществующих имён docker пишет ошибку в stderr, остальные выводит
        parts = line.split("\t")
        if len(parts) == 4:
            try:
                restarts = int(parts[1])
            except ValueError:
                restarts = 0
            out[parts[0].lstrip("/")] = {"restarts": restarts, "started": parts[2], "health": parts[3]}
    return out


def container_stats() -> list[tuple[str, str, str]]:
    """[(имя, CPU%, память)] для работающих контейнеров."""
    r = docker("stats", "--no-stream", "--format", "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}", timeout=20)
    if r is None or r.returncode != 0:
        return []
    rows = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            rows.append((parts[0], parts[1], parts[2].split(" / ")[0]))
    return sorted(rows)


def container_logs(name: str, tail: int = 30, since: int | None = None) -> str | None:
    args = ["logs", "--tail", str(tail)] if since is None else ["logs", "--since", str(since)]
    r = docker(*args, name, timeout=20)
    if r is None or r.returncode != 0:
        return None
    return (r.stdout or "") + (r.stderr or "")  # docker отдаёт stderr контейнера в stderr


def logs_text(name: str, tail: int = 30) -> str:
    logs = container_logs(name, tail)
    if logs is None:
        return f"📜 Не удалось получить логи {html.escape(name)} (нет контейнера или доступа к docker)"
    logs = logs.strip() or "(пусто)"
    if len(logs) > 3500:
        logs = "…" + logs[-3500:]
    return f"📜 <b>{html.escape(name)}</b>, последние {tail} строк\n<pre>{html.escape(logs)}</pre>"


def watched_containers(cfg: dict, state: "AlertState | None" = None) -> list[str]:
    """Контейнеры из настроек плюс (в авто-режиме) все, которые бот когда-либо видел работающими."""
    names = list(cfg.get("containers") or [])
    if cfg.get("containers_auto") and state is not None:
        for n in state.seen_containers:
            if n not in names:
                names.append(n)
    return names


def services_text(cfg: dict, state: "AlertState | None" = None) -> str:
    lines = ["⚙️ <b>Службы и процессы</b>"]
    for svc in cfg.get("services", []) or []:
        st = service_state(svc)
        icon = "✅" if st == "running" else "❌"
        lines.append(f"{icon} служба {html.escape(svc)}: {html.escape(st)}")
    for proc in cfg.get("processes", []) or []:
        n = process_running(proc)
        icon = "✅" if n else "❌"
        lines.append(f"{icon} процесс {html.escape(proc)}: {'запущен (' + str(n) + ')' if n else 'не найден'}")
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
                extra.append(f"перезапусков: {i['restarts']}")
            ok = st == "running" and i.get("health") != "unhealthy"
            tail = f" ({', '.join(extra)})" if extra else ""
            lines.append(f"{'✅' if ok else '❌'} контейнер {html.escape(c)}: {html.escape(st)}{html.escape(tail)}")
    if len(lines) == 1:
        lines.append("Список пуст. Добавьте services / processes в config.yaml.")
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

CRITICAL_PREFIXES = ("svc:", "proc:", "ctr:")


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

    path: Path | None = field(default=None, repr=False, compare=False)

    @classmethod
    def load(cls, path: Path) -> "AlertState":
        st = cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for k, v in data.items():
                if k in cls.__dataclass_fields__ and k != "path":
                    setattr(st, k, v)
        except (OSError, ValueError):
            pass
        st.path = path
        return st

    def save(self) -> None:
        if not self.path:
            return
        data = {k: v for k, v in asdict(self).items() if k != "path"}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def muted(self, now: float) -> bool:
        return now < self.mute_until


def key_label(key: str) -> str:
    return key.split(":", 1)[-1]


def evaluate(cfg: dict, state: "AlertState | None" = None) -> dict[str, str | None]:
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
    containers = watched_containers(cfg, state)
    if containers:
        states = container_states()
        info = container_info(containers)
        for c in containers:
            st = container_state(c, states)
            health = info.get(c, {}).get("health")
            if st != "running":
                res[f"ctr:{c}"] = f"❌ Контейнер {c} не работает ({st})"
            elif health == "unhealthy":
                res[f"ctr:{c}"] = f"❌ Контейнер {c} работает, но healthcheck: unhealthy"
            else:
                res[f"ctr:{c}"] = None
    res["reboot_required"] = "♻️ Серверу нужна перезагрузка после обновлений" if reboot_required() else None
    return res


def instant_keys(key: str) -> bool:
    """Падение службы/процесса/контейнера сообщаем сразу, ресурсы только после duration."""
    return key.startswith(CRITICAL_PREFIXES) or key == "reboot_required"


@dataclass
class Alert:
    text: str
    key: str | None = None   # ключ проблемы, для кнопок под сообщением
    kind: str = "alert"      # alert / remind / recovered / event


def step(cfg: dict, state: AlertState, now: float | None = None,
         results: dict[str, str | None] | None = None) -> list[Alert]:
    """Одна итерация проверки. Возвращает сообщения для отправки и сохраняет состояние."""
    now = now or time.time()
    th = cfg.get("thresholds", {})
    duration = th.get("duration_sec", 300)
    remind = (cfg.get("remind_min") or 0) * 60
    muted = state.muted(now)
    quiet = in_quiet_hours(cfg, now)
    out: list[Alert] = []
    real = results is None
    if real:
        update_seen_containers(state)
    results = evaluate(cfg, state) if real else results
    recovered: set[str] = set()

    for key, problem in results.items():
        if problem:
            state.since.setdefault(key, now)
            if not (instant_keys(key) or now - state.since[key] >= duration):
                continue
            suppressed = muted or (quiet and not is_critical(key))
            if suppressed:
                continue  # отправим, когда тишина закончится, если проблема ещё есть
            if key not in state.active:
                state.active.append(key)
                state.last_sent[key] = now
                state.problems[key] = problem
                state.log.append([now, key])
                out.append(Alert(f"🚨 {problem}", key, "alert"))
            elif remind and now - state.last_sent.get(key, now) >= remind:
                state.last_sent[key] = now
                state.problems[key] = problem
                mins = int((now - state.since[key]) // 60)
                out.append(Alert(f"⏰ Всё ещё не решено ({mins} мин): {problem}", key, "remind"))
        else:
            state.since.pop(key, None)
            if key in state.active:
                state.active.remove(key)
                state.last_sent.pop(key, None)
                state.problems.pop(key, None)
                recovered.add(key)
                if not muted:
                    out.append(Alert(f"✅ Восстановлено: {key_label(key)}", key, "recovered"))

    # Ключи, которых больше нет в проверке (убрали из настроек) — забываем молча
    for key in list(state.active):
        if key not in results:
            state.active.remove(key)
            state.problems.pop(key, None)
            state.last_sent.pop(key, None)
    for key in list(state.since):
        if key not in results:
            state.since.pop(key)

    # OOM killer — событие, не состояние
    n = oom_kills()
    if n is not None:
        if state.oom_kills is not None and n > state.oom_kills and not muted:
            out.append(Alert(f"💥 OOM killer убил процесс(ы): {n - state.oom_kills}. "
                             "Не хватило памяти (swap на сервере нет).", None, "event"))
            state.log.append([now, "oom"])
        state.oom_kills = n

    if real:
        out += docker_events(cfg, state, now, recovered, muted, quiet)

    state.log = [x for x in state.log if now - x[0] < 48 * 3600]
    state.save()
    return out


def update_seen_containers(state: AlertState) -> None:
    states = container_states()
    for name, st in (states or {}).items():
        if st == "running" and name not in state.seen_containers:
            state.seen_containers.append(name)


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
        # перезапуск, о котором уже сообщили как «упал → восстановлено», второй раз не дублируем
        if restarted and f"ctr:{name}" not in recovered and not muted:
            extra = ""
            if prev_restarts is not None and i["restarts"] > prev_restarts:
                extra = f" (автоперезапуск Docker, всего {i['restarts']})"
            out.append(Alert(f"🔁 Контейнер {name} перезапустился{extra}", f"log:{name}", "event"))
            state.log.append([now, f"restart:{name}"])

    if not cfg.get("log_alerts", True):
        return out
    patterns = list(cfg.get("log_patterns") or DEFAULT_LOG_PATTERNS)  # с учётом регистра: ERROR, не «error»
    every = (cfg.get("log_alert_min") or 15) * 60
    for name in names:
        if name not in info:
            continue
        since = state.log_checked.get(name)
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
            state.log.append([now, f"logerr:{name}"])
            sample = "\n".join(h[:200] for h in hits[:3])
            out.append(Alert(f"⚠️ Ошибки в логах {name}: {len(hits)} строк\n{sample}", f"log:{name}", "event"))
    return out


def check_reboot(state: AlertState) -> str | None:
    """Вызывается при старте бота: сообщение, если сервер перезагружался с прошлого запуска."""
    boot = psutil.boot_time()
    msg = None
    if state.boot_time and abs(boot - state.boot_time) > 60:
        msg = f"🔄 Сервер перезагружался, аптайм {fmt_duration(time.time() - boot)}"
        state.oom_kills = None  # счётчик обнулился вместе с ядром
    state.boot_time = boot
    state.save()
    return msg


def summary_text(cfg: dict, state: AlertState, now: float | None = None) -> str:
    now = now or time.time()
    day = [x for x in state.log if now - x[0] < 24 * 3600]
    lines = ["📋 <b>Сводка за сутки</b>", status_text(cfg), "",
             f"Алертов за 24 ч: {len(day)}"]
    if day:
        counts: dict[str, int] = {}
        for _, k in day:
            counts[k] = counts.get(k, 0) + 1
        for k, c in sorted(counts.items(), key=lambda x: -x[1])[:8]:
            lines.append(f"  • {html.escape(key_label(k))}: {c}")
    if state.active:
        lines.append("Сейчас активны: " + ", ".join(html.escape(key_label(k)) for k in state.active))
    return "\n".join(lines)
