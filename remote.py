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


def ssh_cmd(srv: dict, cfg: dict, data_dir: Path | None, since: int | None = None) -> list[str]:
    d = key_dir(cfg, data_dir)
    key = str(srv.get("key") or d / "remote_ed25519")
    services = [x for x in (srv.get("services") or []) if SVC_RE.match(str(x))]
    return ["ssh", "-i", key, "-p", str(int(srv.get("port") or 22)),
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={d / 'known_hosts'}",
            "-o", "LogLevel=ERROR", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2",
            f"{srv.get('user') or 'root'}@{srv['host']}", ("status " + ",".join(services) + (f" since={int(since)}" if since else "")).strip()]


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
            r = results_from(srv, cfg, data)
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
                      state.remote_ssh_failed, state.remote_fail_alert_at, state.remote_ports):
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
                               total=total, top=top), None, "event", server=name))
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
    return out


def servers_text(cfg: dict, state=None) -> str:
    srvs = servers(cfg)
    if not srvs:
        return _('Других серверов нет. Добавьте remote_servers в config.yaml (см. README).')
    return "\n\n".join(server_text(s, cfg, None, state) for s in srvs)
