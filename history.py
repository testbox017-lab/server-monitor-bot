"""История метрик в SQLite, графики и прогноз заполнения диска."""
from __future__ import annotations

import io
import sqlite3
from contextlib import contextmanager
import threading
import time
from datetime import datetime
from pathlib import Path

RETENTION_DAYS = 30
PERIODS = {"1h": 3600, "24h": 86400, "7d": 7 * 86400}
PERIOD_NAMES = {"1h": "1 час", "24h": "24 часа", "7d": "7 дней"}

_lock = threading.Lock()


class History:
    def __init__(self, path: Path):
        self.path = path
        self._last_cleanup = 0.0
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS m (ts INTEGER PRIMARY KEY, cpu REAL, ram REAL, "
                      "disk REAL, rx INTEGER, tx INTEGER, load REAL)")

    @contextmanager
    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        try:
            with c:  # commit / rollback
                yield c
        finally:
            c.close()

    def record(self, sample: dict | None, now: float | None = None) -> None:
        """sample — результат checks.collect()."""
        if not sample:
            return
        now = int(now or time.time())
        disks = sample.get("disks") or {}
        root = disks.get("/") or disks.get("C:\\")
        disk = root.percent if root else max((d.percent for d in disks.values()), default=None)
        net = sample.get("net")
        load = sample["load"][0] if sample.get("load") else None
        with _lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO m VALUES (?,?,?,?,?,?,?)",
                      (now, sample["cpu"], sample["ram"].percent, disk,
                       net.bytes_recv if net else None, net.bytes_sent if net else None, load))
            if now - self._last_cleanup > 3600:
                c.execute("DELETE FROM m WHERE ts < ?", (now - RETENTION_DAYS * 86400,))
                self._last_cleanup = now

    def rows(self, since: float, until: float | None = None) -> list[tuple]:
        until = until or time.time()
        with _lock, self._conn() as c:
            return c.execute("SELECT ts, cpu, ram, disk, rx, tx, load FROM m WHERE ts >= ? AND ts <= ? "
                             "ORDER BY ts", (int(since), int(until))).fetchall()

    # ---------- аналитика ----------

    def peaks(self, seconds: int = 86400, now: float | None = None) -> dict | None:
        now = now or time.time()
        rows = self.rows(now - seconds, now)
        if not rows:
            return None
        cpu = max(rows, key=lambda r: r[1] or 0)
        ram = max(rows, key=lambda r: r[2] or 0)
        disks = [r[3] for r in rows if r[3] is not None]
        rx, tx = _traffic(rows)
        return {"cpu": (cpu[1], cpu[0]), "ram": (ram[2], ram[0]),
                "cpu_avg": sum(r[1] or 0 for r in rows) / len(rows),
                "disk_from": disks[0] if disks else None, "disk_to": disks[-1] if disks else None,
                "rx": rx, "tx": tx, "points": len(rows)}

    def disk_forecast(self, now: float | None = None, window: int = 7 * 86400) -> float | None:
        """Через сколько дней диск заполнится при текущем темпе. None — данных мало или место не убывает."""
        now = now or time.time()
        pts = [(r[0], r[3]) for r in self.rows(now - window, now) if r[3] is not None]
        if len(pts) < 10 or pts[-1][0] - pts[0][0] < 6 * 3600:
            return None
        n = len(pts)
        mx = sum(p[0] for p in pts) / n
        my = sum(p[1] for p in pts) / n
        var = sum((p[0] - mx) ** 2 for p in pts)
        if var == 0:
            return None
        slope = sum((p[0] - mx) * (p[1] - my) for p in pts) / var  # % в секунду
        per_day = slope * 86400
        if per_day <= 0.01:  # меньше 0.01% в сутки — считаем, что не растёт
            return None
        return (100 - pts[-1][1]) / per_day


def _traffic(rows: list[tuple]) -> tuple[int, int]:
    """Сумма трафика за период; сбросы счётчика (перезагрузка) пропускаем."""
    rx = tx = 0
    for a, b in zip(rows, rows[1:]):
        if None in (a[4], b[4], a[5], b[5]):
            continue
        if b[4] >= a[4]:
            rx += b[4] - a[4]
        if b[5] >= a[5]:
            tx += b[5] - a[5]
    return rx, tx


def _bucket(rows: list[tuple], max_points: int = 400) -> list[tuple]:
    """Усреднение до max_points точек, чтобы график недели не был кашей."""
    if len(rows) <= max_points:
        return rows
    size = len(rows) / max_points
    out = []
    for i in range(max_points):
        chunk = rows[int(i * size):int((i + 1) * size)] or [rows[-1]]
        avg = []
        for col in range(len(chunk[0])):
            vals = [r[col] for r in chunk if r[col] is not None]
            avg.append(sum(vals) / len(vals) if vals else None)
        avg[4], avg[5] = chunk[-1][4], chunk[-1][5]  # счётчики сети берём последним значением
        out.append(tuple(avg))
    return out


def chart_png(hist: History, period: str, tz, host: str, now: float | None = None) -> bytes | None:
    """PNG: CPU/RAM, диск и сеть за период. None — данных нет."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    now = now or time.time()
    rows = hist.rows(now - PERIODS[period], now)
    if len(rows) < 2:
        return None
    rows = _bucket(rows)
    t = [datetime.fromtimestamp(r[0], tz) for r in rows]

    # скорость сети, Мбит/с
    net_t, rx_rate, tx_rate = [], [], []
    for a, b, tb in zip(rows, rows[1:], t[1:]):
        dt = b[0] - a[0]
        if dt <= 0 or None in (a[4], b[4], a[5], b[5]) or b[4] < a[4] or b[5] < a[5]:
            continue
        net_t.append(tb)
        rx_rate.append((b[4] - a[4]) * 8 / dt / 1e6)
        tx_rate.append((b[5] - a[5]) * 8 / dt / 1e6)

    fig, axes = plt.subplots(3, 1, figsize=(9, 7.5), sharex=True,
                             gridspec_kw={"height_ratios": [2, 1, 1]})
    blue, orange, grey = "#2b6cb0", "#dd6b20", "#4a5568"
    ax = axes[0]
    ax.plot(t, [r[1] for r in rows], color=blue, lw=1.4, label="CPU")
    ax.plot(t, [r[2] for r in rows], color=orange, lw=1.4, label="RAM")
    ax.set_ylim(0, 100)
    ax.set_ylabel("%")
    ax.legend(loc="upper left", frameon=False, ncol=2)
    ax.set_title(f"{host}: последние {PERIOD_NAMES[period]}", loc="left", fontsize=12)

    ax = axes[1]
    disk = [r[3] for r in rows]
    ax.plot(t, disk, color=grey, lw=1.4)
    lo = min((d for d in disk if d is not None), default=0)
    hi = max((d for d in disk if d is not None), default=100)
    pad = max(2, (hi - lo) * 0.3)
    ax.set_ylim(max(0, lo - pad), min(100, hi + pad))
    ax.set_ylabel("Диск, %")

    ax = axes[2]
    if net_t:
        ax.plot(net_t, rx_rate, color=blue, lw=1.2, label="Входящий")
        ax.plot(net_t, tx_rate, color=orange, lw=1.2, label="Исходящий")
        ax.legend(loc="lower right", bbox_to_anchor=(1, 1), frameon=False, ncol=2, borderaxespad=0.2)
    ax.set_ylabel("Мбит/с")
    ax.set_ylim(bottom=0)

    fmt = "%H:%M" if period != "7d" else "%d.%m"
    axes[2].xaxis.set_major_formatter(mdates.DateFormatter(fmt, tz=tz))
    for a in axes:
        a.grid(True, alpha=0.25)
        for side in ("top", "right"):
            a.spines[side].set_visible(False)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    return buf.getvalue()


def chart_caption(hist: History, period: str, now: float | None = None) -> str:
    p = hist.peaks(PERIODS[period], now)
    if not p:
        return "Данных пока нет"
    from checks import fmt_bytes
    parts = [f"Пик CPU {p['cpu'][0]:.0f}%, в среднем {p['cpu_avg']:.0f}%",
             f"пик RAM {p['ram'][0]:.0f}%",
             f"трафик ↓ {fmt_bytes(p['rx'])} ↑ {fmt_bytes(p['tx'])}"]
    days = hist.disk_forecast(now)
    if days is not None:
        parts.append(f"диск заполнится примерно через {days:.0f} дн." if days < 365 else "диск растёт очень медленно")
    return ", ".join(parts)
