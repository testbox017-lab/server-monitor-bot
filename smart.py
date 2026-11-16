"""Умные алерты: «необычно для этого часа» по истории метрик и прогноз роста памяти."""
from __future__ import annotations

import time

import psutil
from i18n import _

ANOM_MIN_DELTA = 25        # насколько процентов выше нормы, минимум
ANOM_SIGMAS = 3            # и не меньше чем на 3 разброса
ANOM_MIN_DAYS = 3          # норма считается, если есть хотя бы 3 дня истории для этого часа
ANOM_DURATION = 900        # необычная нагрузка должна держаться 15 минут
RAM_TREND_WINDOW = 6 * 3600
RAM_TREND_EVERY = 6 * 3600  # не чаще раза в 6 часов
NAMES = {"cpu": "CPU", "ram": "RAM"}

_cache: dict = {"hour": None, "profile": {}}


def profile(hist, now: float, tzinfo) -> dict:
    """Норма для текущего часа, пересчитывается раз в час."""
    hour = int(now // 3600)
    if _cache["hour"] != hour:
        _cache["profile"] = hist.hour_profile(now, tzinfo)
        _cache["hour"] = hour
    return _cache["profile"]


def anomaly_results(cfg: dict, sample: dict | None, hist, now: float, tzinfo, prof: dict | None = None) -> dict:
    """Ключи anom:cpu / anom:ram для общей системы алертов."""
    if not cfg.get("anomaly_alerts", True) or not sample or hist is None:
        return {}
    prof = profile(hist, now, tzinfo) if prof is None else prof
    th = cfg.get("thresholds", {})
    res: dict[str, str | None] = {}
    for metric, value in (("cpu", sample["cpu"]), ("ram", sample["ram"].percent)):
        res[f"anom:{metric}"] = None
        p = prof.get(metric)
        if not p:
            continue
        mean, std, days = p
        if days < ANOM_MIN_DAYS:
            continue
        limit = max(mean + ANOM_SIGMAS * std, mean + ANOM_MIN_DELTA)
        over_threshold = th.get(metric) is not None and value >= th[metric]
        if value >= limit and not over_threshold:  # выше порога — уже придёт обычный алерт
            res[f"anom:{metric}"] = (_('🧐 Необычно высокая {NAMES} для этого времени: {value:.0f}%, обычно в этот '
                                       'час около {mean:.0f}%',
                                       NAMES=NAMES[metric], value=value, mean=mean))
    return res


def top_memory_process() -> tuple[str, int] | None:
    best = None
    for p in psutil.process_iter(["name", "memory_info"]):
        try:
            rss = p.info["memory_info"].rss if p.info["memory_info"] else 0
        except psutil.Error:
            continue
        if best is None or rss > best[1]:
            best = (p.info["name"] or "?", rss)
    return best


def ram_trend_events(cfg: dict, state, hist, now: float, muted: bool, quiet: bool, top_fn=top_memory_process) -> list:
    """Память стабильно растёт и при таком темпе скоро закончится (похоже на утечку)."""
    from checks import Alert, fmt_bytes

    if not cfg.get("ram_trend_alerts", True) or hist is None or muted or quiet:
        return []
    if now - state.ram_trend_at < RAM_TREND_EVERY:
        return []
    t = hist.trend("ram", RAM_TREND_WINDOW, now)
    if not t:
        return []
    per_hour, r2, last = t
    if per_hour < 0.5 or r2 < 0.8 or last < 50:
        return []
    hours = (100 - last) / per_hour
    if hours > 12:
        return []
    state.ram_trend_at = now
    state.log.append([now, "ram_trend"])
    text = (_('📈 Память растёт: +{v:.0f}% за 6 ч, сейчас {last:.0f}%. При таком темпе закончится примерно через '
              '{max:.0f} ч.',
              v=per_hour * RAM_TREND_WINDOW / 3600, last=last, max=max(hours, 0.5)))
    top = top_fn()
    if top:
        text += _(' Больше всех памяти занимает {top} ({fmt_bytes}).', top=top[0], fmt_bytes=fmt_bytes(top[1]))
    return [Alert(text, None, "event")]


def anomaly_duration(duration: int) -> int:
    return max(duration, ANOM_DURATION)


def clean(state, now: float | None = None) -> None:
    now = now or time.time()
    state.snoozed = {k: v for k, v in state.snoozed.items() if v > now}
