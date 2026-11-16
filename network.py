"""Сеть и диск подробнее: скорость по интерфейсам, трафик за сутки и неделю, кто подключён, кто грузит диск."""
from __future__ import annotations

import html
import ipaddress
import json
import os
import time

import psutil
from i18n import _

SKIP_IFACES = ("lo", "veth", "br-", "docker")


def iface_rates(interval: float = 1.0) -> list[tuple[str, float, float]]:
    """[(интерфейс, приём байт/с, отдача байт/с)] — без lo и виртуальных интерфейсов Docker."""
    a = psutil.net_io_counters(pernic=True)
    time.sleep(interval)
    b = psutil.net_io_counters(pernic=True)
    rows = []
    for name, c in b.items():
        if name.startswith(SKIP_IFACES) or name not in a:
            continue
        rows.append((name, max(0, c.bytes_recv - a[name].bytes_recv) / interval,
                     max(0, c.bytes_sent - a[name].bytes_sent) / interval))
    return sorted(rows, key=lambda r: r[1] + r[2], reverse=True)


def remote_ips(n: int = 5, conns=None) -> list[tuple[str, int]]:
    """Внешние IP с наибольшим числом установленных TCP-соединений."""
    counts: dict[str, int] = {}
    try:
        conns = psutil.net_connections(kind="inet") if conns is None else conns
    except (psutil.AccessDenied, OSError):
        return []
    for c in conns:
        if c.status != psutil.CONN_ESTABLISHED or not c.raddr:
            continue
        ip = c.raddr[0].removeprefix("::ffff:")
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if a.is_loopback or a.is_private or a.is_link_local:
            continue
        counts[ip] = counts.get(ip, 0) + 1
    return sorted(counts.items(), key=lambda x: -x[1])[:n]


def inode_usage(paths: list[str]) -> dict[str, float]:
    out = {}
    for p in paths:
        try:
            st = os.statvfs(p)
        except (OSError, AttributeError):
            continue
        if st.f_files:
            out[p] = 100 * (st.f_files - st.f_ffree) / st.f_files
    return out


def io_top(helper_fn) -> list[dict] | None:
    ok, out = helper_fn("io-top", timeout=20)
    if not ok:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def net_text(cfg: dict, hist=None, helper_fn=None, rates_fn=iface_rates, ips_fn=remote_ips) -> str:
    from checks import disk_paths, fmt_bytes
    lines = [_('🌐 <b>Сеть и диск</b>')]
    for name, rx, tx in rates_fn():
        lines.append(_('{escape}: ↓ {fmt_bytes}/с ↑ {fmt_bytes2}/с',
                       escape=html.escape(name), fmt_bytes=fmt_bytes(rx), fmt_bytes2=fmt_bytes(tx)))
    if hist is not None:
        for title, sec in ((_('за сутки'), 86400), (_('за неделю'), 7 * 86400)):
            p = hist.peaks(sec)
            if p:
                lines.append(_('Трафик {title}: ↓ {fmt_bytes} ↑ {fmt_bytes2}',
                               title=title, fmt_bytes=fmt_bytes(p['rx']), fmt_bytes2=fmt_bytes(p['tx'])))
    ips = ips_fn()
    if ips:
        lines.append(_('\nБольше всего соединений:'))
        lines += [f"  • {html.escape(ip)}: {n}" for ip, n in ips]
    if helper_fn is not None:
        top = io_top(helper_fn)
        if top is None:
            lines.append(_('\nДиск по процессам: помощник не обновлён'))
        elif top:
            lines.append(_('\nДиск сейчас (чтение / запись в секунду):'))
            lines += [f"  • {html.escape(r['name'])} ({r['pid']}): {fmt_bytes(r['read'])} / {fmt_bytes(r['write'])}"
                      for r in top[:5]]
        else:
            lines.append(_('\nДиск сейчас: никто не читает и не пишет'))
    for path, pct in inode_usage(disk_paths(cfg.get("disks"))).items():
        lines.append(_('Inode на {escape}: {pct:.0f}%', escape=html.escape(path), pct=pct))
    return "\n".join(lines)
