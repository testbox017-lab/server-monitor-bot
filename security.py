"""Безопасность: входы по SSH, открытые порты, обновления безопасности, сигнал «я жив» во внешний сервис."""
from __future__ import annotations

import ipaddress
import re
import shutil
import subprocess
import time
import urllib.request

ACCEPTED_RE = re.compile(r"^Accepted (\S+) for (\S+) from (\S+) port \d+")
# Имя пользователя пишет атакующий, в нём могут быть пробелы и «from 1.2.3.4 port 1». Поэтому IP и порт
# берём с конца строки, а имя — всё, что между.
FAILED_RE = re.compile(r"^Failed \S+ for (?:invalid user )?(.*) from (\S+) port (\d+)(?: ssh2)?$")
INVALID_RE = re.compile(r"^Invalid user (.*) from (\S+) port (\d+)$")
KNOWN_IP_DAYS = 30   # IP с успешным входом не попадает под автобан столько дней после последнего входа


def run(cmd: list[str], timeout: int = 30) -> str | None:
    if not shutil.which(cmd[0]):
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8",
                           errors="replace")
    except Exception:
        return None
    return r.stdout if r.returncode == 0 else None


# ---------- SSH ----------

def ssh_log(since: int) -> str | None:
    """Строки sshd из журнала с момента since. Пользователю бота нужна группа systemd-journal."""
    return run(["journalctl", "-u", "ssh.service", "-u", "sshd.service", "--since", f"@{since}",
                "-o", "cat", "--no-pager"])


def parse_ssh(text: str) -> tuple[list[tuple[str, str, str]], dict[str, int]]:
    """([(user, ip, method)], {ip: число неудачных попыток})"""
    accepted, failed = [], {}
    failed_conns: set[tuple[str, str]] = set()   # (ip, порт) соединений, где уже была строка Failed
    invalid: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        m = ACCEPTED_RE.match(line)
        if m:
            accepted.append((m.group(2), m.group(3), m.group(1)))
            continue
        m = FAILED_RE.match(line)
        if m:
            ip = m.group(2)
            failed[ip] = failed.get(ip, 0) + 1
            failed_conns.add((ip, m.group(3)))
            continue
        m = INVALID_RE.match(line)
        if m:
            invalid.append((m.group(2), m.group(3)))
    # «Invalid user» и «Failed password for invalid user» — одна и та же попытка. Отдельно считаем
    # только «Invalid user» без пароля (например, перебор только ключами).
    for ip, port in invalid:
        if (ip, port) not in failed_conns:
            failed[ip] = failed.get(ip, 0) + 1
            failed_conns.add((ip, port))
    return accepted, failed


# ---------- Автобан ----------

AUTOBAN_PER_CHECK = 10   # больше за одну проверку не блокируем
AUTOBAN_RETRY = 3600     # если помощник отказал, повторяем не раньше чем через час


def public_ip(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_unspecified
                or a.is_reserved)


def autoban_candidates(cfg: dict, state, now: float) -> list[tuple[str, int]]:
    """[(ip, попыток за 10 мин)], которые пора заблокировать. Никогда — IP, с которых уже входили по SSH,
    и IP из autoban_whitelist."""
    if not cfg.get("autoban", True) or not cfg.get("ssh_alerts", True):
        return []
    thr = int(cfg.get("autoban_threshold") or 10)
    by_ip: dict[str, int] = {}
    for ts, ip, n in state.ssh_failed:
        if now - ts < 600:
            by_ip[ip] = by_ip.get(ip, 0) + n
    skip = recent_known_ips(state, now) | set(cfg.get("autoban_whitelist") or []) | set(state.autobans)
    skip |= {ip for ip, ts in state.autoban_skip.items() if now - ts < AUTOBAN_RETRY}
    out = [(ip, n) for ip, n in sorted(by_ip.items(), key=lambda x: -x[1])
           if n >= thr and ip not in skip and public_ip(ip)]
    return out[:AUTOBAN_PER_CHECK]


def recent_known_ips(state, now: float) -> set[str]:
    """IP, с которых входили по SSH за последние KNOWN_IP_DAYS дней. Для старых записей без времени
    (до появления known_ip_at) считаем временем входа момент загрузки состояния."""
    at = getattr(state, "known_ip_at", {}) or {}
    return {ip for ip in state.known_ips if now - at.get(ip, now) < KNOWN_IP_DAYS * 86400}


def expired_autobans(state, now: float) -> list[str]:
    return [ip for ip, until in state.autobans.items() if until and until <= now]


def ban_duration_text(hours: int) -> str:
    if not hours:
        return "навсегда"
    return f"на {hours // 24} дн." if hours >= 48 else f"на {hours} ч"


# ---------- Порты ----------

def listening_ports() -> set[str] | None:
    """Порты TCP/UDP, открытые не только на localhost: {"tcp/22", "udp/53"}."""
    out = run(["ss", "-H", "-tuln"])
    if out is None:
        return None
    ports = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        proto, local = parts[0], parts[4]
        addr, _, port = local.rpartition(":")
        addr = addr.strip("[]").split("%")[0]
        if addr.startswith("127.") or addr == "::1":
            continue
        ports.add(f"{proto}/{port}")
    return ports


# ---------- Обновления ----------

def security_updates() -> list[str] | None:
    out = run(["apt", "list", "--upgradable"], timeout=60)
    if out is None:
        return None
    return sorted({ln.split("/")[0] for ln in out.splitlines() if "-security" in ln})


# ---------- Сигнал «я жив» ----------

def heartbeat(url: str) -> bool:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "monitor-bot"}),
                                    timeout=10) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


# ---------- Итерация ----------

def events(cfg: dict, state, now: float, muted: bool, quiet: bool,
           ssh_text_fn=ssh_log, ports_fn=listening_ports, updates_fn=security_updates) -> list:
    from checks import Alert  # избегаем циклического импорта

    out: list = []
    # SSH читаем всегда, даже когда уведомления выключены: иначе в это время не работал бы автобан
    # и не запоминались бы свои IP. При выключенных уведомлениях только не шлём сообщения.
    if cfg.get("ssh_alerts", True):
        since = state.ssh_checked
        state.ssh_checked = int(now)
        if since:
            text = ssh_text_fn(since)
            if text:
                accepted, failed = parse_ssh(text)
                for user, ip, method in accepted:
                    new = ip not in state.known_ips
                    if new:
                        state.known_ips.append(ip)
                    state.known_ip_at[ip] = now
                    if muted:
                        continue
                    if new or cfg.get("ssh_alert_all_logins"):
                        tag = "с нового IP" if new else ""
                        out.append(Alert(f"🔐 Вход по SSH: {user} {tag} {ip} ({method})".replace("  ", " "),
                                         None, "event"))
                        state.log.append([now, f"ssh:{ip}"])
                for ip, n in failed.items():
                    state.ssh_failed.append([now, ip, n])
        state.ssh_failed = [x for x in state.ssh_failed if now - x[0] < 600]
        total = sum(x[2] for x in state.ssh_failed)
        threshold = cfg.get("ssh_fail_threshold", 30)
        if threshold and total >= threshold and not quiet and not muted and now - state.ssh_fail_alert_at >= 3600:
            by_ip: dict[str, int] = {}
            for _, ip, n in state.ssh_failed:
                by_ip[ip] = by_ip.get(ip, 0) + n
            top = ", ".join(f"{ip} ({n})" for ip, n in sorted(by_ip.items(), key=lambda x: -x[1])[:3])
            worst = max(by_ip.items(), key=lambda x: x[1])[0]
            out.append(Alert(f"🛡 Подбор пароля по SSH: {total} неудачных попыток за 10 мин. Чаще всего: {top}",
                             f"brute:{worst}", "event"))
            state.ssh_fail_alert_at = now
            state.log.append([now, "ssh_bruteforce"])

    if muted:
        return out  # порты и обновления проверим, когда уведомления включатся

    # Порты
    if cfg.get("port_alerts", True):
        ports = ports_fn()
        if ports is not None:
            if not state.known_ports:
                state.known_ports = sorted(ports)  # первый запуск: запоминаем как норму
            else:
                new = sorted(ports - set(state.known_ports))
                for p in new:
                    out.append(Alert(f"🚪 Новый открытый наружу порт: {p}", None, "event"))
                    state.log.append([now, f"port:{p}"])
                if new:
                    state.known_ports = sorted(set(state.known_ports) | set(new))

    # Обновления безопасности — раз в 6 часов, напоминание не чаще раза в сутки
    if cfg.get("update_alerts", True) and now - state.updates_checked >= 6 * 3600:
        state.updates_checked = now
        pkgs = updates_fn()
        if pkgs and not quiet and now - state.updates_alert_at >= 86400:
            state.updates_alert_at = now
            names = ", ".join(pkgs[:8]) + (" и др." if len(pkgs) > 8 else "")
            out.append(Alert(f"🩹 Доступны обновления безопасности Ubuntu: {len(pkgs)} ({names})", None, "event"))
    return out
