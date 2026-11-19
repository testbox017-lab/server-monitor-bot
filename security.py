"""Безопасность: входы по SSH, открытые порты, обновления безопасности, сигнал «я жив» во внешний сервис."""
from __future__ import annotations

import ipaddress
import re
import shutil
import subprocess
import time
import urllib.request
import pkgupdates
from i18n import _

# «Accepted publickey for root from 1.2.3.4 port 5 ssh2: ED25519 SHA256:abc» — отпечаток ключа в конце
ACCEPTED_RE = re.compile(r"^Accepted (\S+) for (\S+) from (\S+) port \d+(?: ssh2)?(?:: \S+ (SHA256:\S+))?")
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


def parse_ssh(text: str) -> tuple[list[tuple[str, str, str, str]], dict[str, int]]:
    """([(user, ip, method, отпечаток ключа или "")], {ip: число неудачных попыток})"""
    accepted, failed = [], {}
    failed_conns: set[tuple[str, str]] = set()   # (ip, порт) соединений, где уже была строка Failed
    invalid: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        m = ACCEPTED_RE.match(line)
        if m:
            accepted.append((m.group(2), m.group(3), m.group(1), m.group(4) or ""))
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


FAIL_KEEP = 3600          # сколько помним отдельные попытки (окна 10 мин и 1 ч)
REPEAT_DAYS = 30          # повторный бан в течение стольких дней — сразу навсегда
NET_MIN_FAILS = 5         # IP считается подборщиком для правила подсети, если за сутки у него столько попыток


def _hour_threshold(cfg: dict) -> int:
    v = cfg.get("autoban_hour_threshold")
    return 20 if v is None else int(v)


def _in_banned_net(ip: str, state) -> bool:
    for t in state.autobans:
        if "/" in t:
            try:
                if ipaddress.ip_address(ip) in ipaddress.ip_network(t, strict=False):
                    return True
            except ValueError:
                continue
    return False


def autoban_candidates(cfg: dict, state, now: float) -> list[tuple[str, int]]:
    """[(ip, попыток за час)], которые пора заблокировать: autoban_threshold попыток за 10 минут
    (быстрый подбор) или autoban_hour_threshold за час (медленный, «под порогом»). Никогда — IP,
    с которых уже входили по SSH, и IP из autoban_whitelist."""
    if not cfg.get("autoban", True) or not cfg.get("ssh_alerts", True):
        return []
    thr = int(cfg.get("autoban_threshold") or 10)
    hthr = _hour_threshold(cfg)
    by10: dict[str, int] = {}
    by60: dict[str, int] = {}
    for ts, ip, n in state.ssh_failed:
        if now - ts < 3600:
            by60[ip] = by60.get(ip, 0) + n
            if now - ts < 600:
                by10[ip] = by10.get(ip, 0) + n
    skip = recent_known_ips(state, now) | set(cfg.get("autoban_whitelist") or []) | set(state.autobans)
    skip |= {ip for ip, ts in state.autoban_skip.items() if now - ts < AUTOBAN_RETRY}
    out = [(ip, n) for ip, n in sorted(by60.items(), key=lambda x: -x[1])
           if (by10.get(ip, 0) >= thr or (hthr and n >= hthr))
           and ip not in skip and public_ip(ip) and not _in_banned_net(ip, state)]
    return out[:AUTOBAN_PER_CHECK]


def net24(ip: str) -> str | None:
    """Подсеть /24 для IPv4, для IPv6 — None (подсетями IPv6 не баним)."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if a.version != 4:
        return None
    return str(ipaddress.ip_network(f"{ip}/24", strict=False))


def net_candidates(cfg: dict, state, now: float) -> list[tuple[str, list[str]]]:
    """[(подсеть /24, [IP-подборщики в ней])]: из одной /24 подбирают пароль сразу несколько адресов
    (autoban_net_ips, по умолчанию 2) — блокируем всю подсеть. Подборщик — IP, забаненный сейчас или за
    последние 30 дней, или сделавший за сутки 5+ неудачных попыток. Подсеть не трогаем, если в ней есть
    IP, с которого входили, или IP из autoban_whitelist."""
    need = int(cfg.get("autoban_net_ips") or 0) if cfg.get("autoban_net_ips") is not None else 2
    if not cfg.get("autoban", True) or not cfg.get("ssh_alerts", True) or need < 2:
        return []
    bad: dict[str, set[str]] = {}

    def add(ip: str) -> None:
        net = net24(ip)
        if net and public_ip(ip):
            bad.setdefault(net, set()).add(ip)
    for ip, (n, last) in state.fail_ips.items():
        if now - last < 86400 and n >= NET_MIN_FAILS:
            add(ip)
    for t in state.autobans:
        if "/" not in t:
            add(t)
    for t, (__, last) in state.ban_history.items():
        if "/" not in t and now - last < REPEAT_DAYS * 86400:
            add(t)
    protect = recent_known_ips(state, now) | set(cfg.get("autoban_whitelist") or [])
    protected_nets = {net24(ip) for ip in protect}
    skip = set(state.autobans) | {t for t, ts in state.autoban_skip.items() if now - ts < AUTOBAN_RETRY}
    out = [(net, sorted(ips)) for net, ips in sorted(bad.items())
           if len(ips) >= need and net not in skip and net not in protected_nets]
    return out[:AUTOBAN_PER_CHECK]


def ban_hours(cfg: dict, state, target: str, now: float) -> int:
    """На сколько часов банить (0 = навсегда). Кого уже банили за последние 30 дней — сразу навсегда."""
    hours = cfg.get("autoban_hours")
    hours = 24 if hours is None else int(hours)
    prev = state.ban_history.get(target)
    if prev and now - prev[1] < REPEAT_DAYS * 86400 and cfg.get("autoban_repeat_forever", True):
        return 0
    return hours


def remember_ban(state, target: str, now: float) -> None:
    n = (state.ban_history.get(target) or [0, 0])[0]
    state.ban_history[target] = [n + 1, now]
    state.ban_history = {t: v for t, v in state.ban_history.items() if now - v[1] < REPEAT_DAYS * 86400}


def is_deploy(cfg: dict, ip: str, fingerprint: str) -> bool:
    """Вход по SSH от автодеплоя: ключ из deploy_keys (отпечаток SHA256:…) или адрес из deploy_nets."""
    if fingerprint and fingerprint in (cfg.get("deploy_keys") or []):
        return True
    try:
        a = ipaddress.ip_address(ip)
        return any(a in ipaddress.ip_network(n, strict=False) for n in cfg.get("deploy_nets") or [])
    except ValueError:
        return False


def recent_known_ips(state, now: float) -> set[str]:
    """IP, с которых входили по SSH за последние KNOWN_IP_DAYS дней. Для старых записей без времени
    (до появления known_ip_at) считаем временем входа момент загрузки состояния."""
    at = getattr(state, "known_ip_at", {}) or {}
    return {ip for ip in state.known_ips if now - at.get(ip, now) < KNOWN_IP_DAYS * 86400}


def expired_autobans(state, now: float) -> list[str]:
    return [ip for ip, until in state.autobans.items() if until and until <= now]


def ban_duration_text(hours: int, bare: bool = False) -> str:
    """«на 24 ч»; bare=True — «24 ч» (для кнопок)."""
    if not hours:
        return _('навсегда')
    if bare:
        return _('{days} дн.', days=hours // 24) if hours >= 48 else _('{hours} ч', hours=hours)
    return _('на {days} дн.', days=hours // 24) if hours >= 48 else _('на {hours} ч', hours=hours)


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
        addr, __, port = local.rpartition(":")
        addr = addr.strip("[]").split("%")[0]
        if addr.startswith("127.") or addr == "::1":
            continue
        ports.add(f"{proto}/{port}")
    return ports


# ---------- Обновления ----------

def security_updates() -> list[str] | None:
    pkgs = pkgupdates.upgradable()
    return None if pkgs is None else [p.name for p in pkgs if p.security]


# ---------- Сигнал «я жив» ----------

def heartbeat(url: str) -> bool:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "monitor-bot"}),
                                    timeout=10) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


# ---------- Статистика попыток ----------

DAILY_KEEP = 8      # дней статистики для недельного отчёта
DAILY_MAX_IPS = 300  # больше адресов за день не храним (оставляем самых активных)


def record_failures(state, failed: dict[str, int], now: float) -> None:
    """Счётчики для правила подсети (сутки) и для недельного отчёта (по дням UTC)."""
    for ip, n in failed.items():
        cur = state.fail_ips.get(ip)
        total = cur[0] if cur and now - cur[1] < 86400 else 0
        state.fail_ips[ip] = [total + n, now]
    state.fail_ips = {ip: v for ip, v in state.fail_ips.items() if now - v[1] < 86400}
    if failed:
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        d = state.ssh_daily.setdefault(day, {})
        for ip, n in failed.items():
            d[ip] = d.get(ip, 0) + n
        if len(d) > DAILY_MAX_IPS:
            state.ssh_daily[day] = dict(sorted(d.items(), key=lambda x: -x[1])[:DAILY_MAX_IPS])
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(now - DAILY_KEEP * 86400))
    state.ssh_daily = {k: v for k, v in state.ssh_daily.items() if k > cutoff}


def ssh_week_lines(state, now: float) -> list[str]:
    """Строки для недельного отчёта: сколько было попыток подбора, откуда, сколько заблокировано."""
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(now - 7 * 86400))
    by_ip: dict[str, int] = {}
    for day, d in state.ssh_daily.items():
        if day > cutoff:
            for ip, n in d.items():
                by_ip[ip] = by_ip.get(ip, 0) + n
    if not by_ip:
        return []
    total = sum(by_ip.values())
    nets: dict[str, int] = {}
    for ip, n in by_ip.items():
        nets[net24(ip) or ip] = nets.get(net24(ip) or ip, 0) + n
    top = ", ".join(f"{t} ({n})" for t, n in sorted(nets.items(), key=lambda x: -x[1])[:3])
    blocked = [t for t in state.autobans]
    lines = [_('🛡 Подбор пароля SSH: {total} попыток с {len} IP. Больше всего: {top}',
               total=total, len=len(by_ip), top=top)]
    if blocked:
        lines.append(_('  Сейчас в автобане: {len} (подсетей: {sum})',
                       len=len(blocked), sum=sum('/' in t for t in blocked)))
    return lines


# ---------- Итерация ----------

def events(cfg: dict, state, now: float, muted: bool, quiet: bool,
           ssh_text_fn=ssh_log, ports_fn=listening_ports, updates_fn=pkgupdates.upgradable) -> list:
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
                for user, ip, method, fp in accepted:
                    if is_deploy(cfg, ip, fp):
                        # автодеплой: адрес каждый раз новый (GitHub Actions), в «свои» IP его не записываем
                        # и не шумим; перезапуск контейнера следом будет считаться деплоем
                        state.deploy_at = now
                        state.log.append([now, "deploy"])
                        continue
                    new = ip not in state.known_ips
                    if new:
                        state.known_ips.append(ip)
                    state.known_ip_at[ip] = now
                    if muted:
                        continue
                    if new or cfg.get("ssh_alert_all_logins"):
                        tag = _('с нового IP') if new else ""
                        out.append(Alert(_('🔐 Вход по SSH: {user} {tag} {ip} ({method})',
                                           user=user, tag=tag, ip=ip, method=method).replace("  ", " "),
                                         None, "event"))
                        state.log.append([now, f"ssh:{ip}"])
                for ip, n in failed.items():
                    state.ssh_failed.append([now, ip, n])
                record_failures(state, failed, now)
        state.ssh_failed = [x for x in state.ssh_failed if now - x[0] < FAIL_KEEP]
        recent = [x for x in state.ssh_failed if now - x[0] < 600]
        total = sum(x[2] for x in recent)
        threshold = cfg.get("ssh_fail_threshold", 30)
        if threshold and total >= threshold and not quiet and not muted and now - state.ssh_fail_alert_at >= 3600:
            by_ip: dict[str, int] = {}
            for __, ip, n in recent:
                by_ip[ip] = by_ip.get(ip, 0) + n
            top = ", ".join(f"{ip} ({n})" for ip, n in sorted(by_ip.items(), key=lambda x: -x[1])[:3])
            worst = max(by_ip.items(), key=lambda x: x[1])[0]
            out.append(Alert(_('🛡 Подбор пароля по SSH: {total} неудачных попыток за 10 мин. Чаще всего: {top}',
                               total=total, top=top),
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
                    out.append(Alert(_('🚪 Новый открытый наружу порт: {p}', p=p), None, "event"))
                    state.log.append([now, f"port:{p}"])
                if new:
                    state.known_ports = sorted(set(state.known_ports) | set(new))

    # Обновления пакетов — раз в 6 часов, напоминание не чаще раза в сутки. По умолчанию пишем, только если
    # есть обновления безопасности; update_alerts_all — и об обычных. Под сообщением кнопка со списком.
    if cfg.get("update_alerts", True) and now - state.updates_checked >= 6 * 3600:
        state.updates_checked = now
        pkgs = updates_fn() or []
        sec = [p.name for p in pkgs if p.security]
        if (sec or (pkgs and cfg.get("update_alerts_all"))) and not quiet and now - state.updates_alert_at >= 86400:
            state.updates_alert_at = now
            if sec:
                text = _('🩹 Доступны обновления безопасности Ubuntu: {len} ({names})',
                         len=len(sec), names=pkgupdates.names_short(sec))
                if len(pkgs) > len(sec):
                    text += _('. Всего обновлений: {len}', len=len(pkgs))
            else:
                text = _('📦 Доступны обновления Ubuntu: {len} ({names})',
                         len=len(pkgs), names=pkgupdates.names_short([p.name for p in pkgs]))
            out.append(Alert(text, "apt:updates", "event"))
    return out
