"""Проверки снаружи: сайты и API, сроки SSL-сертификатов, DNS, ping и TCP-порты.

Цель записывается одной строкой (список probes в настройках):
  https://example.com            сайт: ответ 2xx/3xx
  https://example.com/health | OK   сайт, и на странице есть текст «OK»
  tcp://db.example.com:5432      порт открыт
  ping://1.1.1.1                 хост отвечает на ping
  dns://example.com              имя резолвится
Без схемы: «host:port» = tcp, иначе https.
"""
from __future__ import annotations

import socket
import ssl
import subprocess
import shutil
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urlsplit

SCHEMES = ("http", "https", "tcp", "ping", "dns")
SSL_STEPS = (14, 7, 3, 1, 0)       # за сколько дней до окончания предупреждать
SSL_RECHECK = 6 * 3600             # срок сертификата перепроверяем раз в 6 часов
MAX_BODY = 1024 * 1024


@dataclass
class Target:
    raw: str
    scheme: str
    host: str
    port: int | None
    url: str | None = None
    expect: str | None = None

    @property
    def label(self) -> str:
        return self.raw


def parse(raw: str) -> Target | None:
    """Разбор строки цели. None, если строка непонятная."""
    raw = (raw or "").strip()
    if not raw or len(raw) > 200 or any(c in raw for c in "\n\r\t"):
        return None
    expect = None
    if " | " in raw:
        raw_url, expect = (x.strip() for x in raw.split(" | ", 1))
        expect = expect or None
    else:
        raw_url = raw
    if "://" not in raw_url:
        host, _, port = raw_url.rpartition(":")
        raw_url = f"tcp://{raw_url}" if host and port.isdigit() else f"https://{raw_url}"
    try:
        u = urlsplit(raw_url)
        port = u.port
    except ValueError:
        return None
    scheme = u.scheme.lower()
    host = u.hostname or ""
    if scheme not in SCHEMES or not host or " " in host:
        return None
    if scheme == "tcp" and not port:
        return None
    if scheme in ("http", "https"):
        return Target(raw, scheme, host, port or (443 if scheme == "https" else 80), raw_url, expect)
    if expect:
        return None  # текст на странице имеет смысл только для http(s)
    return Target(raw, scheme, host, port)


def normalize(raw: str) -> str | None:
    """Строка в том виде, в каком её сохраняем: со схемой."""
    t = parse(raw)
    if not t:
        return None
    if t.scheme in ("http", "https"):
        return t.url + (f" | {t.expect}" if t.expect else "")
    if t.scheme == "tcp":
        return f"tcp://{t.host}:{t.port}"
    return f"{t.scheme}://{t.host}"


# ---------- Одна проверка ----------

def check_http(t: Target, timeout: float) -> tuple[bool, str]:
    req = urllib.request.Request(t.url, headers={"User-Agent": "monitor-bot"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code = r.status
            if t.expect:
                body = r.read(MAX_BODY).decode("utf-8", "replace")
                if t.expect not in body:
                    return False, f"HTTP {code}, но на странице нет «{t.expect}»"
            return True, f"HTTP {code}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, short_error(e.reason)
    except Exception as e:  # noqa: BLE001
        return False, short_error(e)


def check_tcp(t: Target, timeout: float) -> tuple[bool, str]:
    try:
        with socket.create_connection((t.host, t.port), timeout=timeout):
            return True, "порт открыт"
    except Exception as e:  # noqa: BLE001
        return False, short_error(e)


def check_ping(t: Target, timeout: float) -> tuple[bool, str]:
    if not shutil.which("ping"):
        return False, "нет команды ping"
    w = str(max(1, int(timeout)))
    try:
        r = subprocess.run(["ping", "-c", "1", "-W", w, t.host], capture_output=True, text=True,
                           timeout=timeout + 5)
    except Exception as e:  # noqa: BLE001
        return False, short_error(e)
    return (True, "отвечает") if r.returncode == 0 else (False, "не отвечает на ping")


def check_dns(t: Target, timeout: float) -> tuple[bool, str]:
    try:
        infos = socket.getaddrinfo(t.host, None)
    except Exception as e:  # noqa: BLE001
        return False, short_error(e)
    addrs = sorted({i[4][0] for i in infos})
    return True, ", ".join(addrs[:3])


CHECKS = {"http": check_http, "https": check_http, "tcp": check_tcp, "ping": check_ping, "dns": check_dns}


def short_error(e) -> str:
    if isinstance(e, socket.timeout) or "timed out" in str(e):
        return "нет ответа (таймаут)"
    if isinstance(e, socket.gaierror):
        return "имя не найдено в DNS"
    if isinstance(e, ConnectionRefusedError):
        return "соединение отклонено"
    if isinstance(e, ssl.SSLCertVerificationError):
        return f"ошибка SSL-сертификата: {getattr(e, 'verify_message', '') or e}"[:150]
    return str(e)[:150] or e.__class__.__name__


def run_one(t: Target, timeout: float) -> tuple[bool, str, int]:
    start = time.monotonic()
    ok, detail = CHECKS[t.scheme](t, timeout)
    return ok, detail, int((time.monotonic() - start) * 1000)


def run_all(raws: list[str], timeout: float = 10, check_fn=None) -> dict[str, tuple[bool, str, int]]:
    """{строка цели: (ok, подробности, мс)} — все цели параллельно."""
    targets = [(r, parse(r)) for r in raws]
    out: dict[str, tuple[bool, str, int]] = {r: (False, "непонятная цель", 0) for r, t in targets if t is None}
    good = [(r, t) for r, t in targets if t is not None]
    if not good:
        return out
    fn = check_fn or run_one
    with ThreadPoolExecutor(max_workers=min(8, len(good))) as ex:
        for r, res in zip([r for r, _ in good], ex.map(lambda rt: fn(rt[1], timeout), good)):
            out[r] = res
    return out


# ---------- SSL ----------

def ssl_days_left(host: str, port: int = 443, timeout: float = 10) -> float | None:
    """Сколько дней до окончания сертификата. None, если узнать не удалось."""
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as s:
                cert = s.getpeercert()
    except Exception:  # noqa: BLE001
        return None
    not_after = cert.get("notAfter") if cert else None
    if not not_after:
        return None
    return (ssl.cert_time_to_seconds(not_after) - time.time()) / 86400


def https_hosts(raws: list[str]) -> list[tuple[str, int]]:
    seen = []
    for r in raws:
        t = parse(r)
        if t and t.scheme == "https" and (t.host, t.port) not in seen:
            seen.append((t.host, t.port))
    return seen


def host_key(host: str, port: int) -> str:
    return host if port == 443 else f"{host}:{port}"


# ---------- Итерация ----------

def evaluate(cfg: dict, state, now: float, run_fn=run_all) -> dict[str, str | None]:
    """Ключи web:<цель> для общей системы алертов. Ошибка засчитывается после N неудач подряд."""
    raws = list(cfg.get("probes") or [])
    res: dict[str, str | None] = {}
    if not raws:
        state.probe_fails = {}
        state.probe_last = {}
        return res
    need = max(1, int(cfg.get("probe_fail_count") or 2))
    results = run_fn(raws, float(cfg.get("probe_timeout") or 10))
    for raw in raws:
        ok, detail, ms = results.get(raw, (False, "нет результата", 0))
        fails = 0 if ok else state.probe_fails.get(raw, 0) + 1
        state.probe_fails[raw] = fails
        state.probe_last[raw] = [ok, detail, ms, now]
        if ok or fails < need:
            res[f"web:{raw}"] = None
        else:
            res[f"web:{raw}"] = f"🌐 Не отвечает {raw}: {detail}"
    for raw in list(state.probe_fails):
        if raw not in raws:
            state.probe_fails.pop(raw, None)
            state.probe_last.pop(raw, None)
    return res


def ssl_events(cfg: dict, state, now: float, muted: bool, quiet: bool, days_fn=ssl_days_left) -> list:
    """Предупреждения о скором окончании SSL-сертификатов: за 14, 7, 3, 1 день и в день окончания."""
    from checks import Alert

    out: list = []
    hosts = https_hosts(list(cfg.get("probes") or []))
    keys = {host_key(h, p) for h, p in hosts}
    for k in list(state.ssl_days):
        if k not in keys:
            state.ssl_days.pop(k, None)
            state.ssl_checked.pop(k, None)
            state.ssl_alerted.pop(k, None)
    if not cfg.get("ssl_alerts", True):
        return out
    warn = int(cfg.get("ssl_warn_days") or 14)
    steps = [s for s in SSL_STEPS if s <= warn] or [0]
    if warn not in steps:
        steps = [warn] + steps
    for host, port in hosts:
        k = host_key(host, port)
        if now - state.ssl_checked.get(k, 0) >= SSL_RECHECK:
            days = days_fn(host, port)
            state.ssl_checked[k] = now
            if days is not None:
                state.ssl_days[k] = days
        days = state.ssl_days.get(k)
        if days is None:
            continue
        if days > warn:
            state.ssl_alerted.pop(k, None)  # сертификат продлили
            continue
        step_now = min(s for s in steps if days <= s) if any(days <= s for s in steps) else None
        if step_now is None or muted or quiet:
            continue
        last = state.ssl_alerted.get(k)
        if last is not None and last <= step_now:
            continue
        state.ssl_alerted[k] = step_now
        state.log.append([now, f"ssl:{k}"])
        if days <= 0:
            text = f"🔒 SSL-сертификат {k} истёк! Браузеры уже показывают ошибку."
        else:
            text = f"🔒 SSL-сертификат {k} истекает через {days:.0f} дн. Пора продлить."
        out.append(Alert(text, None, "event"))
    return out


def probes_text(cfg: dict, state) -> str:
    raws = list(cfg.get("probes") or [])
    if not raws:
        return ""
    lines = ["🌐 <b>Проверки снаружи</b>"]
    import html
    for raw in raws:
        last = state.probe_last.get(raw)
        if not last:
            lines.append(f"⏳ {html.escape(raw)} — ждёт проверки")
            continue
        ok, detail, ms = last[0], last[1], last[2]
        icon = "✅" if ok else "❌"
        extra = f", {ms} мс" if ok and ms else ""
        t = parse(raw)
        if t and t.scheme == "https":
            d = state.ssl_days.get(host_key(t.host, t.port))
            if d is not None:
                extra += f", SSL ещё {d:.0f} дн."
        lines.append(f"{icon} {html.escape(raw)} — {html.escape(detail)}{extra}")
    return "\n".join(lines)
