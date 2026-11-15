"""Telegram-бот мониторинга сервера.

Запуск:  python bot.py [путь_к_config.yaml]
Управление командами (/status, /procs, ...) или кнопками.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import logging
import sys
import time
from pathlib import Path

from telegram import (InlineKeyboardButton as Btn, InlineKeyboardMarkup, KeyboardButton,
                      ReplyKeyboardMarkup, Update)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

import checks
from settings import Settings

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("monitor_bot")

CONFIG_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("config.yaml")
S = Settings(CONFIG_PATH)
CFG = S.cfg  # общий словарь, обновляется на месте при изменении настроек
STATE = checks.AlertState.load(S.dir / "state.json")
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
    return kb([Btn("🔄 Обновить", callback_data="status")],
              [Btn("🔝 Процессы", callback_data="procs:cpu:10"), Btn("⚙️ Службы", callback_data="services")])


def procs_kb(by: str, n: int) -> InlineKeyboardMarkup:
    other = "mem" if by == "cpu" else "cpu"
    return kb([Btn("🔄 Обновить", callback_data=f"procs:{by}:{n}"),
               Btn("По RAM" if other == "mem" else "По CPU", callback_data=f"procs:{other}:{n}")],
              [Btn("Показать 20" if n == 10 else "Показать 10", callback_data=f"procs:{by}:{20 if n == 10 else 10}")])


def logs_btn(name: str, label: str | None = None) -> Btn | None:
    data = f"logs:{name}"
    return Btn(label or f"📜 {name}", callback_data=data) if len(data.encode()) <= 60 else None


def services_kb() -> InlineKeyboardMarkup:
    logs = [b for b in (logs_btn(c) for c in checks.watched_containers(CFG, STATE)) if b]
    return kb([Btn("🔄 Обновить", callback_data="services"),
               Btn("✏️ Что отслеживать", callback_data="set:watch")],
              *[logs[i:i + 2] for i in range(0, len(logs), 2)])


def alert_kb(key: str | None = None) -> InlineKeyboardMarkup:
    extra = []
    if key and key.startswith(("ctr:", "log:")):
        b = logs_btn(checks.key_label(key), "📜 Логи")
        if b:
            extra.append(b)
    return kb([Btn("🔕 На 1 ч", callback_data="mute:3600:a"), Btn("🌙 До утра", callback_data="mute:morning:a")],
              [Btn("📊 Статус сейчас", callback_data="status:new")] + extra)


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
            lines.append(html.escape(STATE.problems.get(k, checks.key_label(k))) + mins)
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
        f"Часовой пояс: {CFG.get('tz')}",
    ])


def settings_kb() -> InlineKeyboardMarkup:
    return kb([Btn("📏 Пороги", callback_data="set:th"), Btn("⏱ Интервал", callback_data="set:int")],
              [Btn("👁 Что отслеживать", callback_data="set:watch")],
              [Btn("🌙 Тихие часы", callback_data="set:quiet"), Btn("⏰ Напоминания", callback_data="set:remind")],
              [Btn("📋 Ежедневная сводка", callback_data="set:summary"), Btn("🐳 Docker", callback_data="set:docker")],
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
        "/status /procs /mem /services /alerts /settings /logs\n"
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
            await edit(update, alerts_text(), alerts_kb())
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
                        "docker": docker_view}[sub]
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
        elif cmd == "logs":
            name = parts[1]
            tail = int(parts[2]) if len(parts) > 2 else 30
            text = await asyncio.to_thread(checks.logs_text, name, tail)
            if len(parts) > 2:
                await edit(update, text, logs_kb(name, tail))
            else:
                await send(update, text, logs_kb(name, tail))
        elif cmd == "remind":
            S.set("remind_min", int(parts[1]))
            await edit(update, *remind_view())
        elif cmd == "summary":
            S.set("daily_summary", None if parts[1] == "off" else ":".join(parts[1:]))
            reschedule(ctx.application)
            await edit(update, *summary_view())
        if cmd in ("th", "thd", "int", "tg", "quiet", "remind", "summary", "dk") and note is None:
            note = "Сохранено"
    except Exception:
        log.exception("Ошибка кнопки %s", data)
        note = "Ошибка, подробности в журнале бота"
    await q.answer(note or None)


# ---------- Фоновые задачи ----------

async def broadcast(bot, text: str, markup=None) -> None:
    for chat_id in CFG["allowed_chats"]:
        try:
            await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception:
            log.exception("Не удалось отправить сообщение в %s", chat_id)


async def monitor_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        alerts = await asyncio.to_thread(checks.step, CFG, STATE)
    except Exception:
        log.exception("Ошибка проверки")
        return
    for a in alerts:
        markup = alert_kb(a.key) if a.kind in ("alert", "remind") or (a.key or "").startswith("log:") else None
        await broadcast(ctx.bot, f"<b>{html.escape(HOST)}</b>\n{html.escape(a.text)}", markup)


async def summary_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = await asyncio.to_thread(checks.summary_text, CFG, STATE)
    await broadcast(ctx.bot, text, status_kb())


def reschedule(app: Application) -> None:
    jq = app.job_queue
    for name in ("monitor", "summary"):
        for job in jq.get_jobs_by_name(name):
            job.schedule_removal()
    interval = int(CFG.get("check_interval_sec", 60))
    jq.run_repeating(monitor_job, interval=interval, first=10, name="monitor")
    if CFG.get("daily_summary"):
        h, m = map(int, CFG["daily_summary"].split(":"))
        jq.run_daily(summary_job, time=dt.time(h, m, tzinfo=checks.tz(CFG)), name="summary")
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
                     ("alerts", cmd_alerts), ("settings", cmd_settings), ("mute", cmd_mute), ("logs", cmd_logs),
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
