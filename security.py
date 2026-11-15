"""Безопасность: входы по SSH, открытые порты, обновления безопасности, сигнал «я жив» во внешний сервис."""
from __future__ import annotations

import re
import shutil
import subprocess
import time
import urllib.request

ACCEPTED_RE = re.compile(r"Accepted (\S+) for (\S+) from (\S+) port")
FAILED_RE = re.compile(r"(?:Failed \S+ for (?:invalid user )?(\S+)|Invalid user (\S+)) from (\S+)")


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
    for line in text.splitlines():
        m = ACCEPTED_RE.search(line)
        if m:
            accepted.append((m.group(2), m.group(3), m.group(1)))
            continue
        m = FAILED_RE.search(line)
        if m:
            ip = m.group(3)
            failed[ip] = failed.get(ip, 0) + 1
    return accepted, failed


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
    if muted:
        # всё равно двигаем отметки, чтобы после тишины не пришла пачка старого
        state.ssh_checked = int(now)
        return out

    # SSH
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
        if threshold and total >= threshold and not quiet and now - state.ssh_fail_alert_at >= 3600:
            by_ip: dict[str, int] = {}
            for _, ip, n in state.ssh_failed:
                by_ip[ip] = by_ip.get(ip, 0) + n
            top = ", ".join(f"{ip} ({n})" for ip, n in sorted(by_ip.items(), key=lambda x: -x[1])[:3])
            worst = max(by_ip.items(), key=lambda x: x[1])[0]
            out.append(Alert(f"🛡 Подбор пароля по SSH: {total} неудачных попыток за 10 мин. Чаще всего: {top}",
                             f"brute:{worst}", "event"))
            state.ssh_fail_alert_at = now
            state.log.append([now, "ssh_bruteforce"])

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
