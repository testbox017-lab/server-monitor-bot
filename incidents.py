"""Журнал инцидентов и недельный отчёт.

Инцидент — проблема, которая продержалась дольше порога (то же условие, что и для алерта), от начала до
восстановления. Записывается всегда, даже в тихие часы и при выключенных уведомлениях: журнал про сервер,
а не про сообщения. События без длительности (перезапуск контейнера, OOM, вход по SSH) — отдельные строки.
Хранится в metrics.db рядом с историей метрик, поэтому попадает в копию бота.
"""
from __future__ import annotations

import html
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from i18n import N_, _

RETENTION_DAYS = 180
WEEK = 7 * 86400
EVENT_KINDS = {"🔁": N_('перезапуски контейнеров'), "💥": "OOM killer", "🔐": N_('входы по SSH'),
               "🛡": N_('подбор пароля по SSH'), "🚪": N_('новые открытые порты'), "🩹": N_('обновления безопасности'),
               "📦": N_('обновления пакетов'),
               "🔒": N_('SSL-сертификаты'), "🚫": N_('автобан IP'), "📈": N_('прогноз памяти'), "🚀": N_('деплои'),
               "🌐": N_('сбои связи'), "⚠️": N_('прочие предупреждения')}
PLANNED_KINDS = (N_('деплои'),)  # плановые события: в отчёте отдельной строкой, не среди тревог

_lock = threading.Lock()


class Incidents:
    def __init__(self, path: Path):
        self.path = path
        self._last_cleanup = 0.0
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS inc (id INTEGER PRIMARY KEY, key TEXT NOT NULL, text TEXT, "
                      "start REAL NOT NULL, end REAL, kind TEXT NOT NULL DEFAULT 'problem')")
            c.execute("CREATE INDEX IF NOT EXISTS inc_start ON inc(start)")
            self._open = {r[0] for r in c.execute("SELECT key FROM inc WHERE end IS NULL AND kind = 'problem'")}

    @contextmanager
    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        try:
            with c:
                yield c
        finally:
            c.close()

    # ---------- запись ----------

    def open(self, key: str, text: str, start: float) -> None:
        if key in self._open:
            return
        with _lock, self._conn() as c:
            c.execute("INSERT INTO inc (key, text, start) VALUES (?,?,?)", (key, text[:300], start))
        self._open.add(key)

    def close(self, key: str, end: float) -> None:
        if key not in self._open:
            return
        with _lock, self._conn() as c:
            c.execute("UPDATE inc SET end = ? WHERE key = ? AND end IS NULL AND kind = 'problem'", (end, key))
        self._open.discard(key)

    def event(self, key: str | None, text: str, ts: float) -> None:
        with _lock, self._conn() as c:
            c.execute("INSERT INTO inc (key, text, start, end, kind) VALUES (?,?,?,?, 'event')",
                      (key or "event", text[:300], ts, ts))
            if ts - self._last_cleanup > 86400:
                c.execute("DELETE FROM inc WHERE end IS NOT NULL AND end < ?", (ts - RETENTION_DAYS * 86400,))
                self._last_cleanup = ts

    def open_keys(self) -> set[str]:
        return set(self._open)

    # ---------- чтение ----------

    def between(self, since: float, until: float, kind: str = "problem") -> list[tuple]:
        """[(key, text, start, end)] — пересекающиеся с периодом; end None — идёт сейчас."""
        with _lock, self._conn() as c:
            return c.execute("SELECT key, text, start, end FROM inc WHERE kind = ? AND start < ? "
                             "AND (end IS NULL OR end >= ?) ORDER BY start", (kind, until, since)).fetchall()

    def recent(self, n: int = 15) -> list[tuple]:
        with _lock, self._conn() as c:
            return c.execute("SELECT key, text, start, end FROM inc WHERE kind = 'problem' "
                             "ORDER BY start DESC LIMIT ?", (n,)).fetchall()


def clipped(rows: list[tuple], since: float, until: float) -> list[tuple[str, str, float, bool]]:
    """[(key, text, длительность внутри периода, ещё идёт)]."""
    return [(k, t, max(0.0, min(e if e is not None else until, until) - max(s, since)), e is None)
            for k, t, s, e in rows]


def planned(text: str | None) -> bool:
    from checks import MAINT_MARK
    return bool(text) and text.startswith(MAINT_MARK)


def downtime_by_key(rows: list[tuple], since: float, until: float) -> dict[str, tuple[int, float]]:
    out: dict[str, tuple[int, float]] = {}
    for k, __, dur, __ in clipped(rows, since, until):
        n, total = out.get(k, (0, 0.0))
        out[k] = (n + 1, total + dur)
    return out


def event_kind(text: str) -> str:
    for mark, name in EVENT_KINDS.items():
        if text.startswith(mark):
            return name
    return EVENT_KINDS["⚠️"]


def local(cfg: dict, ts: float, fmt: str = "%d.%m %H:%M") -> str:
    from checks import tz
    return datetime.fromtimestamp(ts, tz(cfg)).strftime(fmt)


def incidents_text(inc: Incidents, cfg: dict, now: float | None = None, n: int = 15) -> str:
    from checks import fmt_duration, key_label
    now = now or time.time()
    rows = inc.recent(n)
    lines = [_('📒 <b>Журнал инцидентов</b>')]
    if not rows:
        lines.append(_('Пока пусто: ни одна проблема не держалась дольше порога.'))
        return "\n".join(lines)
    for k, text, s, e in rows:
        label = html.escape(key_label(k)) + (_(' (обслуживание)') if planned(text) else "")
        if e is None:
            lines.append(_('🔴 {label}: с {local}, идёт {fmt_duration}',
                           label=label, local=local(cfg, s), fmt_duration=fmt_duration(now - s)))
        else:
            lines.append(f"• {local(cfg, s)} {label}: {fmt_duration(max(e - s, 60))}")
    week = [r for r in inc.between(now - WEEK, now) if not planned(r[1])]
    total = sum(d for *__, d, __ in clipped(week, now - WEEK, now))
    lines.append(_('\n'
                   'За 7 дней: {len} инцидент(ов), простоя в сумме {v}',
                   len=len(week), v=fmt_duration(total) if total else _('0 мин')))
    return "\n".join(lines)


def availability(down: float, period: float) -> str:
    pct = 100 * (1 - down / period)
    return "100%" if down <= 0 else f"{min(pct, 99.99):.2f}%"


def weekly_text(cfg: dict, inc: Incidents, hist=None, now: float | None = None, extra: list[str] | None = None) -> str:
    """Отчёт за 7 дней до now: инциденты, доступность, нагрузка, события, плюс строки extra (бэкапы и т.п.)."""
    from checks import fmt_bytes, fmt_duration, key_label
    now = now or time.time()
    since, prev = now - WEEK, now - 2 * WEEK
    lines = [_('📅 <b>Неделя {local}–{local2}</b>', local=local(cfg, since, '%d.%m'), local2=local(cfg, now, '%d.%m'))]
    # Бот мог начать наблюдение посреди недели: тогда честно пишем, за какой срок данные,
    # и доступность считаем от этого момента, а не от всей недели
    first = hist.first_ts() if hist is not None and hasattr(hist, "first_ts") else None
    period = WEEK
    if first is not None and first > since + 3600:
        period = max(now - first, 3600)
        lines.append(_('ℹ️ Данные только с {local} ({fmt_duration}): бот наблюдает с этого времени',
                       local=local(cfg, first), fmt_duration=fmt_duration(period)))

    allrows = inc.between(since, now)
    rows = [r for r in allrows if not planned(r[1])]
    plan = [r for r in allrows if planned(r[1])]
    prev_n = len([r for r in inc.between(prev, since) if not planned(r[1])])
    by_key = downtime_by_key(rows, since, now)
    total = sum(v[1] for v in by_key.values())
    if not rows:
        lines.append(_('✅ Инцидентов не было') + (_(' (неделей раньше: {prev_n})', prev_n=prev_n) if prev_n else ""))
    else:
        lines.append(_('🚨 Инцидентов: {len} (неделей раньше {prev_n}), простоя в сумме {fmt_duration}',
                       len=len(rows), prev_n=prev_n, fmt_duration=fmt_duration(total)))
        for k, (n, dur) in sorted(by_key.items(), key=lambda x: -x[1][1])[:6]:
            lines.append(_('  • {escape}: {n} раз, {fmt_duration}',
                           escape=html.escape(key_label(k)), n=n, fmt_duration=fmt_duration(max(dur, 60))))
        still = [k for k, *__, e in rows if e is None]
        if still:
            lines.append(_('  Не решено сейчас: ') + ", ".join(html.escape(key_label(k)) for k in still))
    if plan:
        pdur = sum(d for *__, d, __ in clipped(plan, since, now))
        lines.append(_('🔧 Во время обслуживания: {len} проблем(ы), {fmt_duration}, в простой не считаются',
                       len=len(plan), fmt_duration=fmt_duration(max(pdur, 60))))

    targets = [f"web:{p}" if isinstance(p, str) else None for p in cfg.get("probes") or []]
    targets = [t for t in targets if t]
    if targets:
        lines.append(_('\n🌐 Доступность снаружи:'))
        for t in targets[:8]:
            down = by_key.get(t, (0, 0.0))[1]
            lines.append(f"  • {html.escape(t[4:])}: {availability(down, period)}")

    p = hist.peaks(WEEK, now) if hist is not None else None
    if p:
        lines.append(_('\n📊 Нагрузка:'))
        lines.append(_('  CPU в среднем {cpu_avg:.0f}%, пик {cpu:.0f}% ({local})',
                       cpu_avg=p['cpu_avg'], cpu=p['cpu'][0], local=local(cfg, p['cpu'][1])))
        lines.append(_('  RAM пик {ram:.0f}% ({local})', ram=p['ram'][0], local=local(cfg, p['ram'][1])))
        if p["disk_from"] is not None:
            lines.append(_('  Диск {disk_from:.1f}% → {disk_to:.1f}%', disk_from=p['disk_from'], disk_to=p['disk_to']))
        lines.append(_('  Трафик ↓ {fmt_bytes} ↑ {fmt_bytes2}',
                       fmt_bytes=fmt_bytes(p['rx']), fmt_bytes2=fmt_bytes(p['tx'])))
        days = hist.disk_forecast(now)
        if days is not None and days < 365:
            lines.append(_('  ⚠️ Диск заполнится примерно через {days:.0f} дн.', days=days))

    events = inc.between(since, now, "event")
    if events:
        counts: dict[str, int] = {}
        for __, text, *__ in events:
            name = event_kind(text or "")
            counts[name] = counts.get(name, 0) + 1
        plan_ev = {k: counts.pop(k) for k in PLANNED_KINDS if k in counts}
        if counts:
            lines.append(_('\n🗒 События: ') + ", ".join(f"{_(name)} — {n}" for name, n in
                                                     sorted(counts.items(), key=lambda x: -x[1])))
        if plan_ev:
            lines.append(("\n" if not counts else "") + _('🚀 Плановое: ')
                         + ", ".join(f"{_(name)} — {n}" for name, n in plan_ev.items()))
    if extra:
        lines.append("")
        lines += extra
    return "\n".join(lines)
