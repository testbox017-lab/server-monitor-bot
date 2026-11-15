"""Telegram-бот мониторинга сервера.

Запуск:  python bot.py [путь_к_config.yaml]
Управление командами (/status, /procs, ...) или кнопками.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import html
import json
import logging
import sys
import time
from pathlib import Path

from telegram import (InlineKeyboardButton as Btn, InlineKeyboardMarkup, InputMediaPhoto, KeyboardButton,
                      ReplyKeyboardMarkup, Update)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

import actions
import backups
import checks
import history
import probes
import security
import updater
from settings import Settings

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("monitor_bot")

CONFIG_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("config.yaml")
S = Settings(CONFIG_PATH)
CFG = S.cfg  # общий словарь, обновляется на месте при изменении настроек
STATE = checks.AlertState.load(S.dir / "state.json")
HIST = history.History(S.dir / "metrics.db")
ACT = actions.Actions(S.dir / "actions.log")
HOST = checks.platform.node()

# ---------- Клавиатуры ----------

B_STATUS, B_PROCS, B_SERVICES, B_ALERTS, B_SETTINGS = "📊 Статус", "🔝 Процессы", "⚙️ Службы", "🚨 Алерты", "🛠 Настройки"

MAIN_KB = ReplyKeyboardMarkup(
    [[KeyboardButton(B_STATUS), KeyboardButton(B_PROCS)],
     [KeyboardButton(B_SERVICES), KeyboardButton(B_ALERTS)],
     [KeyboardButton(B_SETTINGS)]],
    resize_keyboard=True, is_persistent=True)


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([list(r) for r in rows if r])


def status_kb() -> InlineKeyboardMarkup:
    return kb([Btn("🔄 Обновить", callback_data="status"), Btn("📈 График", callback_data="chart:24h")],
              [Btn("🔝 Процессы", callback_data="procs:cpu:10"), Btn("⚙️ Службы", callback_data="services")])


def procs_kb(by: str, n: int) -> InlineKeyboardMarkup:
    other = "mem" if by == "cpu" else "cpu"
    return kb([Btn("🔄 Обновить", callback_data=f"procs:{by}:{n}"),
               Btn("По RAM" if other == "mem" else "По CPU", callback_data=f"procs:{other}:{n}")],
              [Btn("Показать 20" if n == 10 else "Показать 10", callback_data=f"procs:{by}:{20 if n == 10 else 10}")]
              + ([Btn("⛔ Остановить…", callback_data="act:killmenu")] if actions_on() else []))


def actions_on() -> bool:
    return bool(CFG.get("actions_enabled", True))


def action_services() -> list[str]:
    return list(CFG.get("action_services") or [])


def act_btn(label: str, data: str) -> Btn | None:
    return Btn(label, callback_data=data) if actions_on() and len(data.encode()) <= 64 else None


def logs_btn(name: str, label: str | None = None) -> Btn | None:
    data = f"logs:{name}"
    return Btn(label or f"📜 {name}", callback_data=data) if len(data.encode()) <= 60 else None


def services_kb() -> InlineKeyboardMarkup:
    logs = [b for b in (logs_btn(c) for c in checks.watched_containers(CFG, STATE)) if b]
    return kb([Btn("🔄 Обновить", callback_data="services"),
               Btn("✏️ Что отслеживать", callback_data="set:watch")],
              *[logs[i:i + 2] for i in range(0, len(logs), 2)],
              [b for b in [act_btn("🧰 Действия", "act:menu")] if b])


def short_id(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:10]


def active_key(kid: str) -> str | None:
    return next((k for k in STATE.active if short_id(k) == kid), None)


def alert_kb(key: str | None = None, ackable: bool = False) -> InlineKeyboardMarkup:
    extra = []
    fix = None
    if key and key.startswith(("ctr:", "log:")):
        b = logs_btn(checks.key_label(key), "📜 Логи")
        if b:
            extra.append(b)
        fix = act_btn("♻️ Перезапустить", f"act:ctr:{checks.key_label(key)}")
    elif key and key.startswith("svc:") and checks.key_label(key) in action_services():
        fix = act_btn("♻️ Перезапустить", f"act:svc:{checks.key_label(key)}")
    elif key and key.startswith("disk:"):
        fix = act_btn("🧹 Очистить место", "act:clean")
    ack = None
    if ackable and key:
        ack = [Btn("✔️ Принял", callback_data=f"ack:{short_id(key)}"),
               Btn("💤 Этот на 1 ч", callback_data=f"snz:{short_id(key)}")]
    return kb(ack,
              [Btn("🔕 Все на 1 ч", callback_data="mute:3600:a"), Btn("🌙 До утра", callback_data="mute:morning:a")],
              [Btn("📊 Статус сейчас", callback_data="status:new")] + extra,
              [fix] if fix else None)


def group_text(alerts: list) -> str:
    lines = [f"<b>{html.escape(HOST)}</b>", f"Сразу {len(alerts)} событий:"]
    for a in alerts:
        lines.append("• " + html.escape(a.text[:300]))
    text = "\n".join(lines)
    return text if len(text) < 3900 else text[:3900] + "\n…"


def group_kb() -> InlineKeyboardMarkup:
    return kb([Btn("✔️ Принять все", callback_data="ackall"), Btn("🔕 Все на 1 ч", callback_data="mute:3600:a")],
              [Btn("🚨 Активные алерты", callback_data="alerts:new"), Btn("📊 Статус", callback_data="status:new")])


def actions_menu() -> tuple[str, InlineKeyboardMarkup]:
    rows = []
    ctrs = [b for b in (act_btn(f"♻️ {c}", f"act:ctr:{c}") for c in checks.watched_containers(CFG, STATE)) if b]
    svcs = [b for b in (act_btn(f"♻️ {s}", f"act:svc:{s}") for s in action_services()) if b]
    rows += [ctrs[i:i + 2] for i in range(0, len(ctrs), 2)]
    rows += [svcs[i:i + 3] for i in range(0, len(svcs), 3)]
    rows.append([Btn("🧹 Очистить место", callback_data="act:clean"),
                 Btn("⛔ Остановить процесс", callback_data="act:killmenu")])
    rows.append([Btn("📜 Журнал действий", callback_data="act:log"),
                 Btn("🔄 Перезагрузить сервер", callback_data="act:reboot")])
    text = ("🧰 <b>Действия</b>\nКаждое действие нужно подтвердить. Контейнеры перезапускаются через docker, "
            "службы — только из списка action_services в config.yaml.")
    return text, InlineKeyboardMarkup(rows)


def confirm_kb(token: str, yes: str = "✅ Да, выполнить") -> InlineKeyboardMarkup:
    return kb([Btn(yes, callback_data=f"do:{token}"), Btn("✖️ Отмена", callback_data=f"no:{token}")])


def actions_log_text() -> str:
    rows = ACT.last(10)
    if not rows:
        return "📜 Журнал действий пуст"
    lines = ["📜 <b>Последние действия</b>"]
    for r in rows:
        if len(r) >= 4:
            lines.append(f"{'✅' if r[3] == 'ok' else '❌'} {html.escape(r[0][5:16])} {html.escape(r[2])}")
    return "\n".join(lines)


def chart_kb(period: str) -> InlineKeyboardMarkup:
    return kb([Btn(("• " if p == period else "") + history.PERIOD_NAMES[p], callback_data=f"chart:{p}:e")
               for p in history.PERIODS])


async def make_chart(period: str) -> tuple[bytes | None, str]:
    png = await asyncio.to_thread(history.chart_png, HIST, period, checks.tz(CFG), HOST)
    caption = await asyncio.to_thread(history.chart_caption, HIST, period)
    return png, caption


async def send_chart(update: Update, period: str) -> None:
    png, caption = await make_chart(period)
    if png is None:
        await send(update, "📈 Данных для графика пока нет: история копится раз в минуту, загляните чуть позже.")
        return
    await update.effective_message.reply_photo(png, caption=caption, reply_markup=chart_kb(period))


def logs_kb(name: str, tail: int) -> InlineKeyboardMarkup:
    return kb([Btn("🔄 Обновить", callback_data=f"logs:{name}:{tail}"),
               Btn("100 строк" if tail == 30 else "30 строк", callback_data=f"logs:{name}:{100 if tail == 30 else 30}")])


def alerts_kb() -> InlineKeyboardMarkup:
    rows = [[Btn("🔄 Обновить", callback_data="alerts")]]
    if STATE.muted(time.time()):
        rows.append([Btn("🔔 Включить уведомления", callback_data="mute:off")])
    else:
        rows.append([Btn("🔕 1 ч", callback_data="mute:3600"), Btn("🔕 3 ч", callback_data="mute:10800"),
                     Btn("🌙 До утра", callback_data="mute:morning")])
    return kb(*rows)


# ---------- Тексты ----------

def alerts_text() -> str:
    now = time.time()
    lines = []
    if STATE.muted(now):
        lines.append(f"🔕 Уведомления выключены до {checks.fmt_local(CFG, STATE.mute_until)}")
    if checks.in_quiet_hours(CFG, now):
        lines.append("🌙 Сейчас тихие часы: приходят только критичные алерты")
    if STATE.active:
        lines.append("🚨 <b>Активные алерты</b>")
        for k in STATE.active:
            since = STATE.since.get(k)
            mins = f" ({int((now - since) // 60)} мин)" if since else ""
            mark = ""
            if k in STATE.acked:
                mark = " ✔️ принято"
            elif STATE.snoozed.get(k, 0) > now:
                mark = f" 💤 до {checks.fmt_local(CFG, STATE.snoozed[k])}"
            lines.append(html.escape(STATE.problems.get(k, checks.key_label(k))) + mins + mark)
    else:
        lines.append("✅ Активных алертов нет")
    return "\n".join(lines)


def onoff(v: bool) -> str:
    return "✅" if v else "▫️"


def settings_text() -> str:
    th = CFG["thresholds"]
    q = CFG.get("quiet_hours")
    remind = CFG.get("remind_min") or 0
    summary = CFG.get("daily_summary")
    return "\n".join([
        "🛠 <b>Настройки</b>",
        f"CPU ≥ {th.get('cpu')}%, RAM ≥ {th.get('ram')}%, диск ≥ {th.get('disk')}%",
        f"Сколько должно держаться превышение: {th.get('duration_sec', 300) // 60} мин",
        f"Проверка каждые {CFG.get('check_interval_sec', 60)} с",
        f"Тихие часы: {q['start']}–{q['end']}" if q else "Тихие часы: выкл",
        f"Напоминать о нерешённой проблеме: каждые {remind} мин" if remind else "Напоминания: выкл",
        f"Ежедневная сводка: в {summary}" if summary else "Ежедневная сводка: выкл",
        f"Служб: {len(CFG.get('services') or [])}, контейнеров: {len(CFG.get('containers') or [])}, "
        f"процессов: {len(CFG.get('processes') or [])}",
        "Docker: " + ("все контейнеры" if CFG.get("containers_auto") else "только из списка")
        + (f", ошибки в логах не чаще раза в {CFG.get('log_alert_min') or 15} мин" if CFG.get("log_alerts", True)
           else ", ошибки в логах: выкл"),
        f"Проверок снаружи: {len(CFG.get('probes') or [])}"
        + (f", SSL предупреждать за {CFG.get('ssl_warn_days') or 14} дн." if CFG.get("ssl_alerts", True) else ""),
        f"Часовой пояс: {CFG.get('tz')}",
        f"Версия бота: {updater.short(updater.current().get('sha'))}",
    ])


def settings_kb() -> InlineKeyboardMarkup:
    return kb([Btn("📏 Пороги", callback_data="set:th"), Btn("⏱ Интервал", callback_data="set:int")],
              [Btn("👁 Что отслеживать", callback_data="set:watch")],
              [Btn("🌙 Тихие часы", callback_data="set:quiet"), Btn("⏰ Напоминания", callback_data="set:remind")],
              [Btn("📋 Ежедневная сводка", callback_data="set:summary"), Btn("🐳 Docker", callback_data="set:docker")],
              [Btn("🛡 Безопасность", callback_data="set:sec"), Btn("🌐 Сайты и порты", callback_data="set:probes")],
              [Btn("🧠 Умные алерты", callback_data="set:smart"), Btn("💾 Бэкапы", callback_data="set:backup")],
              [Btn("⬆️ Обновление бота", callback_data="set:update")],
              [Btn("↩️ Сбросить к config.yaml", callback_data="set:reset")])


BACK = Btn("⬅️ Назад", callback_data="set")


def thresholds_view() -> tuple[str, InlineKeyboardMarkup]:
    th = CFG["thresholds"]
    rows = []
    for key, name in (("cpu", "CPU"), ("ram", "RAM"), ("disk", "Диск")):
        rows.append([Btn(f"{name}: {th.get(key)}%", callback_data="noop"),
                     Btn("−5", callback_data=f"th:{key}:-5"), Btn("+5", callback_data=f"th:{key}:5")])
    d = th.get("duration_sec", 300)
    rows.append([Btn(("• " if d == s else "") + f"{s // 60} мин", callback_data=f"thd:{s}") for s in (60, 300, 900)])
    rows.append([BACK])
    return ("📏 <b>Пороги</b>\nАлерт, если значение выше порога дольше выбранного времени.",
            InlineKeyboardMarkup(rows))


def interval_view() -> tuple[str, InlineKeyboardMarkup]:
    cur = CFG.get("check_interval_sec", 60)
    opts = ((30, "30 с"), (60, "1 мин"), (300, "5 мин"))
    return ("⏱ <b>Как часто проверять сервер</b>",
            kb([Btn(("• " if cur == s else "") + t, callback_data=f"int:{s}") for s, t in opts], [BACK]))


def watch_view() -> tuple[str, InlineKeyboardMarkup]:
    rows = []
    for svc in CFG.get("services") or []:
        rows.append([Btn(f"✅ служба {svc}", callback_data=f"tg:services:{svc}"[:64])])
    for c in CFG.get("containers") or []:
        rows.append([Btn(f"✅ контейнер {c}", callback_data=f"tg:containers:{c}"[:64])])
    for p in CFG.get("processes") or []:
        rows.append([Btn(f"✅ процесс {p}", callback_data=f"tg:processes:{p}"[:64])])
    rows.append([Btn("➕ Служба", callback_data="add:services"), Btn("➕ Контейнер", callback_data="add:containers")])
    rows.append([BACK])
    return ("👁 <b>Что отслеживать</b>\nНажмите на элемент, чтобы перестать его отслеживать.",
            InlineKeyboardMarkup(rows))


async def add_view(kind: str) -> tuple[str, InlineKeyboardMarkup]:
    if kind == "services":
        found = await asyncio.to_thread(checks.running_services)
        title = "службы"
    else:
        states = await asyncio.to_thread(checks.container_states)
        found = sorted(states or {})
        title = "контейнеры"
    current = set(CFG.get(kind) or [])
    btns = [Btn(f"{onoff(n in current)} {n}", callback_data=f"tg:{kind}:{n}:add"[:64])
            for n in found if len(f"tg:{kind}:{n}:add") <= 64]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([Btn("⬅️ Назад", callback_data="set:watch")])
    text = f"➕ <b>Работающие {title} на сервере</b>\nНажмите, чтобы включить или выключить отслеживание."
    if not found:
        text += "\n\nНичего не найдено (нет доступа или список пуст)."
    return text, InlineKeyboardMarkup(rows)


QUIET_OPTS = (None, ("23:00", "08:00"), ("00:00", "07:00"), ("22:00", "09:00"))


def quiet_view() -> tuple[str, InlineKeyboardMarkup]:
    q = CFG.get("quiet_hours")
    cur = (q["start"], q["end"]) if q else None
    btns = [Btn(("• " if cur == o else "") + (f"{o[0]}–{o[1]}" if o else "Выкл"),
                callback_data=f"quiet:{o[0]}-{o[1]}" if o else "quiet:off") for o in QUIET_OPTS]
    return ("🌙 <b>Тихие часы</b>\nВ это время приходят только критичные алерты: упала служба, контейнер "
            "или процесс. Остальные придут утром, если проблема не исчезнет.",
            kb(btns[:2], btns[2:], [BACK]))


def remind_view() -> tuple[str, InlineKeyboardMarkup]:
    cur = CFG.get("remind_min") or 0
    btns = [Btn(("• " if cur == m else "") + (f"{m} мин" if m else "Выкл"), callback_data=f"remind:{m}")
            for m in (0, 30, 60, 180)]
    return ("⏰ <b>Напоминания</b>\nПовторять алерт, пока проблема не решена.", kb(btns, [BACK]))


def summary_view() -> tuple[str, InlineKeyboardMarkup]:
    cur = CFG.get("daily_summary")
    btns = [Btn(("• " if cur == t else "") + (t or "Выкл"), callback_data=f"summary:{t or 'off'}")
            for t in (None, "09:00", "20:00")]
    return ("📋 <b>Ежедневная сводка</b>\nСостояние сервера и алерты за сутки.", kb(btns, [BACK]))


def docker_view() -> tuple[str, InlineKeyboardMarkup]:
    auto = bool(CFG.get("containers_auto"))
    logs_on = CFG.get("log_alerts", True)
    every = CFG.get("log_alert_min") or 15
    rows = [[Btn(f"{onoff(auto)} Отслеживать все контейнеры", callback_data="dk:auto")],
            [Btn(f"{onoff(logs_on)} Алерты на ошибки в логах", callback_data="dk:logs")]]
    if logs_on:
        rows.append([Btn(("• " if every == m else "") + f"не чаще 1 раза в {m} мин", callback_data=f"dk:every:{m}")
                     for m in (15, 60)])
    rows.append([BACK])
    return ("🐳 <b>Docker</b>\nВсе контейнеры: бот сам добавляет каждый контейнер, который видел работающим, "
            "и пишет, если он остановился или перезапустился.\n"
            "Ошибки в логах: строки с Traceback, ERROR, CRITICAL, Exception, FATAL.",
            InlineKeyboardMarkup(rows))


def security_view() -> tuple[str, InlineKeyboardMarkup]:
    thr = CFG.get("ssh_fail_threshold", 30)
    rows = [[Btn(f"{onoff(CFG.get('ssh_alerts', True))} Входы по SSH и подбор пароля", callback_data="sec:ssh_alerts")],
            [Btn(f"{onoff(CFG.get('ssh_alert_all_logins'))} Сообщать о каждом входе", callback_data="sec:ssh_alert_all_logins")],
            [Btn(("• " if thr == n else "") + f"≥{n} попыток", callback_data=f"secthr:{n}") for n in (30, 100, 300)],
            [Btn(f"{onoff(CFG.get('port_alerts', True))} Новые открытые порты", callback_data="sec:port_alerts")],
            [Btn(f"{onoff(CFG.get('update_alerts', True))} Обновления безопасности", callback_data="sec:update_alerts")],
            [Btn(f"🧽 Забыть известные IP ({len(STATE.known_ips)})", callback_data="secforget")],
            [BACK]]
    hb = CFG.get("heartbeat_url")
    hb_text = ("не настроен" if not hb else
               "работает" if STATE.heartbeat_ok else "не доходит" if STATE.heartbeat_ok is False else "ждёт первой проверки")
    text = ("🛡 <b>Безопасность</b>\n"
            f"Известные IP для входа по SSH: {html.escape(', '.join(STATE.known_ips) or 'пока нет')}\n"
            f"Открытые наружу порты: {html.escape(', '.join(STATE.known_ports) or 'ещё не проверены')}\n"
            f"Внешний контроль (heartbeat_url в config.yaml): {hb_text}\n"
            "Подбор пароля: алерт, если неудачных попыток за 10 минут больше порога, не чаще раза в час.")
    return text, InlineKeyboardMarkup(rows)


probe_id = short_id

LATEST: dict = {}  # последняя проверка GitHub: {"sha", "message", "date", "changes", "checked", "error"}


def update_repo() -> str:
    return str(CFG.get("update_repo") or updater.DEFAULT_REPO)


def check_updates() -> None:
    cur = updater.current().get("sha")
    try:
        info = updater.latest(update_repo())
        info["changes"] = updater.changes(update_repo(), cur, info["sha"]) if cur and cur != info["sha"] else []
        info["error"] = None
    except Exception as e:  # noqa: BLE001
        info = {"error": str(e)[:200]}
    info["checked"] = time.time()
    LATEST.clear()
    LATEST.update(info)


def update_view() -> tuple[str, InlineKeyboardMarkup]:
    cur = updater.current()
    lines = ["⬆️ <b>Обновление бота</b>",
             f"Сейчас: {updater.short(cur.get('sha'))}"
             + (f", установлена {updater.age_text(cur.get('installed'))}" if cur.get("installed") else "")]
    rows = [[Btn("🔍 Проверить обновления", callback_data="up:check")]]
    if LATEST.get("error"):
        lines.append(f"Не удалось проверить GitHub: {html.escape(LATEST['error'])}")
    elif LATEST.get("sha"):
        if LATEST["sha"] == cur.get("sha"):
            lines.append("✅ Установлена последняя версия")
        else:
            lines.append(f"Доступна версия {LATEST['sha'][:7]}: {html.escape(LATEST['message'])}")
            for c in LATEST.get("changes") or []:
                lines.append(f"  • {html.escape(c)}")
            if actions_on():
                rows.append([Btn(f"⬆️ Обновить до {LATEST['sha'][:7]}", callback_data=f"up:go:{LATEST['sha']}")])
    lines.append("\nПеред обновлением бот прогоняет тесты новой версии и сохраняет копию текущей. Если новая версия "
                 "не запустится, бот сам вернёт прежнюю. Помощник с правами root, sudoers и файл службы так не "
                 "обновляются, это делается вручную.")
    if actions_on():
        rows.append([Btn("🧽 Удалить старые копии кода", callback_data="up:prune")])
    rows.append([BACK])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def report_update(bot) -> None:
    """Сообщить результат обновления один раз (файл пишет помощник)."""
    res = await asyncio.to_thread(updater.last_result)
    if not res or res.get("finished") == STATE.update_reported:
        return
    STATE.update_reported = res.get("finished")
    STATE.save()
    await broadcast(bot, f"<b>{html.escape(HOST)}</b>\n{html.escape(updater.result_text(res))}")


BACKUP_TIMES = (None, "03:45", "05:00")


def backup_kb() -> InlineKeyboardMarkup:
    t = CFG.get("backup_time")
    rows = [[Btn("💾 Сделать копию бота сейчас", callback_data="bk:make")],
            [Btn("📤 Прислать копию бота", callback_data="bk:send:own")]]
    for i, st in enumerate(backups.watched_status() or []):
        name = Path(st["path"]).name or st["path"]
        rows.append([Btn(f"📤 Прислать {name[:30]}", callback_data=f"bk:send:{i}")])
    rows.append([Btn(("• " if t == o else "") + (o or "Не делать"), callback_data=f"bk:time:{o or 'off'}")
                 for o in BACKUP_TIMES])
    rows.append([Btn(f"{onoff(CFG.get('backup_send_daily'))} Каждый день присылать копии сюда",
                     callback_data="bk:daily")])
    rows.append([Btn(f"{onoff(CFG.get('backup_alerts', True))} Алерт, если бэкап устарел", callback_data="bk:alerts")])
    rows.append([Btn("🔄 Обновить", callback_data="bk:refresh"), BACK])
    return InlineKeyboardMarkup(rows)


def backup_view() -> tuple[str, InlineKeyboardMarkup]:
    text = backups.status_text(CFG, S.dir) + (
        "\n\nКопия бота: config.yaml (с токеном), настройки, состояние алертов, история метрик. "
        "Лежит на этом же сервере, поэтому полезно присылать её сюда. Не пересылайте её никому.")
    return text, backup_kb()


async def send_file(bot, chat_id: int, path: Path, caption: str) -> None:
    with open(path, "rb") as f:
        await bot.send_document(chat_id, document=f, filename=path.name, caption=caption)


async def send_backups(bot, chat_ids: list[int], own: Path | None = None, watched: bool = True) -> list[str]:
    """Отправить копию бота и последние чужие копии. Возвращает список ошибок."""
    errors = []
    if own is None:
        files = backups.own_files(S.dir)
        own = backups.own_dir(S.dir) / files[0][0] if files else None
    if own is not None:
        for c in chat_ids:
            await send_file(bot, c, own, f"💾 Копия бота {HOST}. Внутри токен, не пересылайте.")
    if watched:
        for i, st in enumerate(await asyncio.to_thread(backups.watched_status) or []):
            path, err = await asyncio.to_thread(backups.fetch, i)
            if path is None:
                errors.append(f"{st['path']}: {err}")
                continue
            for c in chat_ids:
                await send_file(bot, c, path, f"💾 {st['path']} на {HOST}")
    return errors


def smart_view() -> tuple[str, InlineKeyboardMarkup]:
    rows = [[Btn(f"{onoff(CFG.get('anomaly_alerts', True))} Необычная нагрузка для этого часа",
                 callback_data="sm:anomaly_alerts")],
            [Btn(f"{onoff(CFG.get('ram_trend_alerts', True))} Память растёт и скоро закончится",
                 callback_data="sm:ram_trend_alerts")],
            [Btn(f"{onoff(CFG.get('group_alerts', True))} Много алертов сразу — одним сообщением",
                 callback_data="sm:group_alerts")],
            [BACK]]
    text = ("🧠 <b>Умные алерты</b>\n"
            "Необычная нагрузка: бот знает, сколько CPU и RAM обычно бывает в этот час (по истории за 14 дней), "
            "и пишет, если 15 минут подряд намного больше обычного, даже когда до порога далеко. "
            "Начинает работать, когда накопится 3 дня истории.\n"
            "Память растёт: если RAM ровно растёт 6 часов и при таком темпе закончится меньше чем через 12 часов "
            "(похоже на утечку). Не чаще раза в 6 часов.\n"
            "Под каждым алертом: «✔️ Принял» — без напоминаний, пока проблема не исчезнет; "
            "«💤 Этот на 1 ч» — молчать только об этой проблеме.")
    return text, InlineKeyboardMarkup(rows)


PROBES_HELP = ("Добавить: <code>/probe https://site.ru</code>\n"
               "Текст на странице: <code>/probe https://site.ru/health | OK</code>\n"
               "Порт: <code>/probe tcp://host:5432</code>, ping: <code>/probe ping://1.1.1.1</code>, "
               "DNS: <code>/probe dns://site.ru</code>")


def probes_view() -> tuple[str, InlineKeyboardMarkup]:
    raws = list(CFG.get("probes") or [])
    fails = int(CFG.get("probe_fail_count") or 2)
    ssl_on = CFG.get("ssl_alerts", True)
    rows = [[Btn(f"🗑 {r[:40]}", callback_data=f"pr:del:{probe_id(r)}")] for r in raws]
    if raws:
        rows.append([Btn("▶️ Проверить сейчас", callback_data="pr:run")])
    rows.append([Btn(("• " if fails == n else "") + f"алерт после {n} неудач", callback_data=f"pr:fails:{n}")
                 for n in (1, 2, 3)])
    rows.append([Btn(f"{onoff(ssl_on)} Предупреждать об окончании SSL", callback_data="pr:ssl")])
    rows.append([BACK])
    body = probes.probes_text(CFG, STATE) if raws else "🌐 <b>Проверки снаружи</b>\nПока ничего не проверяется."
    text = (f"{body}\n\nБот проверяет цели каждые {CFG.get('check_interval_sec', 60)} с. Падение сайта приходит "
            "и в тихие часы. SSL: предупреждения за 14, 7, 3 и 1 день до окончания.\n"
            f"Нажмите на цель, чтобы удалить её.\n\n{PROBES_HELP}")
    return text, InlineKeyboardMarkup(rows)


def run_probes_now() -> None:
    raws = list(CFG.get("probes") or [])
    now = time.time()
    for raw, (ok, detail, ms) in probes.run_all(raws, float(CFG.get("probe_timeout") or 10)).items():
        STATE.probe_last[raw] = [ok, detail, ms, now]
    for host, port in probes.https_hosts(raws):
        days = probes.ssl_days_left(host, port)
        if days is not None:
            k = probes.host_key(host, port)
            STATE.ssl_days[k] = days
            STATE.ssl_checked[k] = now
    STATE.save()


# ---------- Доступ и отправка ----------

def allowed(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat) and chat.id in CFG["allowed_chats"]


async def send(update: Update, text: str, markup=None) -> None:
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def edit(update: Update, text: str, markup=None) -> None:
    try:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


# ---------- Команды и кнопки клавиатуры ----------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        # Подсказка для первичной настройки: показываем chat_id, больше ничего
        await send(update, f"Доступ закрыт. Ваш chat_id: <code>{update.effective_chat.id}</code>\n"
                           "Добавьте его в allowed_chats в config.yaml и перезапустите бота.")
        return
    await update.effective_message.reply_text(
        "👋 Бот мониторинга сервера. Управляйте кнопками внизу или командами:\n"
        "/status /procs /mem /services /alerts /settings /logs /chart /actions /probe /backup\n"
        "/mute 2h — выключить уведомления, /unmute — включить", reply_markup=MAIN_KB)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await send(update, await asyncio.to_thread(checks.status_text, CFG), status_kb())


async def cmd_procs(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await send(update, await asyncio.to_thread(checks.top_processes, 10, "cpu"), procs_kb("cpu", 10))


async def cmd_mem(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await send(update, await asyncio.to_thread(checks.top_processes, 10, "mem"), procs_kb("mem", 10))


async def cmd_services(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await send(update, await asyncio.to_thread(checks.services_text, CFG, STATE), services_kb())


async def cmd_alerts(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await send(update, alerts_text(), alerts_kb())


async def cmd_logs(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    names = checks.watched_containers(CFG, STATE)
    if not ctx.args:
        btns = [b for b in (logs_btn(c) for c in names) if b]
        await send(update, "Чьи логи показать? Или /logs имя_контейнера",
                   kb(*[btns[i:i + 2] for i in range(0, len(btns), 2)]) if btns else None)
        return
    name = ctx.args[0]
    tail = int(ctx.args[1]) if len(ctx.args) > 1 and ctx.args[1].isdigit() else 30
    tail = max(1, min(tail, 300))
    await send(update, await asyncio.to_thread(checks.logs_text, name, tail), logs_kb(name, 30 if tail == 30 else 100))


async def cmd_chart(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        period = ctx.args[0] if ctx.args and ctx.args[0] in history.PERIODS else "24h"
        await send_chart(update, period)


async def cmd_actions(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    if not actions_on():
        await send(update, "Действия выключены (actions_enabled: false в config.yaml)")
        return
    await send(update, *actions_menu())


async def cmd_probe(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    arg = " ".join(ctx.args or []).strip()
    if not arg:
        await send(update, *probes_view())
        return
    norm = probes.normalize(arg)
    if not norm:
        await send(update, f"Не понял цель «{html.escape(arg)}».\n\n{PROBES_HELP}")
        return
    raws = list(CFG.get("probes") or [])
    if norm in raws:
        await send(update, f"Уже проверяю: {html.escape(norm)}", None)
        return
    if len(raws) >= 30:
        await send(update, "Максимум 30 целей. Удалите лишние в /probe")
        return
    S.set("probes", raws + [norm])
    await asyncio.to_thread(run_probes_now)
    await send(update, f"➕ Добавлено: {html.escape(norm)}", None)
    await send(update, *probes_view())


async def cmd_backup(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await send(update, *(await asyncio.to_thread(backup_view)))


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await send(update, settings_text(), settings_kb())


def parse_duration(s: str) -> int | None:
    s = s.strip().lower()
    mult = {"m": 60, "м": 60, "h": 3600, "ч": 3600, "d": 86400, "д": 86400}
    try:
        if s[-1] in mult:
            return int(float(s[:-1]) * mult[s[-1]])
        return int(float(s) * 60)
    except (ValueError, IndexError):
        return None


async def cmd_mute(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    sec = parse_duration(ctx.args[0]) if ctx.args else 3600
    if not sec or sec <= 0:
        await send(update, "Формат: /mute 30m, /mute 2h, /mute 1d")
        return
    await send(update, set_mute(time.time() + sec))


async def cmd_unmute(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await send(update, set_mute(0))


def set_mute(until: float) -> str:
    STATE.mute_until = until
    STATE.save()
    if until <= time.time():
        return "🔔 Уведомления включены"
    return f"🔕 Уведомления выключены до {checks.fmt_local(CFG, until)}"


TEXT_BUTTONS = {B_STATUS: cmd_status, B_PROCS: cmd_procs, B_SERVICES: cmd_services,
                B_ALERTS: cmd_alerts, B_SETTINGS: cmd_settings}


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    fn = TEXT_BUTTONS.get((update.effective_message.text or "").strip())
    if fn:
        await fn(update, ctx)


# ---------- Inline-кнопки ----------

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not allowed(update):
        await q.answer("Доступ закрыт", show_alert=True)
        return
    data = q.data or ""
    parts = data.split(":")
    cmd = parts[0]
    note = None
    answered = False
    try:
        if cmd == "noop":
            pass
        elif cmd == "status":
            text = await asyncio.to_thread(checks.status_text, CFG)
            if len(parts) > 1:  # из алерта: новое сообщение, алерт не трогаем
                await send(update, text, status_kb())
            else:
                await edit(update, text, status_kb())
        elif cmd == "procs":
            by, n = parts[1], int(parts[2])
            await edit(update, await asyncio.to_thread(checks.top_processes, n, by), procs_kb(by, n))
        elif cmd == "services":
            await edit(update, await asyncio.to_thread(checks.services_text, CFG, STATE), services_kb())
        elif cmd == "alerts":
            if len(parts) > 1:
                await send(update, alerts_text(), alerts_kb())
            else:
                await edit(update, alerts_text(), alerts_kb())
        elif cmd == "ack":
            key = active_key(parts[1])
            if key is None:
                note = "Эта проблема уже решена"
            else:
                if key not in STATE.acked:
                    STATE.acked.append(key)
                    STATE.save()
                note = "Принято: напоминаний не будет, пока проблема не исчезнет"
                try:
                    await q.edit_message_reply_markup(alert_kb(key))
                except BadRequest:
                    pass
        elif cmd == "ackall":
            STATE.acked = list(STATE.active)
            STATE.save()
            note = f"Принято: {len(STATE.acked)}" if STATE.acked else "Активных алертов нет"
        elif cmd == "snz":
            key = active_key(parts[1])
            if key is None:
                note = "Эта проблема уже решена"
            else:
                STATE.snoozed[key] = time.time() + 3600
                STATE.save()
                note = f"Об этой проблеме молчу до {checks.fmt_local(CFG, STATE.snoozed[key])}"
        elif cmd == "sm":
            S.set(parts[1], not CFG.get(parts[1], True))
            await edit(update, *smart_view())
        elif cmd == "mute":
            arg = parts[1]
            if arg == "off":
                note = set_mute(0)
            elif arg == "morning":
                note = set_mute(checks.next_morning(CFG, time.time()))
            else:
                note = set_mute(time.time() + int(arg))
            if len(parts) > 2:
                await send(update, note)  # под алертом: подтверждение отдельным сообщением
            else:
                await edit(update, alerts_text(), alerts_kb())
        elif cmd == "set":
            sub = parts[1] if len(parts) > 1 else ""
            if sub == "reset":
                S.reset()
                reschedule(ctx.application)
                note = "Настройки сброшены к config.yaml"
                await edit(update, settings_text(), settings_kb())
            elif sub == "":
                await edit(update, settings_text(), settings_kb())
            else:
                view = {"th": thresholds_view, "int": interval_view, "watch": watch_view,
                        "quiet": quiet_view, "remind": remind_view, "summary": summary_view,
                        "docker": docker_view, "sec": security_view, "probes": probes_view,
                        "smart": smart_view, "backup": backup_view,
                        "update": update_view}[sub]
                await edit(update, *view())
        elif cmd == "th":
            key, delta = parts[1], int(parts[2])
            val = max(5, min(100, int(CFG["thresholds"].get(key) or 90) + delta))
            S.set(f"thresholds.{key}", val)
            await edit(update, *thresholds_view())
        elif cmd == "thd":
            S.set("thresholds.duration_sec", int(parts[1]))
            await edit(update, *thresholds_view())
        elif cmd == "int":
            S.set("check_interval_sec", int(parts[1]))
            reschedule(ctx.application)
            await edit(update, *interval_view())
        elif cmd == "add":
            await edit(update, *(await add_view(parts[1])))
        elif cmd == "tg":
            kind, name = parts[1], parts[2]
            on = S.toggle_item(kind, name)
            note = ("Отслеживаю: " if on else "Не отслеживаю: ") + name
            if len(parts) > 3:
                await edit(update, *(await add_view(kind)))
            else:
                await edit(update, *watch_view())
        elif cmd == "quiet":
            if parts[1] == "off":
                S.set("quiet_hours", None)
            else:
                start, end = ":".join(parts[1:]).split("-")
                S.set("quiet_hours", {"start": start, "end": end})
            await edit(update, *quiet_view())
        elif cmd == "dk":
            if parts[1] == "auto":
                S.set("containers_auto", not CFG.get("containers_auto"))
            elif parts[1] == "logs":
                S.set("log_alerts", not CFG.get("log_alerts", True))
            elif parts[1] == "every":
                S.set("log_alert_min", int(parts[2]))
            await edit(update, *docker_view())
        elif cmd == "chart":
            period = parts[1] if parts[1] in history.PERIODS else "24h"
            if len(parts) > 2:  # переключение периода под уже отправленным графиком
                png, caption = await make_chart(period)
                if png is None:
                    note = "Данных за этот период пока нет"
                else:
                    await q.edit_message_media(InputMediaPhoto(png, caption=caption), reply_markup=chart_kb(period))
            else:
                await send_chart(update, period)
        elif cmd == "logs":
            name = parts[1]
            tail = int(parts[2]) if len(parts) > 2 else 30
            text = await asyncio.to_thread(checks.logs_text, name, tail)
            if len(parts) > 2:
                await edit(update, text, logs_kb(name, tail))
            else:
                await send(update, text, logs_kb(name, tail))
        elif cmd == "sec":
            key = parts[1]
            default = key != "ssh_alert_all_logins"
            S.set(key, not CFG.get(key, default))
            await edit(update, *security_view())
        elif cmd == "secthr":
            S.set("ssh_fail_threshold", int(parts[1]))
            await edit(update, *security_view())
        elif cmd == "secforget":
            STATE.known_ips = []
            STATE.save()
            note = "Следующий вход с любого IP придёт как новый"
            await edit(update, *security_view())
        elif cmd == "pr":
            sub = parts[1]
            if sub == "del":
                raws = list(CFG.get("probes") or [])
                gone = [r for r in raws if probe_id(r) == parts[2]]
                if gone:
                    S.set("probes", [r for r in raws if r not in gone])
                    note = "Удалено: " + gone[0][:150]
                else:
                    note = "Уже удалено"
            elif sub == "run":
                await asyncio.to_thread(run_probes_now)
                note = "Проверено"
            elif sub == "fails":
                S.set("probe_fail_count", int(parts[2]))
                note = "Сохранено"
            elif sub == "ssl":
                S.set("ssl_alerts", not CFG.get("ssl_alerts", True))
                note = "Сохранено"
            await edit(update, *probes_view())
        elif cmd == "bk":
            sub = parts[1]
            if sub == "make":
                await q.answer("Делаю копию…")
                answered = True
                path, size, removed = await asyncio.to_thread(
                    backups.make_backup, CONFIG_PATH, S.dir, int(CFG.get("backup_keep") or 14))
                await send(update, f"💾 Готово: {html.escape(path.name)}, {checks.fmt_bytes(size)}"
                                   + (f", удалено старых: {removed}" if removed else ""))
                await edit(update, *backup_view())
            elif sub == "send":
                await q.answer("Отправляю…")
                answered = True
                chat = [update.effective_chat.id]
                if parts[2] == "own":
                    files = backups.own_files(S.dir)
                    if not files:
                        await send(update, "Копий бота ещё нет. Нажмите «Сделать копию бота сейчас».")
                    else:
                        await send_backups(ctx.bot, chat, watched=False)
                else:
                    i = int(parts[2])
                    path, err = await asyncio.to_thread(backups.fetch, i)
                    if path is None:
                        await send(update, f"Не удалось: {html.escape(err)}")
                    else:
                        await send_file(ctx.bot, chat[0], path, f"💾 {path.name} с {HOST}")
            else:
                if sub == "time":
                    S.set("backup_time", None if parts[2] == "off" else ":".join(parts[2:]))
                    reschedule(ctx.application)
                elif sub == "daily":
                    S.set("backup_send_daily", not CFG.get("backup_send_daily"))
                elif sub == "alerts":
                    S.set("backup_alerts", not CFG.get("backup_alerts", True))
                elif sub == "refresh":
                    await asyncio.to_thread(backups.watched_status, None, True)
                note = "Обновлено" if sub == "refresh" else "Сохранено"
                await edit(update, *backup_view())
        elif cmd == "up":
            sub = parts[1]
            if sub == "check":
                await asyncio.to_thread(check_updates)
                await edit(update, *update_view())
                note = "Не удалось проверить" if LATEST.get("error") else "Проверено"
            elif not actions_on():
                note = "Действия выключены в config.yaml"
            elif sub == "go":
                sha = parts[2]
                if not updater.SHA_RE.match(sha):
                    note = "Неверная версия"
                else:
                    token = ACT.ask("update", sha, update.effective_chat.id)
                    changes = "".join(f"\n  • {html.escape(c)}" for c in LATEST.get("changes") or [])
                    await send(update, f"⬆️ Обновить бота до <b>{sha[:7]}</b>?{changes}\n\n"
                                       "Сначала тесты, потом перезапуск бота (около минуты без ответа). "
                                       "Если что-то пойдёт не так, вернётся прежняя версия.", confirm_kb(token))
            elif sub == "prune":
                ok, out = await asyncio.to_thread(ACT._helper, "bak-list")
                try:
                    baks = json.loads(out) if ok else None
                except ValueError:
                    baks = None
                if baks is None:
                    await send(update, f"Не удалось получить список: {html.escape(out)}")
                elif len(baks) <= 2:
                    note = f"Старых копий: {len(baks)}, удалять нечего"
                else:
                    extra = baks[2:]
                    size = sum(b["size"] for b in extra)
                    token = ACT.ask("prunebak", "2", update.effective_chat.id)
                    names = "\n".join(f"  • {html.escape(Path(b['path']).name)} ({checks.fmt_bytes(b['size'])})"
                                      for b in extra)
                    await send(update, f"🧽 Удалить {len(extra)} старых копий кода и освободить "
                                       f"{checks.fmt_bytes(size)}? Две самые новые останутся.\n{names}",
                               confirm_kb(token))
        elif cmd == "act":
            if not actions_on():
                note = "Действия выключены в config.yaml"
            else:
                await handle_act(update, parts)
        elif cmd in ("do", "no"):
            note = await handle_confirm(update, cmd, parts[1])
        elif cmd == "remind":
            S.set("remind_min", int(parts[1]))
            await edit(update, *remind_view())
        elif cmd == "summary":
            S.set("daily_summary", None if parts[1] == "off" else ":".join(parts[1:]))
            reschedule(ctx.application)
            await edit(update, *summary_view())
        if cmd in ("th", "thd", "int", "tg", "quiet", "remind", "summary", "dk", "sec", "secthr", "sm", "bk") and note is None:
            note = "Сохранено"
    except Exception:
        log.exception("Ошибка кнопки %s", data)
        note = "Ошибка, подробности в журнале бота"
        if answered:
            await send(update, "❌ " + note)
    if not answered:
        await q.answer(note or None)


# ---------- Действия ----------

async def handle_act(update: Update, parts: list[str]) -> None:
    chat_id = update.effective_chat.id
    sub = parts[1]
    if sub == "menu":
        await edit(update, *actions_menu())
    elif sub == "log":
        await send(update, actions_log_text())
    elif sub == "killmenu":
        rows = await asyncio.to_thread(actions.top_for_kill)
        btns = []
        for pid, name, cpu, _ in rows:
            btns.append([Btn(f"⛔ {name[:20]} (PID {pid}, CPU {cpu:.0f}%)", callback_data=f"act:kill:{pid}")])
        await send(update, "⛔ <b>Какой процесс остановить?</b> (SIGTERM, процесс может завершиться не сразу)",
                   InlineKeyboardMarkup(btns) if btns else None)
    elif sub == "ctr":
        name = parts[2]
        token = ACT.ask("ctr", name, chat_id)
        await send(update, f"♻️ Перезапустить контейнер <b>{html.escape(name)}</b>?\n"
                           "Бот внутри будет недоступен несколько секунд.", confirm_kb(token))
    elif sub == "svc":
        name = parts[2]
        if name not in action_services():
            await send(update, f"Служба {html.escape(name)} не входит в action_services в config.yaml")
            return
        token = ACT.ask("svc", name, chat_id)
        await send(update, f"♻️ Перезапустить службу <b>{html.escape(name)}</b>?", confirm_kb(token))
    elif sub == "kill":
        pid = int(parts[2])
        try:
            p = await asyncio.to_thread(lambda: actions.psutil.Process(pid))
            name, created = p.name(), p.create_time()
            cmdline = " ".join(p.cmdline())[:150]
        except Exception:
            await send(update, "Процесс уже завершился")
            return
        token = ACT.ask("kill", str(pid), chat_id, name=name, created=created)
        await send(update, f"⛔ Остановить процесс <b>{html.escape(name)}</b> (PID {pid})?\n"
                           f"<code>{html.escape(cmdline)}</code>", confirm_kb(token))
    elif sub == "clean":
        est = await asyncio.to_thread(ACT.cleanup_estimate)
        token = ACT.ask("clean", "", chat_id)
        await send(update, "🧹 <b>Очистить место?</b>\nУдалятся неиспользуемые Docker-образы и кэш сборки, "
                           "журнал systemd сократится до 200 МБ, очистится кэш apt. Контейнеры и их данные "
                           f"не трогаются.\n\nСколько можно освободить:\n<pre>{html.escape(est)}</pre>",
                   confirm_kb(token))
    elif sub == "reboot":
        token = ACT.ask("reboot", "", chat_id)
        await send(update, "🔄 <b>Перезагрузить сервер?</b>\nСервер и все контейнеры будут недоступны 1–2 минуты.",
                   confirm_kb(token, "Да, перезагрузить"))


async def handle_confirm(update: Update, cmd: str, token: str) -> str | None:
    chat_id = update.effective_chat.id
    p = ACT.take(token, chat_id)
    if p is None:
        await edit(update, "⌛ Подтверждение устарело, начните заново")
        return None
    if cmd == "no":
        ACT.drop(token)
        await edit(update, f"✖️ Отменено: {actions.describe(p)}")
        return "Отменено"
    if p.kind == "reboot" and p.stage == 1:
        p.stage = 2
        p.expires = time.time() + 60
        await edit(update, "⚠️ <b>Точно перезагрузить сервер?</b> Это второе и последнее подтверждение.",
                   confirm_kb(token, "🔄 Перезагрузить сейчас"))
        return None
    ACT.drop(token)
    await edit(update, f"⏳ Выполняю: {actions.describe(p)}…")
    ok, out = await asyncio.to_thread(ACT.execute, p)
    if p.kind == "update" and ok:
        await edit(update, f"⏳ Обновление до {p.arg[:7]} запущено. Сначала тесты новой версии, потом перезапуск. "
                           "Результат пришлю через 1–3 минуты.")
        return None
    out = out.strip()[-1500:] or ("готово" if ok else "ошибка без вывода")
    icon = "✅" if ok else "❌"
    await edit(update, f"{icon} {html.escape(actions.describe(p))}\n<pre>{html.escape(out)}</pre>")
    return None


# ---------- Фоновые задачи ----------

async def broadcast(bot, text: str, markup=None) -> None:
    for chat_id in CFG["allowed_chats"]:
        try:
            await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception:
            log.exception("Не удалось отправить сообщение в %s", chat_id)


async def monitor_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        alerts = await asyncio.to_thread(checks.step, CFG, STATE, None, None, HIST)
    except Exception:
        log.exception("Ошибка проверки")
        return
    try:
        await asyncio.to_thread(HIST.record, checks.LAST_SAMPLE)
    except Exception:
        log.exception("Не удалось записать историю")
    if CFG.get("heartbeat_url"):
        ok = await asyncio.to_thread(security.heartbeat, CFG["heartbeat_url"])
        if ok != STATE.heartbeat_ok:
            log.info("Сигнал во внешний сервис: %s", "ok" if ok else "не доходит")
            STATE.heartbeat_ok = ok
    try:
        await report_update(ctx.bot)
    except Exception:
        log.exception("Не удалось сообщить результат обновления")
    if CFG.get("group_alerts", True) and len(alerts) > 3:
        await broadcast(ctx.bot, group_text(alerts), group_kb())
        return
    for a in alerts:
        markup = alert_kb(a.key, a.kind in ("alert", "remind")) if a.kind in ("alert", "remind") or (a.key or "").startswith("log:") else None
        await broadcast(ctx.bot, f"<b>{html.escape(HOST)}</b>\n{html.escape(a.text)}", markup)


async def backup_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        path, size, _ = await asyncio.to_thread(backups.make_backup, CONFIG_PATH, S.dir,
                                                int(CFG.get("backup_keep") or 14))
        log.info("Копия бота: %s (%s байт)", path.name, size)
    except Exception as e:
        log.exception("Не удалось сделать копию бота")
        await broadcast(ctx.bot, f"<b>{html.escape(HOST)}</b>\n❌ Не удалось сделать копию бота: {html.escape(str(e))}")
        return
    if CFG.get("backup_send_daily"):
        try:
            errors = await send_backups(ctx.bot, CFG["allowed_chats"], own=path)
        except Exception as e:
            log.exception("Не удалось отправить копии")
            errors = [str(e)]
        if errors:
            await broadcast(ctx.bot, "❌ Не все копии удалось отправить:\n" + html.escape("\n".join(errors)))


async def summary_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = await asyncio.to_thread(checks.summary_text, CFG, STATE, None, HIST)
    await broadcast(ctx.bot, text, status_kb())


def reschedule(app: Application) -> None:
    jq = app.job_queue
    for name in ("monitor", "summary", "backup"):
        for job in jq.get_jobs_by_name(name):
            job.schedule_removal()
    interval = int(CFG.get("check_interval_sec", 60))
    jq.run_repeating(monitor_job, interval=interval, first=10, name="monitor")
    if CFG.get("daily_summary"):
        h, m = map(int, CFG["daily_summary"].split(":"))
        jq.run_daily(summary_job, time=dt.time(h, m, tzinfo=checks.tz(CFG)), name="summary")
    if CFG.get("backup_time"):
        h, m = map(int, CFG["backup_time"].split(":"))
        jq.run_daily(backup_job, time=dt.time(h, m, tzinfo=checks.tz(CFG)), name="backup")
    log.info("Проверка каждые %s с, сводка: %s", interval, CFG.get("daily_summary") or "выкл")


async def on_startup(app: Application) -> None:
    reboot_msg = checks.check_reboot(STATE)
    parts = []
    if CFG.get("notify_on_start", True):
        parts.append(f"▶️ Мониторинг запущен на {html.escape(HOST)}")
    if reboot_msg:
        parts.append(reboot_msg)
    if parts:
        for chat_id in CFG["allowed_chats"]:
            try:
                await app.bot.send_message(chat_id, "\n".join(parts), reply_markup=MAIN_KB)
            except Exception:
                log.exception("Не удалось отправить сообщение о запуске")


def build_app() -> Application:
    app = Application.builder().token(CFG["token"]).post_init(on_startup).build()
    for name, fn in [("start", cmd_start), ("help", cmd_start), ("menu", cmd_start), ("status", cmd_status),
                     ("procs", cmd_procs), ("mem", cmd_mem), ("services", cmd_services),
                     ("alerts", cmd_alerts), ("settings", cmd_settings), ("mute", cmd_mute), ("logs", cmd_logs), ("chart", cmd_chart), ("actions", cmd_actions),
                     ("probe", cmd_probe), ("backup", cmd_backup),
                     ("unmute", cmd_unmute)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    reschedule(app)
    return app


def main() -> None:
    build_app().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
