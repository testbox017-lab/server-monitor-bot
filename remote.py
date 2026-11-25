"""Другие серверы: бот заходит на них по SSH и спрашивает состояние у агента deploy/monitor-agent.

В config.yaml:
    remote_servers:
      - name: ovh                 # как сервер называется в сообщениях
        host: 57.129.160.122
        user: root                # ключ на том сервере ограничен command="/usr/local/sbin/monitor-agent",restrict
        key: /var/lib/monitor_bot/remote_ed25519
        services: [ssh]
        containers: []            # имена контейнеров; containers_auto: true — следить за всеми

Ключи алертов удалённого сервера: «имя|cpu», «имя|svc:ssh», «имя|down» (сервер не отвечает) и т.п.
Пороги общие с главным сервером (thresholds). Если сервер не отвечает, его прежние проблемы не
«выздоравливают», а остаются как были, пока связь не вернётся.
"""
from __future__ import annotations

import html
import json
import re
import subprocess
import threading
import time
from pathlib import Path

from i18n import _

SEP = "|"
DOWN_AFTER = 2          # столько неудачных опросов подряд — и сервер считается недоступным
SSH_TIMEOUT = 40
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
HOST_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,253}$")
SVC_RE = re.compile(r"^[A-Za-z0-9@_.:-]{1,100}$")

LAST: dict[str, dict] = {}   # имя → {"ts": время ответа, "data": ответ агента} — для /servers без нового опроса
ERRORS: dict[str, str] = {}  # имя → последняя ошибка связи
_lock = threading.Lock()


def servers(cfg: dict) -> list[dict]:
    """Описания серверов из config.yaml; с неверным именем или адресом пропускаются."""
    out = []
    for s in cfg.get("remote_servers") or []:
        if not isinstance(s, dict):
            continue
        name, host = str(s.get("name") or ""), str(s.get("host") or "")
        if NAME_RE.match(name) and HOST_RE.match(host) and not host.startswith("-"):
            out.append(s)
    return out


def is_remote(key: str | None) -> bool:
    return bool(key) and SEP in key


def split_key(key: str) -> tuple[str | None, str]:
    """«ovh|svc:ssh» → ("ovh", "svc:ssh"); ключ своего сервера → (None, ключ)."""
    if SEP in key:
        srv, local = key.split(SEP, 1)
        return srv, local
    return None, key


def key_dir(cfg: dict, data_dir: Path | None) -> Path:
    return Path(cfg.get("remote_key_dir") or data_dir or ".")
def ssh_base(srv: dict, cfg: dict, data_dir: Path | None) -> list[str]:
    d = key_dir(cfg, data_dir)
    key = str(srv.get("key") or d / "remote_ed25519")
    return ["ssh", "-i", key, "-p", str(int(srv.get("port") or 22)),
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={d / 'known_hosts'}",
            "-o", "LogLevel=ERROR", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2",
            f"{srv.get('user') or 'root'}@{srv['host']}"]


def ssh_cmd(srv: dict, cfg: dict, data_dir: Path | None, since: int | None = None) -> list[str]:
    services = [x for x in (srv.get("services") or []) if SVC_RE.match(str(x))]
    return ssh_base(srv, cfg, data_dir) + [
        ("status " + ",".join(services) + (f" since={int(since)}" if since else "")).strip()]


# ---------- Действия (агент на том сервере проверяет каждую команду сам) ----------

CFG: dict = {}             # бот подставляет свои настройки и папку данных при запуске (setup)
DATA_DIR: Path | None = None
CTR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
KINDS = {"rsvc", "rctr", "rban", "runban", "rreboot", "rapt"}


def setup(cfg: dict, data_dir: Path | None) -> None:
    global CFG, DATA_DIR
    CFG, DATA_DIR = cfg, data_dir


def server(name: str, cfg: dict | None = None) -> dict | None:
    return next((s for s in servers(cfg if cfg is not None else CFG) if s["name"] == name), None)


def run_agent(srv: dict, args: list[str], timeout: int = 180, cfg: dict | None = None) -> tuple[bool, str]:
    """Выполнить действие агентом: (успех, вывод)."""
    cfg = cfg if cfg is not None else CFG
    try:
        r = subprocess.run(ssh_base(srv, cfg, DATA_DIR) + [" ".join(args)], capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, _('нет ответа за {n} с', n=timeout)
    except OSError as e:
        return False, str(e)
    try:
        data = json.loads(r.stdout)
    except ValueError:
        err = (r.stderr or r.stdout).strip().splitlines()
        return False, (err[-1] if err else f"ssh exit {r.returncode}")[:300]
    if "error" in data:
        return False, str(data["error"])
    return bool(data.get("ok")), str(data.get("out", ""))


def agent_args(kind: str, target: str) -> list[str] | None:
    """Команда агенту для действия из подтверждения; None — неверная цель."""
    import security
    if kind == "rsvc" and SVC_RE.match(target):
        return ["restart", "svc", target]
    if kind == "rctr" and CTR_RE.match(target):
        return ["restart", "ctr", target]
    if kind in ("rban", "runban") and security.public_ip(target):
        return ["ban" if kind == "rban" else "unban", target]
    if kind == "rapt" and target in ("security", "all"):
        return ["apt-upgrade", target]
    if kind == "rreboot":
        return ["reboot"]
    return None


def execute(kind: str, arg: str) -> tuple[bool, str]:
    """arg — «сервер|цель»."""
    name, __, target = arg.partition(SEP)
    srv = server(name)
    if srv is None:
        return False, _('Сервера {name} нет в remote_servers', name=name)
    args = agent_args(kind, target)
    if args is None:
        return False, _('неверная цель: {target}', target=target)
    return run_agent(srv, args)


def logs(name: str, kind: str, target: str, tail: int) -> str:
    srv = server(name)
    ok_name = SVC_RE.match(target) if kind == "svc" else CTR_RE.match(target)
    if srv is None or kind not in ("svc", "ctr") or not ok_name:
        return _('неверная цель: {target}', target=target)
    ok, out = run_agent(srv, ["logs", kind, target, str(max(1, min(int(tail), 200)))], timeout=60)
    return out if ok or out else _('ошибка без вывода')


# ---------- История ----------

HISTS: dict = {}


def history(name: str, data_dir: Path | None):
    """history.History другого сервера: свой файл metrics-ИМЯ.db рядом с metrics.db."""
    if data_dir is None:
        return None
    if name not in HISTS:
        import history as h
        HISTS[name] = h.History(Path(data_dir) / f"metrics-{name}.db")
    return HISTS[name]


def record_history(name: str, data_dir: Path | None, now: float, data: dict) -> None:
    hist = history(name, data_dir)
    if hist is None:
        return
    disks = data.get("disks") or {}
    root = disks.get("/")
    disk = root.get("percent") if root else max((d.get("percent", 0) for d in disks.values()), default=None)
    net = data.get("net") or {}
    load = (data.get("load") or [None])[0]
    hist.record_row(now, data.get("cpu"), (data.get("ram") or {}).get("percent"), disk,
                    net.get("rx"), net.get("tx"), load)


def fetch(srv: dict, cfg: dict, data_dir: Path | None, since: int | None = None) -> tuple[dict | None, str]:
    """(ответ агента, "") или (None, текст ошибки)."""
    try:
        r = subprocess.run(ssh_cmd(srv, cfg, data_dir, since), capture_output=True, text=True, timeout=SSH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, _('нет ответа за {n} с', n=SSH_TIMEOUT)
    except OSError as e:
        return None, str(e)
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip().splitlines()
        return None, (err[-1] if err else f"ssh exit {r.returncode}")[:200]
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return None, _('агент ответил не JSON')
    if not isinstance(data, dict) or "error" in data:
        return None, str(data.get("error") if isinstance(data, dict) else data)[:200]
    return data, ""


def watched_containers(srv: dict, data: dict) -> list[str]:
    names = [str(c) for c in srv.get("containers") or []]
    if srv.get("containers_auto"):
        names += [c for c in (data.get("containers") or {}) if c not in names]
    return names


def results_from(srv: dict, cfg: dict, data: dict) -> dict[str, str | None]:
    """Проверки по ответу агента — те же пороги и тексты, что у главного сервера."""
    name = srv["name"]
    th = cfg.get("thresholds", {})
    pfx = f"[{name}] "
    res: dict[str, str | None] = {}

    def check(key, value, limit, label):
        if limit is None or value is None:
            return
        res[name + SEP + key] = (pfx + _('{label}: {value:.0f}% (порог {limit}%)', label=label, value=value, limit=limit)
                                 if value >= limit else None)

    check("cpu", data.get("cpu"), th.get("cpu"), _('🔥 Высокая загрузка CPU'))
    check("ram", (data.get("ram") or {}).get("percent"), th.get("ram"), _('💾 Мало свободной памяти, RAM'))
    if (data.get("swap") or {}).get("total"):
        check("swap", data["swap"].get("percent"), th.get("swap"), _('🔁 Активный swap'))
    for path, d in (data.get("disks") or {}).items():
        check(f"disk:{path}", d.get("percent"), th.get("disk"), _('🗄 Заканчивается место на {path}', path=path))
    for svc, st in (data.get("services") or {}).items():
        res[f"{name}{SEP}svc:{svc}"] = None if st == "running" else pfx + _('❌ Служба {svc} не работает ({st})',
                                                                             svc=svc, st=st)
    ctrs = data.get("containers")
    if ctrs is not None:
        for c in watched_containers(srv, data):
            info = ctrs.get(c) or {}
            st = info.get("state") or "not-found"
            if st != "running":
                res[f"{name}{SEP}ctr:{c}"] = pfx + _('❌ Контейнер {c} не работает ({st})', c=c, st=st)
            elif info.get("health") == "unhealthy":
                res[f"{name}{SEP}ctr:{c}"] = pfx + _('❌ Контейнер {c} работает, но завис или не в порядке (healthcheck)', c=c)
            else:
                res[f"{name}{SEP}ctr:{c}"] = None
    res[f"{name}{SEP}reboot_required"] = (pfx + _('♻️ Серверу нужна перезагрузка после обновлений')
                                          if data.get("reboot_required") else None)
    return res


def backup_results(cfg: dict, state, now: float, srv: dict, data: dict) -> dict[str, str | None]:
    """Копии на другом сервере (папки его /etc/monitor-agent/backup-watch) и когда их забирал ПК — те же
    проверки и тексты, что для своих; ключи «имя|bak:ПАПКА» и «имя|bak:pc»."""
    import backups
    name = srv["name"]
    pfx = f"[{name}] "
    res: dict[str, str | None] = {}
    if not cfg.get("backup_alerts", True):
        return res
    for st in data.get("backups") or []:
        if not isinstance(st, dict) or not st.get("path") or not st.get("max_age_h"):
            continue
        p = backups.folder_problem(st, state, now, f"{name}{SEP}{st['path']}")
        res[f"{name}{SEP}bak:{st['path']}"] = pfx + p if p else None
    if data.get("offsite") is not None:
        p = backups.offsite_problem(cfg, data["offsite"], now)
        res[f"{name}{SEP}bak:pc"] = pfx + p if p else None
    return res


def evaluate(cfg: dict, state, now: float, data_dir: Path | None = None, fetcher=None) -> dict[str, str | None]:
    """Опросить все удалённые серверы. state — checks.AlertState (remote_fails, remote_last)."""
    fetcher = fetcher or fetch
    res: dict[str, str | None] = {}
    for srv in servers(cfg):
        name = srv["name"]
        data, err = fetcher(srv, cfg, data_dir, state.remote_ssh_at.get(name))
        down_key = name + SEP + "down"
        if data is not None:
            with _lock:
                LAST[name] = {"ts": now, "data": data, "new": True}
                ERRORS.pop(name, None)
            state.remote_fails[name] = 0
            try:
                record_history(name, data_dir, now, data)
            except Exception:
                import logging
                logging.getLogger("monitor_bot").exception("Не удалось записать историю %s", name)
            r = results_from(srv, cfg, data)
            r.update(backup_results(cfg, state, now, srv, data))
            state.remote_last[name] = r
            res.update(r)
            res[down_key] = None
        else:
            with _lock:
                ERRORS[name] = err
            n = state.remote_fails.get(name, 0) + 1
            state.remote_fails[name] = n
            res.update(state.remote_last.get(name, {}))  # без связи прежние проблемы не считаем решёнными
            res[down_key] = (f"[{name}] " + _('📡 Сервер не отвечает: {err}', err=err)) if n >= DOWN_AFTER else None
    known = {s["name"] for s in servers(cfg)}
    for name in list(state.remote_last):  # сервер убрали из config.yaml
        if name not in known:
            for d in (state.remote_last, state.remote_fails, state.remote_ssh_at, state.remote_known_ips,
                      state.remote_ssh_failed, state.remote_fail_alert_at, state.remote_ports,
                      state.remote_upd_alert_at):
                d.pop(name, None)
    return res


def fmt_bytes(n: float) -> str:
    import checks
    return checks.fmt_bytes(n)


def server_text(srv: dict, cfg: dict, now: float | None = None, state=None) -> str:
    import checks
    now = now or time.time()
    name = srv["name"]
    with _lock:
        last, err = LAST.get(name), ERRORS.get(name)
    if not last:
        line = f"🖥 <b>{html.escape(name)}</b>\n"
        return line + (_('📡 Нет связи: {err}', err=html.escape(err)) if err
                       else _('Данных ещё нет: первая проверка через минуту'))
    s = last["data"]
    lines = [f"🖥 <b>{html.escape(name)}</b> ({html.escape(str(s.get('os', '')))})"]
    if err:
        lines.append(_('📡 Нет связи: {err}', err=html.escape(err)))
    lines.append(_('🕑 Данные {n} с назад', n=int(now - last["ts"])))
    lines.append(_('⏱ Аптайм: {fmt_duration}', fmt_duration=checks.fmt_duration(s.get("uptime", 0))))
    lines.append(_('🧠 CPU: {cpu:.0f}% ({cpu_count} ядер)', cpu=s.get("cpu", 0), cpu_count=s.get("cpu_count", "?")))
    if s.get("load"):
        lines.append("📈 Load avg: " + " / ".join(f"{x:.2f}" for x in s["load"]))
    r = s.get("ram") or {}
    lines.append(_('💾 RAM: {percent:.0f}% ({fmt_bytes} из {fmt_bytes2})', percent=r.get("percent", 0),
                   fmt_bytes=fmt_bytes(r.get("used", 0)), fmt_bytes2=fmt_bytes(r.get("total", 0))))
    if (s.get("swap") or {}).get("total"):
        lines.append(f"🔁 Swap: {s['swap'].get('percent', 0):.0f}%")
    for path, d in (s.get("disks") or {}).items():
        lines.append(_('🗄 Диск {path}: {percent:.0f}% (свободно {fmt_bytes})',
                       path=html.escape(path), percent=d.get("percent", 0), fmt_bytes=fmt_bytes(d.get("free", 0))))
    net = s.get("net")
    if net:
        lines.append(_('🌐 Сеть: ↓ {fmt_bytes} ↑ {fmt_bytes2}',
                       fmt_bytes=fmt_bytes(net.get("rx", 0)), fmt_bytes2=fmt_bytes(net.get("tx", 0))))
    for svc, st in (s.get("services") or {}).items():
        lines.append(_('{icon} служба {escape}: {escape2}', icon="✅" if st == "running" else "❌",
                       escape=html.escape(svc), escape2=html.escape(st)))
    ctrs = s.get("containers")
    if ctrs:
        lines.append(_('🐳 Контейнеры:'))
        for c, i in ctrs.items():
            ok = i.get("state") == "running" and i.get("health") != "unhealthy"
            extra = f", {i['health']}" if i.get("health") else ""
            lines.append(f"  {'✅' if ok else '❌'} {html.escape(c)}: {html.escape(str(i.get('state')))}{html.escape(extra)}")
    upd = updates_line(name)
    if upd:
        lines.append(upd)
    if s.get("reboot_required"):
        lines.append(_('♻️ Серверу нужна перезагрузка после обновлений'))
    if state is not None:
        fails = sum(x[2] for x in state.remote_ssh_failed.get(name, []) if now - x[0] < 3600)
        lines.append(_('🛡 Неудачных входов по SSH за час: {n}', n=fails))
    f2b = s.get("fail2ban")
    if f2b:
        lines.append(_('🚫 fail2ban: заблокировано сейчас {banned}, всего {total}',
                       banned=sum(j.get("banned", 0) for j in f2b.values()),
                       total=sum(j.get("total", 0) for j in f2b.values())))
    if s.get("ports"):
        lines.append(_('🚪 Порты наружу: {ports}', ports=html.escape(", ".join(s["ports"]))))
    return "\n".join(lines)


def events(cfg: dict, state, now: float, muted: bool, quiet: bool) -> list:
    """События по свежим ответам агентов: входы по SSH, подбор пароля, новые открытые порты.
    Как у своего сервера (security.events), только с именем другого сервера в заголовке."""
    import security
    from checks import Alert
    out: list = []
    for srv in servers(cfg):
        name = srv["name"]
        with _lock:
            last = LAST.get(name)
            if not last or not last.get("new"):
                continue
            last["new"] = False
        data = last["data"]
        if data.get("time"):
            state.remote_ssh_at[name] = int(data["time"])  # с этого момента агент пришлёт журнал в следующий раз
        lines = data.get("ssh")
        if cfg.get("ssh_alerts", True) and isinstance(lines, list):
            accepted, failed = security.parse_ssh("\n".join(str(x) for x in lines))
            known = state.remote_known_ips.setdefault(name, {})
            for user, ip, method, fp in accepted:
                if security.is_deploy(cfg, ip, fp):
                    state.remote_deploy_at[name] = now  # перезапуск контейнера следом — это выкладка
                    continue
                new = ip not in known and ip not in state.known_ips  # с этого IP уже входили на главный сервер
                known[ip] = now
                if muted or not (new or cfg.get("ssh_alert_all_logins")):
                    continue
                tag = _('с нового IP') if new else ""
                out.append(Alert(_('🔐 Вход по SSH: {user} {tag} {ip} ({method})',
                                   user=user, tag=tag, ip=ip, method=method).replace("  ", " "),
                                 None, "event", server=name))
                state.log.append([now, f"{name}{SEP}ssh:{ip}"])
            fl = state.remote_ssh_failed.setdefault(name, [])
            fl += [[now, ip, n] for ip, n in failed.items()]
        fl = [x for x in state.remote_ssh_failed.get(name, []) if now - x[0] < 3600]
        state.remote_ssh_failed[name] = fl
        recent = [x for x in fl if now - x[0] < 600]
        total = sum(x[2] for x in recent)
        threshold = cfg.get("ssh_fail_threshold", 30)
        if (threshold and total >= threshold and not quiet and not muted
                and now - state.remote_fail_alert_at.get(name, 0) >= 3600):
            by_ip: dict[str, int] = {}
            for __, ip, n in recent:
                by_ip[ip] = by_ip.get(ip, 0) + n
            top = ", ".join(f"{ip} ({n})" for ip, n in sorted(by_ip.items(), key=lambda x: -x[1])[:3])
            out.append(Alert(_('🛡 Подбор пароля по SSH: {total} неудачных попыток за 10 мин. Чаще всего: {top}',
                               total=total, top=top), f"{name}{SEP}brute:{max(by_ip, key=by_ip.get)}", "event",
                             server=name))
            state.remote_fail_alert_at[name] = now
            state.log.append([now, f"{name}{SEP}ssh_bruteforce"])
        ports = data.get("ports")
        if cfg.get("port_alerts", True) and isinstance(ports, list) and not muted:
            known_p = state.remote_ports.get(name)
            if not known_p:
                state.remote_ports[name] = sorted(ports)  # первый ответ: запоминаем как норму
            else:
                new_p = sorted(set(ports) - set(known_p))
                for p in new_p:
                    out.append(Alert(_('🚪 Новый открытый наружу порт: {p}', p=p), None, "event", server=name))
                    state.log.append([now, f"{name}{SEP}port:{p}"])
                if new_p:
                    state.remote_ports[name] = sorted(set(known_p) | set(new_p))
        out += container_events(cfg, state, now, srv, data, muted, quiet)
    return out


def container_events(cfg: dict, state, now: float, srv: dict, data: dict, muted: bool, quiet: bool) -> list:
    """Перезапуски контейнеров другого сервера и ошибки в их логах — как docker_events у своего."""
    from checks import Alert, BOOT_WINDOW, DEPLOY_WINDOW, DEFAULT_LOG_PATTERNS, docker_time, network_errors
    name = srv["name"]
    out: list = []
    ctrs = data.get("containers") or {}
    watched = watched_containers(srv, data)
    boot = (data.get("time") or now) - float(data.get("uptime") or 0)
    for c in watched:
        i = ctrs.get(c) or {}
        started = i.get("started")
        if not started:
            continue  # старый агент без StartedAt или контейнера нет (об этом — алерт «не работает»)
        skey = f"{name}{SEP}{c}"
        prev = state.remote_ctr_started.get(skey)
        state.remote_ctr_started[skey] = started
        if prev is None or prev == started:
            continue
        ts = docker_time(started)
        if ts is not None and 0 <= ts - boot < BOOT_WINDOW:
            state.log.append([now, f"{name}{SEP}boot:{c}"])  # поднялся вместе с сервером
            continue
        if now - state.remote_deploy_at.get(name, 0) < DEPLOY_WINDOW:
            out.append(Alert(_('🚀 Деплой: {name} перезапущен с новой версией', name=c),
                             f"{name}{SEP}deploy:{c}", "event", "journal", server=name))
            state.log.append([now, f"{name}{SEP}deploy:{c}"])
            continue
        if muted:
            continue
        extra = _(' (автоперезапуск Docker, всего {restarts})', restarts=i["restarts"]) if i.get("restarts") else ""
        out.append(Alert(_('🔁 Контейнер {name} перезапустился{extra}', name=c, extra=extra),
                         f"{name}{SEP}log:{c}", "event", server=name))
        state.log.append([now, f"{name}{SEP}restart:{c}"])

    logs = data.get("ctr_logs")
    if not cfg.get("log_alerts", True) or not isinstance(logs, dict) or muted or quiet:
        return out
    patterns = list(cfg.get("log_patterns") or DEFAULT_LOG_PATTERNS)
    every = (cfg.get("log_alert_min") or 15) * 60
    for c in watched:
        lines = [str(x) for x in logs.get(c) or []]
        hits = [ln for ln in lines if any(p in ln for p in patterns)]
        skey = f"{name}{SEP}{c}"
        if not hits or now - state.log_alert_at.get(skey, 0) < every:
            continue
        state.log_alert_at[skey] = now
        sample = "\n".join(h[:200] for h in hits[:3])
        if network_errors("\n".join(lines)):
            state.log.append([now, f"{name}{SEP}net:{c}"])
            out.append(Alert(_('🌐 Сетевые ошибки в логах {name}: {len} строк{when}. Похоже на сбой связи, а не '
                               'на ошибку в коде.\n'
                               '{sample}', name=c, len=len(hits), when="", sample=sample),
                             f"{name}{SEP}log:{c}", "event", "quiet", server=name))
        else:
            state.log.append([now, f"{name}{SEP}logerr:{c}"])
            out.append(Alert(_('⚠️ Ошибки в логах {name}: {len} строк\n'
                               '{sample}', name=c, len=len(hits), sample=sample),
                             f"{name}{SEP}log:{c}", "event", server=name))
    return out


def servers_text(cfg: dict, state=None) -> str:
    srvs = servers(cfg)
    if not srvs:
        return _('Других серверов нет. Добавьте remote_servers в config.yaml (см. README).')
    return "\n\n".join(server_text(s, cfg, None, state) for s in srvs)


# ---------- Карточки другого сервера: как у своего (процессы, службы, сеть и диск, обновления) ----------

UPDATES: dict[str, dict] = {}   # имя → {"ts": когда проверяли, "pkgs": [{name, security}]}
UPDATES_EVERY = 6 * 3600


def last_data(name: str) -> dict:
    with _lock:
        last = LAST.get(name)
    return (last or {}).get("data") or {}


def procs_text(name: str, by: str, n: int) -> str:
    srv = server(name)
    if srv is None:
        return _('Сервера {name} нет в remote_servers', name=name)
    n = max(1, min(int(n), 30))
    ok, out = run_agent(srv, ["procs", "cpu" if by == "cpu" else "mem", str(n)], timeout=60)
    try:
        rows = json.loads(out) if ok else None
    except ValueError:
        rows = None
    if rows is None:
        return f"🖥 <b>{html.escape(name)}</b>\n❌ {html.escape(out)}"
    total = (last_data(name).get("ram") or {}).get("total") or 0
    title = "CPU" if by == "cpu" else "RAM"
    lines = [f"🖥 <b>{html.escape(name)}</b>", _('🔝 <b>Топ-{n} процессов по {title}</b>', n=n, title=title),
             _('<pre>  CPU%  RAM%    PID  Имя')]
    for r in rows:
        mem = 100.0 * r.get("rss", 0) / total if total else 0.0
        lines.append(f"{r.get('cpu', 0):6.1f} {mem:5.1f} {int(r.get('pid', 0)):6d}  "
                     f"{html.escape(str(r.get('name', '?'))[:28])}")
    lines.append("</pre>")
    return "\n".join(lines)


def services_text(name: str) -> str:
    srv = server(name)
    s = last_data(name)
    lines = [f"🖥 <b>{html.escape(name)}</b>", _('⚙️ <b>Службы и процессы</b>')]
    for svc, st in (s.get("services") or {}).items():
        lines.append(_('{icon} служба {escape}: {escape2}', icon="✅" if st == "running" else "❌",
                       escape=html.escape(svc), escape2=html.escape(st)))
    ctrs = s.get("containers") or {}
    for c in (watched_containers(srv, s) if srv else list(ctrs)):
        i = ctrs.get(c) or {}
        st = i.get("state") or "not-found"
        ok = st == "running" and i.get("health") != "unhealthy"
        tail = f" ({i['health']})" if i.get("health") else ""
        lines.append(_('{v} контейнер {escape}: {escape2}{escape3}', v='✅' if ok else '❌', escape=html.escape(c),
                       escape2=html.escape(st), escape3=html.escape(tail)))
    if len(lines) == 2:
        lines.append(_('Список пуст. Добавьте services / processes в config.yaml.'))
    return "\n".join(lines)


def net_text(name: str, state=None, data_dir: Path | None = None, now: float | None = None) -> str:
    from checks import fmt_bytes
    now = now or time.time()
    s = last_data(name)
    lines = [f"🖥 <b>{html.escape(name)}</b>", _('🌐 <b>Сеть и диск</b>')]
    hist = history(name, data_dir if data_dir is not None else DATA_DIR)
    if hist is not None:
        rows = [r for r in hist.rows(now - 600, now) if r[4] is not None and r[5] is not None]
        if len(rows) >= 2 and rows[-1][0] > rows[-2][0] and rows[-1][4] >= rows[-2][4]:
            dt = rows[-1][0] - rows[-2][0]
            lines.append(_('{escape}: ↓ {fmt_bytes}/с ↑ {fmt_bytes2}/с', escape=_('сеть'),
                           fmt_bytes=fmt_bytes((rows[-1][4] - rows[-2][4]) / dt),
                           fmt_bytes2=fmt_bytes(max(0, rows[-1][5] - rows[-2][5]) / dt)))
        for title, sec in ((_('за сутки'), 86400), (_('за неделю'), 7 * 86400)):
            p = hist.peaks(sec, now)
            if p:
                lines.append(_('Трафик {title}: ↓ {fmt_bytes} ↑ {fmt_bytes2}',
                               title=title, fmt_bytes=fmt_bytes(p['rx']), fmt_bytes2=fmt_bytes(p['tx'])))
    for path, d in (s.get("disks") or {}).items():
        lines.append(_('🗄 Диск {path}: {percent:.0f}% (свободно {fmt_bytes})',
                       path=html.escape(path), percent=d.get("percent", 0), fmt_bytes=fmt_bytes(d.get("free", 0))))
    if s.get("ports"):
        lines.append(_('🚪 Порты наружу: {ports}', ports=html.escape(", ".join(s["ports"]))))
    f2b = s.get("fail2ban")
    if f2b:
        lines.append(_('🚫 fail2ban: заблокировано сейчас {banned}, всего {total}',
                       banned=sum(j.get("banned", 0) for j in f2b.values()),
                       total=sum(j.get("total", 0) for j in f2b.values())))
        ips = sorted({ip for j in f2b.values() for ip in j.get("ips", [])})
        if ips:
            lines.append("  " + html.escape(", ".join(ips[:30])) + (_(' и др.') if len(ips) > 30 else ""))
    if state is not None:
        fails = sum(x[2] for x in state.remote_ssh_failed.get(name, []) if now - x[0] < 3600)
        lines.append(_('🛡 Неудачных входов по SSH за час: {n}', n=fails))
    return "\n".join(lines)


def banned_ips(name: str) -> list[str]:
    f2b = last_data(name).get("fail2ban") or {}
    return sorted({ip for j in f2b.values() for ip in j.get("ips", [])})


def check_updates(name: str, force: bool = False) -> list[dict] | None:
    """Список обновлений другого сервера, не чаще раза в UPDATES_EVERY (force — сейчас). None — не узнать."""
    srv = server(name)
    cached = UPDATES.get(name)
    if srv is None:
        return None
    if cached and not force and not cached.get("stale") and time.time() - cached["ts"] < UPDATES_EVERY:
        return cached["pkgs"]
    ok, out = run_agent(srv, ["updates"], timeout=90)
    try:
        pkgs = json.loads(out) if ok else None
    except ValueError:
        pkgs = None
    if pkgs is not None:
        # пока там идёт установка, список ещё старый: перепроверим, когда она закончится
        now = time.time()
        UPDATES[name] = {"ts": now, "pkgs": pkgs, "stale": now if last_data(name).get("apt_running") else 0}
    return pkgs


def updates_started(name: str) -> None:
    """Установка запущена: список в карточке устарел. stale — с какого момента; обновим список, когда свежий
    отчёт агента (полученный после этого момента) скажет, что установка закончилась."""
    if name in UPDATES:
        UPDATES[name]["stale"] = time.time()


def updates_settled(name: str) -> bool:
    """Установка, после которой список устарел, уже закончилась (по отчёту агента, пришедшему после неё)."""
    stale = (UPDATES.get(name) or {}).get("stale") or 0
    with _lock:
        last = LAST.get(name) or {}
    return bool(stale) and last.get("ts", 0) > stale and not (last.get("data") or {}).get("apt_running")


def installable(pkgs: list[dict], mode: str) -> list[dict]:
    """Что поставит установка: для «всех» — без отложенных Ubuntu (phased) и удержанных apt (kept)."""
    return [p for p in pkgs if p.get("security")] if mode == "security" else [p for p in pkgs if not p.get("held")]


def updates_line(name: str) -> str | None:
    pkgs = (UPDATES.get(name) or {}).get("pkgs")
    if pkgs is None:
        return None
    if not pkgs:
        return _('📦 Обновления: нет')
    sec = sum(bool(p.get("security")) for p in pkgs)
    held_n = sum(bool(p.get("held")) for p in pkgs)
    extra = [x for x in (_('🛡 безопасности: {n}', n=sec) if sec else "",
                         _('⏸ отложено: {n}', n=held_n) if held_n else "") if x]
    return _('📦 Обновления: {len}', len=len(pkgs)) + (f" ({', '.join(extra)})" if extra else "")


def update_events(cfg: dict, state, now: float, muted: bool, quiet: bool) -> list:
    """Раз в 6 часов: есть ли обновления безопасности на другом сервере (как у своего, не чаще раза в сутки)."""
    from checks import Alert
    import pkgupdates
    out = []
    if not cfg.get("update_alerts", True):
        return out
    for srv in servers(cfg):
        name = srv["name"]
        cached = UPDATES.get(name)
        if name not in LAST or name in ERRORS:
            continue
        if cached and cached.get("stale"):
            if updates_settled(name):
                check_updates(name, force=True)  # установка закончилась — обновляем карточку, без напоминаний
            continue
        if cached and now - cached["ts"] < UPDATES_EVERY:
            continue
        pkgs = check_updates(name, force=True) or []
        sec = [p["name"] for p in pkgs if p.get("security")]
        if sec and not muted and not quiet and now - state.remote_upd_alert_at.get(name, 0) >= 86400:
            state.remote_upd_alert_at[name] = now
            out.append(Alert(_('🩹 Доступны обновления безопасности Ubuntu: {len} ({names})',
                               len=len(sec), names=pkgupdates.names_short(sec)), f"{name}{SEP}apt:updates", "event",
                             server=name))
    return out
