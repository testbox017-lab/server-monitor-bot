"""Кто имеет доступ к серверу и что открыто наружу мимо ufw.

1. Доступ: отпечатки ключей SSH в authorized_keys, пользователи с оболочкой, uid 0, члены групп
   sudo/admin/wheel/docker, хеши файлов sudoers. Свой сервер — командой помощника «access-audit»
   (раз в access_check_min минут), другой сервер — полем «access» в ответе агента. Первый снимок
   запоминается как норма, дальше о каждом изменении приходит сообщение (и в тихие часы: это безопасность).
2. Порты контейнеров: Docker публикует порты своими правилами iptables в обход ufw, поэтому ни ufw, ни бан
   по IP их не закрывают. О каждом порту, опубликованном не на 127.0.0.1, бот пишет один раз.
"""
from __future__ import annotations

import html
import ipaddress
import json
import re
import subprocess
import time

from i18n import _

PORT_RE = re.compile(r"^(?P<ip>\[[0-9A-Fa-f:.]*\]|[0-9A-Fa-f:.]*):(?P<hp>\d+(?:-\d+)?)->(?P<cp>\d+(?:-\d+)?)/"
                     r"(?P<proto>tcp|udp|sctp)$")
UNSUPPORTED_RETRY = 6 * 3600
_helper = {"unsupported_until": 0.0}


# ---------- Порты контейнеров ----------

def public_ports(ports: str) -> list[str]:
    """«8080/tcp» для портов, опубликованных не только на loopback, из колонки Ports у docker ps."""
    out: list[str] = []
    for part in (ports or "").split(","):
        m = PORT_RE.match(part.strip())
        if not m:
            continue
        ip = m["ip"].strip("[]")
        if ip not in ("", "0.0.0.0", "::"):
            try:
                if ipaddress.ip_address(ip).is_loopback:
                    continue
            except ValueError:
                continue
        p = f"{m['hp']}/{m['proto']}"
        if p not in out:
            out.append(p)
    return out


def parse_ports(text: str) -> dict[str, list[str]]:
    """{контейнер: [открытые наружу порты]} из docker ps --format '{{.Names}}\\t{{.Ports}}'."""
    out: dict[str, list[str]] = {}
    for line in (text or "").splitlines():
        name, _sep, ports = line.partition("\t")
        pub = public_ports(ports)
        if name and pub:
            out[name] = pub
    return out


def exposed_text(ctr: str, ports: list[str]) -> str:
    first = ports[0].split("/")[0].split("-")[0]
    return _('🐳 Контейнер {c} открыт наружу: {ports}. Docker обходит ufw: его правила и бан по IP этот порт '
             'не закрывают. Если снаружи доступ не нужен, опубликуйте порт только на 127.0.0.1 '
             '(в compose: «127.0.0.1:{p}:…»).', c=html.escape(ctr), ports=", ".join(ports), p=first)


def port_events(cfg: dict, known: list[str], exposed: dict[str, list[str]] | None, now: float, muted: bool,
                quiet: bool, server: str | None = None, log: list | None = None) -> tuple[list, list[str]]:
    """Сообщения о новых открытых наружу портах контейнеров и новый список «уже сообщали» («контейнер порт»).
    Порт, который закрыли, из списка уходит: откроют снова — напишем снова. В тихие часы и при выключенных
    уведомлениях ждём (список не меняем), чтобы сообщить потом."""
    from checks import Alert
    if exposed is None or not cfg.get("docker_port_alerts", True):
        return [], known
    current = {f"{c} {p}" for c, ps in exposed.items() for p in ps}
    keep = [k for k in known if k in current]
    if muted or quiet:
        return [], keep
    out = []
    for c, ps in sorted(exposed.items()):
        new = [p for p in ps if f"{c} {p}" not in known]
        if new:
            key = (f"{server}|" if server else "") + f"dport:{c}"
            out.append(Alert(exposed_text(c, new), key, "event", server=server))
            if log is not None:
                log.append([now, key])
            keep += [f"{c} {p}" for p in new]
    return out, sorted(set(keep))


def local_exposed() -> dict[str, list[str]] | None:
    """Открытые наружу порты контейнеров своего сервера. None — не знаем (помощник старый, docker нет)."""
    import checks
    cached = checks._poll_fresh()
    if cached is not None and cached.get("ports") is not None:
        return parse_ports(cached["ports"])
    if checks.docker_via_helper():
        return None   # docker только через помощник, а старый помощник порты не отдаёт
    r = checks.docker("ps", "--format", checks.PORTS_FMT)
    if r is None or r.returncode != 0:
        return None
    return parse_ports(r.stdout)


def remote_exposed(data: dict) -> dict[str, list[str]] | None:
    ctrs = data.get("containers")
    if not isinstance(ctrs, dict):
        return None
    if ctrs and not any(isinstance(i, dict) and "ports" in i for i in ctrs.values()):
        return None   # старый агент портов не присылает
    return {c: pub for c, i in ctrs.items() if isinstance(i, dict) and (pub := public_ports(str(i.get("ports") or "")))}


# ---------- Доступ ----------

def _keyset(snap: dict) -> dict[tuple[str, str, str], dict]:
    out = {}
    for user, keys in (snap.get("keys") or {}).items():
        for k in keys or []:
            if isinstance(k, dict) and k.get("fp"):
                out[(str(user), str(k["fp"]), str(k.get("opts") or ""))] = k
    return out


def _key_line(sign: str, user: str, k: dict) -> str:
    comment = str(k.get("comment") or "")
    text = _('{sign} ключ SSH для {user}: {type} {fp}', sign=sign, user=html.escape(user),
             type=html.escape(str(k.get("type") or "")), fp=html.escape(str(k.get("fp"))[:22] + "…"))
    if comment:
        text += f" «{html.escape(comment)}»"
    if k.get("opts"):
        text += _(' (с ограничениями: {opts})', opts=html.escape(str(k["opts"])[:60]))
    return text


def diff(old: dict, new: dict) -> list[str]:
    """Что поменялось в доступе: строки для сообщения."""
    lines: list[str] = []
    ok, nk = _keyset(old), _keyset(new)
    lines += [_key_line("➕", u, nk[(u, fp, o)]) for (u, fp, o) in sorted(set(nk) - set(ok))]
    lines += [_key_line("➖", u, ok[(u, fp, o)]) for (u, fp, o) in sorted(set(ok) - set(nk))]
    ou, nu = set(old.get("login_users") or []), set(new.get("login_users") or [])
    lines += [_('➕ новый пользователь со входом: {u}', u=html.escape(u)) for u in sorted(nu - ou)]
    lines += [_('➖ пользователь больше не может входить: {u}', u=html.escape(u)) for u in sorted(ou - nu)]
    o0, n0 = set(old.get("uid0") or []), set(new.get("uid0") or [])
    lines += [_('⚠️ ещё один пользователь с правами root (uid 0): {u}', u=html.escape(u)) for u in sorted(n0 - o0)]
    og, ng = old.get("groups") or {}, new.get("groups") or {}
    for g in sorted(set(og) | set(ng)):
        a, b = set(og.get(g) or []), set(ng.get(g) or [])
        lines += [_('➕ {u} в группе {g}', u=html.escape(u), g=html.escape(g)) for u in sorted(b - a)]
        lines += [_('➖ {u} больше не в группе {g}', u=html.escape(u), g=html.escape(g)) for u in sorted(a - b)]
    os_, ns = old.get("sudoers") or {}, new.get("sudoers") or {}
    for f in sorted(set(os_) | set(ns)):
        if f not in os_:
            lines.append(_('➕ новый файл sudo: {f}', f=html.escape(f)))
        elif f not in ns:
            lines.append(_('➖ удалён файл sudo: {f}', f=html.escape(f)))
        elif os_[f] != ns[f]:
            lines.append(_('✏️ изменён файл sudo: {f}', f=html.escape(f)))
    return lines


def valid_snapshot(snap) -> bool:
    return isinstance(snap, dict) and isinstance(snap.get("keys"), dict) and isinstance(snap.get("sudoers"), dict)


def access_events(cfg: dict, baseline: dict | None, snap: dict | None, now: float, muted: bool,
                  server: str | None = None, log: list | None = None) -> tuple[list, dict | None]:
    """Сообщение об изменениях доступа и новая норма. Первый снимок — норма без сообщения. При выключенных
    уведомлениях норму не меняем: об изменении напишем, когда они включатся."""
    from checks import Alert
    if not cfg.get("access_alerts", True) or not valid_snapshot(snap):
        return [], baseline
    if not valid_snapshot(baseline):
        return [], snap
    lines = diff(baseline, snap)
    if not lines:
        return [], snap
    if muted:
        return [], baseline
    key = (f"{server}|" if server else "") + "access"
    if log is not None:
        log.append([now, key])
    more = _('\n… и ещё {n}', n=len(lines) - 15) if len(lines) > 15 else ""
    text = _('🔑 Изменился доступ к серверу:') + "\n" + "\n".join(lines[:15]) + more
    text += "\n" + _('Если это не вы — проверьте сервер.')
    return [Alert(text, key, "event", server=server)], snap


def local_snapshot(helper: str | None = None) -> dict | None:
    """Снимок доступа своего сервера через помощник. None — помощник старый, его нет или он не ответил."""
    import os
    import checks
    helper = helper or checks.HELPER
    if (time.time() < _helper["unsupported_until"] or checks.IS_WINDOWS or not os.path.exists(helper)
            or os.geteuid() == 0):
        return None
    try:
        r = subprocess.run(["sudo", "-n", helper, "access-audit"], capture_output=True, text=True,
                           timeout=30, encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None
    if r.returncode == 2 and "unknown command" in r.stdout:
        _helper["unsupported_until"] = time.time() + UNSUPPORTED_RETRY
        return None
    if r.returncode != 0:
        return None
    try:
        snap = json.loads(r.stdout)
    except ValueError:
        return None
    return snap if valid_snapshot(snap) else None


def events(cfg: dict, state, now: float, muted: bool, quiet: bool, snapshot_fn=None, exposed_fn=None) -> list:
    """Проверки своего сервера: изменения доступа (раз в access_check_min минут) и порты контейнеров."""
    snapshot_fn = snapshot_fn or local_snapshot   # ищем при вызове: тесты подменяют функции модуля
    exposed_fn = exposed_fn or local_exposed
    out: list = []
    every = (cfg.get("access_check_min") or 10) * 60
    if cfg.get("access_alerts", True) and now - state.access_checked >= every:
        state.access_checked = now
        snap = snapshot_fn()
        if snap is not None:
            ev, state.access = access_events(cfg, state.access or None, snap, now, muted, log=state.log)
            out += ev
    if cfg.get("docker_port_alerts", True) and (cfg.get("containers") or cfg.get("containers_auto")):
        ev, state.docker_exposed = port_events(cfg, state.docker_exposed, exposed_fn(), now, muted, quiet,
                                               log=state.log)
        out += ev
    return out


def remote_events(cfg: dict, state, now: float, name: str, data: dict, muted: bool, quiet: bool) -> list:
    """То же по ответу агента другого сервера (старый агент этих полей не шлёт — тогда ничего)."""
    out: list = []
    ev, base = access_events(cfg, state.remote_access.get(name), data.get("access"), now, muted, name, state.log)
    if base is not None:
        state.remote_access[name] = base
    out += ev
    ev, known = port_events(cfg, state.remote_docker_exposed.get(name, []), remote_exposed(data), now, muted,
                            quiet, name, state.log)
    state.remote_docker_exposed[name] = known
    out += ev
    return out
