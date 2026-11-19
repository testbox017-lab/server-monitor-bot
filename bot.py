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

from telegram import (BotCommand, InlineKeyboardButton as Btn, InlineKeyboardMarkup, InputMediaPhoto, KeyboardButton,
                      ReplyKeyboardMarkup, Update)
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

import actions
import backups
import checks
import history
import incidents
import network
import pkgupdates
import probes
import security
import updater
from settings import Settings
import i18n
from i18n import N_, _

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)  # иначе две строки в журнал на каждую проверку
log = logging.getLogger("monitor_bot")

CONFIG_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("config.yaml")
S = Settings(CONFIG_PATH)
CFG = S.cfg  # общий словарь, обновляется на месте при изменении настроек
STATE = checks.AlertState.load(S.dir / "state.json")
HIST = history.History(S.dir / "metrics.db")
ACT = actions.Actions(S.dir / "actions.log")
INC = incidents.Incidents(S.dir / "metrics.db")


def host() -> str:
    """Имя сервера в сообщениях: из «Настройки → Имя сервера» или config.yaml, иначе hostname."""
    return checks.host_name(CFG)


# ---------- Клавиатуры ----------

B_STATUS, B_PROCS, B_SERVICES, B_ALERTS, B_SETTINGS = N_('📊 Статус'), N_('🔝 Процессы'), N_('⚙️ Службы'), N_('🚨 Алерты'), N_('🛠 Настройки')

def main_kb() -> ReplyKeyboardMarkup:
    """Клавиатура внизу чата на текущем языке. После смены языка её присылают заново."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton(_(B_STATUS)), KeyboardButton(_(B_PROCS))],
         [KeyboardButton(_(B_SERVICES)), KeyboardButton(_(B_ALERTS))],
         [KeyboardButton(_(B_SETTINGS))]],
        resize_keyboard=True, is_persistent=True)


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([list(r) for r in rows if r])


def status_kb() -> InlineKeyboardMarkup:
    return kb([Btn(_('🔄 Обновить'), callback_data="status"), Btn(_('📈 График'), callback_data="chart:24h")],
              [Btn(_('🔝 Процессы'), callback_data="procs:cpu:10"), Btn(_('⚙️ Службы'), callback_data="services")],
              [Btn(_('🌐 Сеть и диск'), callback_data="net")])


def procs_kb(by: str, n: int) -> InlineKeyboardMarkup:
    other = "mem" if by == "cpu" else "cpu"
    return kb([Btn(_('🔄 Обновить'), callback_data=f"procs:{by}:{n}"),
               Btn(_('По RAM') if other == "mem" else _('По CPU'), callback_data=f"procs:{other}:{n}")],
              [Btn(_('Показать 20') if n == 10 else _('Показать 10'), callback_data=f"procs:{by}:{20 if n == 10 else 10}")]
              + ([Btn(_('⛔ Остановить…'), callback_data="act:killmenu")] if actions_on() else []))


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
    return kb([Btn(_('🔄 Обновить'), callback_data="services"),
               Btn(_('✏️ Что отслеживать'), callback_data="set:watch")],
              *[logs[i:i + 2] for i in range(0, len(logs), 2)],
              [b for b in [act_btn(_('🧰 Действия'), "act:menu")] if b])


def short_id(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:10]


def active_key(kid: str) -> str | None:
    return next((k for k in STATE.active if short_id(k) == kid), None)


def alert_kb(key: str | None = None, ackable: bool = False) -> InlineKeyboardMarkup:
    extra = []
    fix = None
    if key and key.startswith(("ctr:", "log:")):
        b = logs_btn(checks.key_label(key), _('📜 Логи'))
        if b:
            extra.append(b)
        fix = act_btn(_('♻️ Перезапустить'), f"act:ctr:{checks.key_label(key)}")
    elif key and key.startswith("svc:") and checks.key_label(key) in action_services():
        fix = act_btn(_('♻️ Перезапустить'), f"act:svc:{checks.key_label(key)}")
    elif key and key.startswith("disk:"):
        fix = act_btn(_('🧹 Очистить место'), "act:clean")
    elif key and key.startswith("brute:"):
        fix = act_btn(_('🚫 Заблокировать {key}', key=key[6:]), f"act:ban:{key[6:]}")
    ack = None
    if ackable and key:
        ack = [Btn(_('✔️ Принял'), callback_data=f"ack:{short_id(key)}"),
               Btn(_('💤 Этот на 1 ч'), callback_data=f"snz:{short_id(key)}")]
    return kb(ack,
              [Btn(_('🔕 Все на 1 ч'), callback_data="mute:3600:a"), Btn(_('🌙 До утра'), callback_data="mute:morning:a")],
              [Btn(_('📊 Статус сейчас'), callback_data="status:new")] + extra,
              [fix] if fix else None)


def group_text(alerts: list) -> str:
    lines = [f"<b>{html.escape(host())}</b>", _('Сразу {len} событий:', len=len(alerts))]
    for a in alerts:
        lines.append("• " + html.escape(a.text[:300]))
    text = "\n".join(lines)
    return text if len(text) < 3900 else text[:3900] + "\n…"


def group_kb() -> InlineKeyboardMarkup:
    return kb([Btn(_('✔️ Принять все'), callback_data="ackall"), Btn(_('🔕 Все на 1 ч'), callback_data="mute:3600:a")],
              [Btn(_('🚨 Активные алерты'), callback_data="alerts:new"), Btn(_('📊 Статус'), callback_data="status:new")])


def actions_menu() -> tuple[str, InlineKeyboardMarkup]:
    rows = []
    ctrs = [b for b in (act_btn(f"♻️ {c}", f"act:ctr:{c}") for c in checks.watched_containers(CFG, STATE)) if b]
    svcs = [b for b in (act_btn(f"♻️ {s}", f"act:svc:{s}") for s in action_services()) if b]
    rows += [ctrs[i:i + 2] for i in range(0, len(ctrs), 2)]
    rows += [svcs[i:i + 3] for i in range(0, len(svcs), 3)]
    rows.append([Btn(_('🧹 Очистить место'), callback_data="act:clean"),
                 Btn(_('⛔ Остановить процесс'), callback_data="act:killmenu")])
    rows.append([Btn(_('📦 Обновления пакетов'), callback_data="pkg:view")])
    rows.append([Btn(_('📜 Журнал действий'), callback_data="act:log"),
                 Btn(_('🔄 Перезагрузить сервер'), callback_data="act:reboot")])
    text = (_('🧰 <b>Действия</b>\n'
              'Каждое действие нужно подтвердить. Контейнеры перезапускаются через помощник, службы — только из '
              'списка action_services в config.yaml.'))
    return text, InlineKeyboardMarkup(rows)


def confirm_kb(token: str, yes: str = N_('✅ Да, выполнить')) -> InlineKeyboardMarkup:
    return kb([Btn(_(yes), callback_data=f"do:{token}"), Btn(_('✖️ Отмена'), callback_data=f"no:{token}")])


def actions_log_text() -> str:
    rows = ACT.last(10)
    if not rows:
        return _('📜 Журнал действий пуст')
    lines = [_('📜 <b>Последние действия</b>')]
    for r in rows:
        if len(r) >= 4:
            lines.append(f"{'✅' if r[3] == 'ok' else '❌'} {html.escape(r[0][5:16])} {html.escape(r[2])}")
    return "\n".join(lines)


def chart_kb(period: str) -> InlineKeyboardMarkup:
    return kb([Btn(("• " if p == period else "") + _(history.PERIOD_NAMES[p]), callback_data=f"chart:{p}:e")
               for p in history.PERIODS])


async def make_chart(period: str) -> tuple[bytes | None, str]:
    png = await asyncio.to_thread(history.chart_png, HIST, period, checks.tz(CFG), host())
    caption = await asyncio.to_thread(history.chart_caption, HIST, period)
    return png, caption


async def send_chart(update: Update, period: str) -> None:
    png, caption = await make_chart(period)
    if png is None:
        await send(update, _('📈 Данных для графика пока нет: история копится раз в минуту, загляните чуть позже.'))
        return
    await update.effective_message.reply_photo(png, caption=caption, reply_markup=chart_kb(period))


def logs_kb(name: str, tail: int) -> InlineKeyboardMarkup:
    return kb([Btn(_('🔄 Обновить'), callback_data=f"logs:{name}:{tail}"),
               Btn(_('100 строк') if tail == 30 else _('30 строк'), callback_data=f"logs:{name}:{100 if tail == 30 else 30}")])


def alerts_kb() -> InlineKeyboardMarkup:
    rows = [[Btn(_('🔄 Обновить'), callback_data="alerts"), Btn(_('📒 Журнал'), callback_data="inc")]]
    now = time.time()
    if STATE.muted(now):
        rows.append([Btn(_('🔔 Включить уведомления'), callback_data="mute:off")])
    else:
        rows.append([Btn(_('🔕 1 ч'), callback_data="mute:3600"), Btn(_('🔕 3 ч'), callback_data="mute:10800"),
                     Btn(_('🌙 До утра'), callback_data="mute:morning")])
    if STATE.in_maintenance(now):
        rows.append([Btn(_('✅ Закончить обслуживание'), callback_data="mt:off")])
    else:
        rows.append([Btn(_('🔧 Обслуживание:'), callback_data="noop")] +
                    [Btn(t, callback_data=f"mt:{s}") for t, s in ((_('30 мин'), 1800), (_('1 ч'), 3600), (_('2 ч'), 7200))])
    return kb(*rows)


# ---------- Тексты ----------

def alerts_text() -> str:
    now = time.time()
    lines = []
    if STATE.muted(now):
        lines.append(_('🔕 Уведомления выключены до {fmt_local}', fmt_local=checks.fmt_local(CFG, STATE.mute_until)))
    if STATE.in_maintenance(now):
        lines.append(_('🔧 Идёт обслуживание до {fmt_local}: алерты не приходят, проблемы пишутся в журнал как '
                       'плановые',
                       fmt_local=checks.fmt_local(CFG, STATE.maint_until)))
    if checks.in_quiet_hours(CFG, now):
        lines.append(_('🌙 Сейчас тихие часы: приходят только критичные алерты'))
    if STATE.active:
        lines.append(_('🚨 <b>Активные алерты</b>'))
        for k in STATE.active:
            since = STATE.since.get(k)
            mins = _(' ({int} мин)', int=int((now - since) // 60)) if since else ""
            mark = ""
            if k in STATE.acked:
                mark = _(' ✔️ принято')
            elif STATE.snoozed.get(k, 0) > now:
                mark = _(' 💤 до {fmt_local}', fmt_local=checks.fmt_local(CFG, STATE.snoozed[k]))
            lines.append(html.escape(STATE.problems.get(k, checks.key_label(k))) + mins + mark)
    else:
        lines.append(_('✅ Активных алертов нет'))
    return "\n".join(lines)


def onoff(v: bool) -> str:
    return "✅" if v else "▫️"


def settings_text() -> str:
    th = CFG["thresholds"]
    q = CFG.get("quiet_hours")
    remind = CFG.get("remind_min") or 0
    summary = CFG.get("daily_summary")
    return "\n".join([
        _('🛠 <b>Настройки</b>'),
        _('CPU ≥ {cpu}%, RAM ≥ {ram}%, диск ≥ {disk}%', cpu=th.get('cpu'), ram=th.get('ram'), disk=th.get('disk')),
        _('Сколько должно держаться превышение: {v} мин', v=th.get('duration_sec', 300) // 60),
        _('Проверка каждые {check_interval_sec} с', check_interval_sec=CFG.get('check_interval_sec', 60)),
        _('Тихие часы: {start}–{end}', start=q['start'], end=q['end']) if q else _('Тихие часы: выкл'),
        _('Напоминать о нерешённой проблеме: каждые {remind} мин', remind=remind) if remind else _('Напоминания: выкл'),
        _('Ежедневная сводка: в {summary}', summary=summary) if summary else _('Ежедневная сводка: выкл'),
        _('Недельный отчёт: по понедельникам в {weekly_report}',
          weekly_report=CFG['weekly_report']) if CFG.get("weekly_report") else _('Недельный отчёт: выкл'),
        _('Служб: {len}, контейнеров: {len2}, процессов: {len3}',
          len=len(CFG.get('services') or []), len2=len(CFG.get('containers') or []), len3=len(CFG.get('processes') or [])),
        "Docker: " + (_('все контейнеры') if CFG.get("containers_auto") else _('только из списка'))
        + (_(', ошибки в логах не чаще раза в {v} мин', v=CFG.get('log_alert_min') or 15) if CFG.get("log_alerts", True)
           else _(', ошибки в логах: выкл')),
        _('Проверок снаружи: {len}', len=len(CFG.get('probes') or []))
        + (_(', SSL предупреждать за {v} дн.',
             v=CFG.get('ssl_warn_days') or 14) if CFG.get("ssl_alerts", True) else ""),
        _('Имя сервера: {name}', name=html.escape(host())),
        _('Часовой пояс: {tz}', tz=CFG.get('tz')),
        _('Версия бота: {short}', short=updater.short(updater.current().get('sha'))),
        _('Язык: {name}', name=i18n.LANGS[i18n.lang()]),
    ])


# На всех языках сразу: кнопку языка должно быть легко найти, даже если случайно выбран непонятный язык
LANG_BTN = "🌐 Язык · Мова · Language"


def lang_view() -> tuple[str, InlineKeyboardMarkup]:
    cur = i18n.lang()
    btns = [[Btn(("• " if code == cur else "") + name, callback_data=f"lang:{code}")]
            for code, name in i18n.LANGS.items()]
    return (_('🌐 <b>Язык интерфейса</b>\nНа этом языке будут кнопки, ответы, алерты и отчёты. '
              'Уже отправленные сообщения не меняются.'), InlineKeyboardMarkup(btns + [[back()]]))


LANG_ALIASES = {"ua": "uk", "ukr": "uk", "rus": "ru", "eng": "en"}


async def set_language(update: Update, ctx: ContextTypes.DEFAULT_TYPE, code: str) -> None:
    """Сохранить язык, прислать клавиатуру и меню команд на нём."""
    S.set("lang", code)  # Settings сам переключает i18n
    await send(update, _('✅ Язык интерфейса: {name}', name=i18n.LANGS[i18n.lang()]), main_kb())
    await register_commands(ctx.bot)


async def cmd_lang(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    code = (ctx.args[0].lower() if ctx.args else "")
    code = LANG_ALIASES.get(code, code)
    if code in i18n.LANGS:
        await set_language(update, ctx, code)
    else:
        await send(update, *lang_view())


def settings_kb() -> InlineKeyboardMarkup:
    return kb([Btn(_('📏 Пороги'), callback_data="set:th"), Btn(_('⏱ Интервал'), callback_data="set:int")],
              [Btn(_('👁 Что отслеживать'), callback_data="set:watch")],
              [Btn(_('🌙 Тихие часы'), callback_data="set:quiet"), Btn(_('⏰ Напоминания'), callback_data="set:remind")],
              [Btn(_('📋 Сводки и отчёт'), callback_data="set:summary"), Btn("🐳 Docker", callback_data="set:docker")],
              [Btn(_('🛡 Безопасность'), callback_data="set:sec"), Btn(_('🌐 Сайты и порты'), callback_data="set:probes")],
              [Btn(_('🧠 Умные алерты'), callback_data="set:smart"), Btn(_('💾 Бэкапы'), callback_data="set:backup")],
              [Btn(_('⬆️ Обновление бота'), callback_data="set:update"), Btn(LANG_BTN, callback_data="set:lang")],
              [Btn(_('🏷 Имя сервера'), callback_data="set:name")],
              [Btn(_('↩️ Сбросить к config.yaml'), callback_data="set:reset")])


def back() -> Btn:
    return Btn(_('⬅️ Назад'), callback_data="set")


SERVER_NAME_MAX = 64


def name_view() -> tuple[str, InlineKeyboardMarkup]:
    text = _('🏷 <b>Имя сервера</b>: {name}\n'
             'Оно стоит в начале каждого сообщения бота.\n'
             '\n'
             'Чтобы сменить, отправьте: <code>/name новое-имя</code>',
             name=html.escape(host()))
    rows = []
    if "server_name" in S.overrides:
        rows.append([Btn(_('↩️ Как в config.yaml'), callback_data="name:reset")])
    return text, InlineKeyboardMarkup(rows + [[back()]])


async def cmd_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    name = " ".join(ctx.args or []).strip()
    if not name:
        await send(update, *name_view())
        return
    if not checks.valid_server_name(name, SERVER_NAME_MAX):
        await send(update, _('Имя может содержать буквы, цифры, пробел и знаки . _ - ( ), не длиннее {n} символов.',
                             n=SERVER_NAME_MAX))
        return
    S.set("server_name", name)
    await send(update, _('✅ Имя сервера: {name}', name=html.escape(host())), None)


def thresholds_view() -> tuple[str, InlineKeyboardMarkup]:
    th = CFG["thresholds"]
    rows = []
    for key, name in (("cpu", "CPU"), ("ram", "RAM"), ("disk", _('Диск'))):
        rows.append([Btn(f"{name}: {th.get(key)}%", callback_data="noop"),
                     Btn("−5", callback_data=f"th:{key}:-5"), Btn("+5", callback_data=f"th:{key}:5")])
    d = th.get("duration_sec", 300)
    rows.append([Btn(("• " if d == s else "") + _('{v} мин',
                                                  v=s // 60), callback_data=f"thd:{s}") for s in (60, 300, 900)])
    rows.append([back()])
    return (_('📏 <b>Пороги</b>\nАлерт, если значение выше порога дольше выбранного времени.'),
            InlineKeyboardMarkup(rows))


def interval_view() -> tuple[str, InlineKeyboardMarkup]:
    cur = CFG.get("check_interval_sec", 60)
    opts = ((30, _('30 с')), (60, _('1 мин')), (300, _('5 мин')))
    return (_('⏱ <b>Как часто проверять сервер</b>'),
            kb([Btn(("• " if cur == s else "") + t, callback_data=f"int:{s}") for s, t in opts], [back()]))


def toggle_btn(label: str, kind: str, name: str) -> Btn:
    """Кнопка «перестать отслеживать». Telegram ограничивает callback_data 64 байтами: обрезать имя нельзя
    (обрезанное имя добавилось бы в список как новое), поэтому для длинного имени кнопка ничего не делает."""
    data = f"tg:{kind}:{name}"
    if len(data.encode()) <= 64:
        return Btn(label, callback_data=data)
    return Btn(label + _(' (длинное имя, убрать можно в config.yaml)'), callback_data="noop")


def watch_view() -> tuple[str, InlineKeyboardMarkup]:
    rows = []
    for svc in CFG.get("services") or []:
        rows.append([toggle_btn(_('✅ служба {svc}', svc=svc), "services", svc)])
    for c in CFG.get("containers") or []:
        rows.append([toggle_btn(_('✅ контейнер {c}', c=c), "containers", c)])
    for p in CFG.get("processes") or []:
        rows.append([toggle_btn(_('✅ процесс {p}', p=p), "processes", p)])
    rows.append([Btn(_('➕ Служба'), callback_data="add:services"), Btn(_('➕ Контейнер'), callback_data="add:containers")])
    rows.append([back()])
    return (_('👁 <b>Что отслеживать</b>\nНажмите на элемент, чтобы перестать его отслеживать.'),
            InlineKeyboardMarkup(rows))


async def add_view(kind: str) -> tuple[str, InlineKeyboardMarkup]:
    if kind == "services":
        found = await asyncio.to_thread(checks.running_services)
        title = _('службы')
    else:
        states = await asyncio.to_thread(checks.container_states)
        found = sorted(states or {})
        title = _('контейнеры')
    current = set(CFG.get(kind) or [])
    btns = [Btn(f"{onoff(n in current)} {n}", callback_data=f"tg:{kind}:{n}:add")
            for n in found if len(f"tg:{kind}:{n}:add".encode()) <= 64]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([Btn(_('⬅️ Назад'), callback_data="set:watch")])
    text = _('➕ <b>Работающие {title} на сервере</b>\nНажмите, чтобы включить или выключить отслеживание.', title=title)
    if not found:
        text += _('\n\nНичего не найдено (нет доступа или список пуст).')
    return text, InlineKeyboardMarkup(rows)


QUIET_OPTS = (None, ("23:00", "06:00"), ("23:00", "08:00"), ("00:00", "07:00"), ("22:00", "09:00"))


def quiet_view() -> tuple[str, InlineKeyboardMarkup]:
    q = CFG.get("quiet_hours")
    cur = (q["start"], q["end"]) if q else None
    btns = [Btn(("• " if cur == o else "") + (f"{o[0]}–{o[1]}" if o else _('Выкл')),
                callback_data=f"quiet:{o[0]}-{o[1]}" if o else "quiet:off") for o in QUIET_OPTS]
    return (_('🌙 <b>Тихие часы</b>\n'
              'В это время приходят только критичные алерты: упала служба, контейнер или процесс. Остальные придут '
              'утром, если проблема не исчезнет.'),
            kb(btns[:3], btns[3:], [back()]))


def remind_view() -> tuple[str, InlineKeyboardMarkup]:
    cur = CFG.get("remind_min") or 0
    btns = [Btn(("• " if cur == m else "") + (_('{m} мин', m=m) if m else _('Выкл')), callback_data=f"remind:{m}")
            for m in (0, 30, 60, 180)]
    return (_('⏰ <b>Напоминания</b>\nПовторять алерт, пока проблема не решена.'), kb(btns, [back()]))


def summary_view() -> tuple[str, InlineKeyboardMarkup]:
    cur = CFG.get("daily_summary")
    btns = [Btn(("• " if cur == t else "") + (t or _('Выкл')), callback_data=f"summary:{t or 'off'}")
            for t in (None, "09:00", "20:00")]
    wk = CFG.get("weekly_report")
    wbtns = [Btn(("• " if wk == t else "") + (t or _('Выкл')), callback_data=f"wk:time:{t or 'off'}")
             for t in (None, "09:00", "20:00")]
    return (_('📋 <b>Сводки</b>\n'
              'Ежедневная: состояние сервера и алерты за сутки.\n'
              'Недельная (по понедельникам): инциденты и простой, доступность сайтов, нагрузка, события, бэкапы и '
              'график за 7 дней.'),
            kb([Btn(_('Каждый день:'), callback_data="noop")], btns, [Btn(_('По понедельникам:'), callback_data="noop")],
               wbtns, [Btn(_('📅 Прислать недельный отчёт сейчас'), callback_data="wk:now")], [back()]))


def docker_view() -> tuple[str, InlineKeyboardMarkup]:
    auto = bool(CFG.get("containers_auto"))
    logs_on = CFG.get("log_alerts", True)
    every = CFG.get("log_alert_min") or 15
    rows = [[Btn(_('{onoff} Отслеживать все контейнеры', onoff=onoff(auto)), callback_data="dk:auto")],
            [Btn(_('{onoff} Алерты на ошибки в логах', onoff=onoff(logs_on)), callback_data="dk:logs")]]
    if logs_on:
        rows.append([Btn(("• " if every == m else "") + _('не чаще 1 раза в {m} мин',
                                                          m=m), callback_data=f"dk:every:{m}")
                     for m in (15, 60)])
    rows.append([back()])
    return (_('🐳 <b>Docker</b>\n'
              'Все контейнеры: бот сам добавляет каждый контейнер, который видел работающим, и пишет, если он '
              'остановился или перезапустился.\n'
              'Ошибки в логах: строки с Traceback, ERROR, CRITICAL, Exception, FATAL.'),
            InlineKeyboardMarkup(rows))


def security_view() -> tuple[str, InlineKeyboardMarkup]:
    thr = CFG.get("ssh_fail_threshold", 30)
    ab_thr = int(CFG.get("autoban_threshold") or 10)
    ab_h = int(CFG.get("autoban_hours") if CFG.get("autoban_hours") is not None else 24)
    rows = [[Btn(_('{onoff} Входы по SSH и подбор пароля',
                   onoff=onoff(CFG.get('ssh_alerts', True))), callback_data="sec:ssh_alerts")],
            [Btn(_('{onoff} Сообщать о каждом входе',
                   onoff=onoff(CFG.get('ssh_alert_all_logins'))), callback_data="sec:ssh_alert_all_logins")],
            [Btn(("• " if thr == n else "") + _('≥{n} попыток',
                                                n=n), callback_data=f"secthr:{n}") for n in (30, 100, 300)],
            [Btn(_('{onoff} Автобан при подборе пароля',
                   onoff=onoff(CFG.get('autoban', True))), callback_data="sec:autoban")],
            [Btn(("• " if ab_thr == n else "") + _('≥{n} с IP', n=n), callback_data=f"abthr:{n}") for n in (5, 10, 20)],
            [Btn(("• " if ab_h == h else "") + security.ban_duration_text(h, bare=True),
                 callback_data=f"abh:{h}") for h in (1, 24, 168, 0)],
            [Btn(_('{onoff} Новые открытые порты',
                   onoff=onoff(CFG.get('port_alerts', True))), callback_data="sec:port_alerts")],
            [Btn(_('{onoff} Обновления безопасности',
                   onoff=onoff(CFG.get('update_alerts', True))), callback_data="sec:update_alerts")],
            [Btn(_('{onoff} И обычные обновления пакетов',
                   onoff=onoff(CFG.get('update_alerts_all'))), callback_data="sec:update_alerts_all"),
             Btn(_('📦 Список'), callback_data="pkg:view:new")],
            [Btn(_('🧽 Забыть известные IP ({len})', len=len(STATE.known_ips)), callback_data="secforget")],
            [b for b in [act_btn(_('🚫 Заблокированные IP'), "act:banned")] if b],
            [back()]]
    hb = CFG.get("heartbeat_url")
    hb_text = (_('не настроен') if not hb else
               _('работает') if STATE.heartbeat_ok else _('не доходит') if STATE.heartbeat_ok is False else _('ждёт первой проверки'))
    text = (_('🛡 <b>Безопасность</b>\n'
              'Известные IP для входа по SSH: {escape}\n'
              'Открытые наружу порты: {escape2}\n'
              'Внешний контроль (heartbeat_url в config.yaml): {hb_text}\n'
              'Подбор пароля: алерт, если неудачных попыток за 10 минут больше порога, не чаще раза в час.\n',
              escape=html.escape(', '.join(STATE.known_ips) or _('пока нет')), escape2=html.escape(', '.join(STATE.known_ports) or _('ещё не проверены')), hb_text=hb_text)
            + (_('Автобан: IP с {ab_thr} и больше неудачными попытками за 10 минут блокируется {ban_duration_text}. '
                 'IP, с которых входили по SSH за 30 дней, и открытые сейчас SSH-сессии не блокируются.',
                 ab_thr=ab_thr, ban_duration_text=security.ban_duration_text(ab_h)) if CFG.get("autoban", True) else _('Автобан: выкл')))
    return text, InlineKeyboardMarkup(rows)


probe_id = short_id

LATEST: dict = {}  # последняя проверка GitHub: {"sha", "message", "date", "changes", "checked", "error"}


def update_repo() -> str:
    """Репозиторий, из которого ставит обновления помощник (/etc/monitor-bot/update-repo или по умолчанию).
    Берём его же, иначе бот показал бы изменения одного репозитория, а помощник поставил бы код из другого."""
    return updater.helper_repo() or updater.DEFAULT_REPO


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
    lines = [_('⬆️ <b>Обновление бота</b>'),
             _('Сейчас: {short}', short=updater.short(cur.get('sha')))
             + (_(', установлена {age_text}',
                  age_text=updater.age_text(cur.get('installed'))) if cur.get("installed") else "")]
    rows = [[Btn(_('🔍 Проверить обновления'), callback_data="up:check")]]
    if LATEST.get("error"):
        lines.append(_('Не удалось проверить GitHub: {escape}', escape=html.escape(LATEST['error'])))
    elif LATEST.get("sha"):
        if LATEST["sha"] == cur.get("sha"):
            lines.append(_('✅ Установлена последняя версия'))
        else:
            lines.append(_('Доступна версия {sha}: {escape}',
                           sha=LATEST['sha'][:7], escape=html.escape(LATEST['message'])))
            for c in LATEST.get("changes") or []:
                lines.append(f"  • {html.escape(c)}")
            if actions_on():
                rows.append([Btn(_('⬆️ Обновить до {sha}',
                                   sha=LATEST['sha'][:7]), callback_data=f"up:go:{LATEST['sha']}")])
    lines.append(_('\n'
                   'Перед обновлением бот прогоняет тесты новой версии и сохраняет копию текущей. Если новая версия '
                   'не запустится, бот сам вернёт прежнюю. Помощник с правами root, sudoers и файл службы так не '
                   'обновляются, это делается вручную.'))
    if actions_on():
        rows.append([Btn(_('🧽 Удалить старые копии кода'), callback_data="up:prune")])
    rows.append([back()])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def report_update(bot) -> None:
    """Сообщить результат обновления один раз (файл пишет помощник)."""
    res = await asyncio.to_thread(updater.last_result)
    if not res or res.get("finished") == STATE.update_reported:
        return
    STATE.update_reported = res.get("finished")
    STATE.save()
    await broadcast(bot, f"<b>{html.escape(host())}</b>\n{html.escape(updater.result_text(res))}")


def pkg_view() -> tuple[str, InlineKeyboardMarkup]:
    """Доступные обновления пакетов Ubuntu и кнопки установки. Блокирующая (apt), вызывать в потоке."""
    pkgs = pkgupdates.upgradable()
    busy = pkgupdates.running()
    reboot = pkgupdates.reboot_info()
    lines = [_('📦 <b>Обновления пакетов</b>')] + [html.escape(ln) for ln in pkgupdates.view_lines(pkgs)]
    text = "\n".join(lines)
    if len(text) > 3000:
        text = text[:3000] + "\n…"
    lines = [text]
    age = pkgupdates.lists_age()
    if age:
        lines.append(_('Списки пакетов обновлены: {local_dt}', local_dt=local_dt(age)))
    if reboot is not None:
        lines.append(_('♻️ Серверу нужна перезагрузка')
                     + (f" ({html.escape(pkgupdates.names_short(reboot))})" if reboot else ""))
    if busy:
        lines.append(_('⏳ Сейчас идёт установка обновлений, результат придёт отдельным сообщением.'))
    rows = [[Btn(_('🔍 Проверить заново'), callback_data="pkg:refresh")]]
    if actions_on() and pkgs and not busy:
        sec = sum(p.security for p in pkgs)
        if sec:
            rows.append([Btn(_('🛡 Установить обновления безопасности ({n})', n=sec), callback_data="pkg:go:security")])
        n_all = len(pkgupdates.installable(pkgs, "all"))
        if n_all:
            rows.append([Btn(_('⬆️ Установить все ({n})', n=n_all), callback_data="pkg:go:all")])
    if actions_on() and reboot is not None:
        rows.append([Btn(_('🔄 Перезагрузить сервер'), callback_data="act:reboot")])
    lines.append(_('\n🛡 — обновление безопасности. Установка идёт через помощник с подтверждением; настройки '
                   'пакетов не меняются, пакеты не удаляются. Сервер сам не перезагружается.'))
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def report_apt(bot) -> None:
    """Сообщить результат установки обновлений один раз (файл пишет помощник)."""
    res = await asyncio.to_thread(pkgupdates.last_result)
    if not res or res.get("finished") == STATE.apt_reported:
        return
    first = STATE.apt_reported is None and time.time() - (res.get("finished") or 0) > 3600
    STATE.apt_reported = res.get("finished")
    STATE.save()
    if first:
        return  # старый результат (например, после обновления бота) не присылаем
    if res.get("left") and "phased" not in res:
        # какие из оставшихся Ubuntu выпускает поэтапно: они не ошибка, придут позже
        h = await asyncio.to_thread(pkgupdates.held)
        res["phased"] = [n for n in res["left"] if h.get(n) == "phased"]
    rows = [[Btn(_('📦 Обновления пакетов'), callback_data="pkg:view:new")]]
    if res.get("reboot_required") and actions_on():
        rows.append([Btn(_('🔄 Перезагрузить сервер'), callback_data="act:reboot")])
    await broadcast(bot, f"<b>{html.escape(host())}</b>\n{html.escape(pkgupdates.result_text(res))}",
                    InlineKeyboardMarkup(rows))


BACKUP_TIMES = (None, "03:45", "05:00")
OFFSITE_OPTS = (None, 48, 72, 168)


def backup_kb() -> InlineKeyboardMarkup:
    t = CFG.get("backup_time")
    rows = [[Btn(_('💾 Сделать копию бота сейчас'), callback_data="bk:make")],
            [Btn(_('📤 Прислать копию бота'), callback_data="bk:send:own")]]
    for i, st in enumerate(backups.watched_status() or []):
        name = Path(st["path"]).name or st["path"]
        rows.append([Btn(_('📤 Прислать {name}', name=name[:30]), callback_data=f"bk:send:{i}")])
    rows.append([Btn(("• " if t == o else "") + (o or _('Не делать')), callback_data=f"bk:time:{o or 'off'}")
                 for o in BACKUP_TIMES])
    rows.append([Btn(_('{onoff} Каждый день присылать копии сюда', onoff=onoff(CFG.get('backup_send_daily'))),
                     callback_data="bk:daily")])
    rows.append([Btn(_('{onoff} Алерт, если бэкап устарел',
                       onoff=onoff(CFG.get('backup_alerts', True))), callback_data="bk:alerts")])
    lim = CFG.get("offsite_max_age_h")
    rows.append([Btn(_('💻 ПК:'), callback_data="noop")] +
                [Btn(("• " if lim == o else "") + (_('{v} дн.', v=o // 24) if o else _('выкл')),
                     callback_data=f"bk:pc:{o or 'off'}")
                 for o in OFFSITE_OPTS])
    rows.append([Btn(_('🧪 Проверить копии сейчас'), callback_data="bk:verify")])
    rows.append([Btn(_('{onoff} Проверять копии по воскресеньям', onoff=onoff(CFG.get('verify_backups', True))),
                     callback_data="bk:vauto")])
    rows.append([Btn(_('🔄 Обновить'), callback_data="bk:refresh"), back()])
    return InlineKeyboardMarkup(rows)


def local_dt(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, checks.tz(CFG)).strftime("%d.%m %H:%M")


def verify_line() -> str:
    v = STATE.verify_last
    if not v:
        return _('🧪 Копии ещё не проверялись')
    bad = [r for r in v.get("results") or [] if not r.get("ok")]
    when = local_dt(v["ts"])
    return (_('🧪 Проверка {when}: ', when=when) + (_('все копии читаются целиком') if not bad else
            _('есть проблемы: ') + ", ".join(html.escape(str(r.get("path"))) for r in bad)))


def backup_view() -> tuple[str, InlineKeyboardMarkup]:
    text = backups.status_text(CFG, S.dir) + "\n" + verify_line() + (
        _('\n'
          '\n'
          'Копия бота: config.yaml (с токеном), настройки, состояние алертов, история метрик. Лежит на этом же '
          'сервере, поэтому полезно присылать её сюда или забирать на ПК. Не пересылайте её никому.'))
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
            await send_file(bot, c, own, _('💾 Копия бота {HOST}. Внутри токен, не пересылайте.', HOST=host()))
    if watched:
        for i, st in enumerate(await asyncio.to_thread(backups.watched_status) or []):
            path, err = await asyncio.to_thread(backups.fetch, i)
            if path is None:
                errors.append(f"{st['path']}: {err}")
                continue
            for c in chat_ids:
                await send_file(bot, c, path, _('💾 {path} на {HOST}', path=st['path'], HOST=host()))
    return errors


def smart_view() -> tuple[str, InlineKeyboardMarkup]:
    rows = [[Btn(_('{onoff} Необычная нагрузка для этого часа', onoff=onoff(CFG.get('anomaly_alerts', True))),
                 callback_data="sm:anomaly_alerts")],
            [Btn(_('{onoff} Память растёт и скоро закончится', onoff=onoff(CFG.get('ram_trend_alerts', True))),
                 callback_data="sm:ram_trend_alerts")],
            [Btn(_('{onoff} Много алертов сразу — одним сообщением', onoff=onoff(CFG.get('group_alerts', True))),
                 callback_data="sm:group_alerts")],
            [back()]]
    text = (_('🧠 <b>Умные алерты</b>\n'
              'Необычная нагрузка: бот знает, сколько CPU и RAM обычно бывает в этот час (по истории за 14 дней), и '
              'пишет, если 15 минут подряд намного больше обычного, даже когда до порога далеко. Начинает работать, '
              'когда накопится 3 дня истории.\n'
              'Память растёт: если RAM ровно растёт 6 часов и при таком темпе закончится меньше чем через 12 часов '
              '(похоже на утечку). Не чаще раза в 6 часов.\n'
              'Под каждым алертом: «✔️ Принял» — без напоминаний, пока проблема не исчезнет; «💤 Этот на 1 ч» — '
              'молчать только об этой проблеме.'))
    return text, InlineKeyboardMarkup(rows)


PROBES_HELP = (N_('Добавить: <code>/probe https://site.ru</code>\n'
                  'Текст на странице: <code>/probe https://site.ru/health | OK</code>\n'
                  'Порт: <code>/probe tcp://host:5432</code>, ping: <code>/probe ping://1.1.1.1</code>, DNS: '
                  '<code>/probe dns://site.ru</code>'))


def probes_view() -> tuple[str, InlineKeyboardMarkup]:
    raws = list(CFG.get("probes") or [])
    fails = int(CFG.get("probe_fail_count") or 2)
    ssl_on = CFG.get("ssl_alerts", True)
    rows = [[Btn(f"🗑 {r[:40]}", callback_data=f"pr:del:{probe_id(r)}")] for r in raws]
    if raws:
        rows.append([Btn(_('▶️ Проверить сейчас'), callback_data="pr:run")])
    rows.append([Btn(("• " if fails == n else "") + _('алерт после {n} неудач', n=n), callback_data=f"pr:fails:{n}")
                 for n in (1, 2, 3)])
    rows.append([Btn(_('{onoff} Предупреждать об окончании SSL', onoff=onoff(ssl_on)), callback_data="pr:ssl")])
    rows.append([back()])
    body = probes.probes_text(CFG, STATE) if raws else _('🌐 <b>Проверки снаружи</b>\nПока ничего не проверяется.')
    text = (_('{body}\n'
              '\n'
              'Бот проверяет цели каждые {check_interval_sec} с. Падение сайта приходит и в тихие часы. SSL: '
              'предупреждения за 14, 7, 3 и 1 день до окончания.\n'
              'Нажмите на цель, чтобы удалить её.\n'
              '\n'
              '{PROBES_HELP}',
              body=body, check_interval_sec=CFG.get('check_interval_sec', 60), PROBES_HELP=_(PROBES_HELP)))
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
    """Личный чат из allowed_chats. В группе (если её chat_id добавлен в allowed_chats) команды принимаются
    только от людей из allowed_users или чьи личные chat_id есть в allowed_chats: иначе сервером управлял бы
    любой участник группы."""
    chat = update.effective_chat
    if not chat or chat.id not in CFG["allowed_chats"]:
        return False
    if chat.type == "private":
        return True
    user = update.effective_user
    trusted = set(CFG["allowed_chats"]) | {int(x) for x in (CFG.get("allowed_users") or [])}
    return bool(user) and user.id in trusted


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
        await send(update, _('Доступ закрыт. Ваш chat_id: <code>{id}</code>\n'
                             'Добавьте его в allowed_chats в config.yaml и перезапустите бота.',
                             id=update.effective_chat.id))
        return
    await update.effective_message.reply_text(
        _('👋 Бот мониторинга сервера. Управляйте кнопками внизу или командами:\n'
          '/status /procs /mem /services /alerts /settings /logs /chart /actions /probe /backup\n'
          '/mute 2h — выключить уведомления, /unmute — включить\n'
          '/lang — язык / мова / language'), reply_markup=main_kb())


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
        await send(update, _('Чьи логи показать? Или /logs имя_контейнера'),
                   kb(*[btns[i:i + 2] for i in range(0, len(btns), 2)]) if btns else None)
        return
    name = ctx.args[0]
    if not checks.valid_container(name):
        await send(update, _('Неверное имя контейнера: {escape}', escape=html.escape(name[:64])))
        return
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
        await send(update, _('Действия выключены (actions_enabled: false в config.yaml)'))
        return
    await send(update, *actions_menu())


async def cmd_updates(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await send(update, *(await asyncio.to_thread(pkg_view)))


async def cmd_probe(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    arg = " ".join(ctx.args or []).strip()
    if not arg:
        await send(update, *probes_view())
        return
    norm = probes.normalize(arg)
    if not norm:
        await send(update, _('Не понял цель «{escape}».\n'
                             '\n'
                             '{PROBES_HELP}',
                             escape=html.escape(arg), PROBES_HELP=_(PROBES_HELP)))
        return
    raws = list(CFG.get("probes") or [])
    if norm in raws:
        await send(update, _('Уже проверяю: {escape}', escape=html.escape(norm)), None)
        return
    if len(raws) >= 30:
        await send(update, _('Максимум 30 целей. Удалите лишние в /probe'))
        return
    S.set("probes", raws + [norm])
    await asyncio.to_thread(run_probes_now)
    await send(update, _('➕ Добавлено: {escape}', escape=html.escape(norm)), None)
    await send(update, *probes_view())


async def cmd_incidents(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await send(update, await asyncio.to_thread(incidents.incidents_text, INC, CFG), inc_kb())


def inc_kb() -> InlineKeyboardMarkup:
    return kb([Btn(_('🔄 Обновить'), callback_data="inc:r"), Btn(_('📅 Отчёт за неделю'), callback_data="wk:now")])


async def cmd_backup(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await send(update, *(await asyncio.to_thread(backup_view)))


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await send(update, settings_text(), settings_kb())


MAX_MUTE_SEC = 30 * 86400


def parse_duration(s: str) -> int | None:
    s = s.strip().lower()
    mult = {"m": 60, "м": 60, "h": 3600, "ч": 3600, "г": 3600, "d": 86400, "д": 86400}  # г — година (укр.)
    try:
        if s[-1] in mult:
            return int(float(s[:-1]) * mult[s[-1]])
        return int(float(s) * 60)
    except (ValueError, IndexError, OverflowError):  # «1e400d» даёт бесконечность
        return None


async def cmd_mute(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    sec = parse_duration(ctx.args[0]) if ctx.args else 3600
    if not sec or sec <= 0 or sec > MAX_MUTE_SEC:
        await send(update, _('Формат: /mute 30m, /mute 2h, /mute 1d (до 30 дней)'))
        return
    await send(update, set_mute(time.time() + sec))


async def cmd_unmute(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await send(update, set_mute(0))


def set_maint(until: float) -> str:
    STATE.maint_until = until
    STATE.save()
    if until <= time.time():
        return _('✅ Обслуживание закончено, алерты снова приходят')
    return _('🔧 Обслуживание до {fmt_local}: алерты не приходят', fmt_local=checks.fmt_local(CFG, until))


def maint_end_text() -> str:
    lines = [f"<b>{html.escape(host())}</b>", _('✅ Обслуживание закончилось, алерты снова приходят.')]
    probs = [k for k, s in STATE.since.items()]
    if probs:
        lines.append(_('Сейчас есть проблемы, алерты по ним придут в ближайшие минуты: ')
                     + ", ".join(html.escape(checks.key_label(k)) for k in probs[:8]))
    else:
        lines.append(_('Проблем сейчас нет.'))
    return "\n".join(lines)


async def cmd_maint(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    if ctx.args and ctx.args[0] in ("off", "stop", "0"):
        await send(update, set_maint(0))
        return
    sec = parse_duration(ctx.args[0]) if ctx.args else 1800
    if not sec or sec <= 0 or sec > 86400:
        await send(update, _('Формат: /maint 30m, /maint 2h (до суток), /maint off'))
        return
    await send(update, set_maint(time.time() + sec))


def set_mute(until: float) -> str:
    STATE.mute_until = until
    STATE.save()
    if until <= time.time():
        return _('🔔 Уведомления включены')
    return _('🔕 Уведомления выключены до {fmt_local}', fmt_local=checks.fmt_local(CFG, until))


TEXT_BUTTONS = {B_STATUS: cmd_status, B_PROCS: cmd_procs, B_SERVICES: cmd_services,
                B_ALERTS: cmd_alerts, B_SETTINGS: cmd_settings}


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    # кнопка могла остаться от клавиатуры на другом языке, поэтому узнаём её на любом
    text = (update.effective_message.text or "").strip()
    fn = next((f for b, f in TEXT_BUTTONS.items() if text in i18n.variants(b)), None)
    if fn:
        await fn(update, ctx)


# ---------- Inline-кнопки ----------

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not allowed(update):
        await q.answer(_('Доступ закрыт'), show_alert=True)
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
                note = _('Эта проблема уже решена')
            else:
                if key not in STATE.acked:
                    STATE.acked.append(key)
                    STATE.save()
                note = _('Принято: напоминаний не будет, пока проблема не исчезнет')
                try:
                    await q.edit_message_reply_markup(alert_kb(key))
                except BadRequest:
                    pass
        elif cmd == "ackall":
            STATE.acked = list(STATE.active)
            STATE.save()
            note = _('Принято: {len}', len=len(STATE.acked)) if STATE.acked else _('Активных алертов нет')
        elif cmd == "snz":
            key = active_key(parts[1])
            if key is None:
                note = _('Эта проблема уже решена')
            else:
                STATE.snoozed[key] = time.time() + 3600
                STATE.save()
                note = _('Об этой проблеме молчу до {fmt_local}', fmt_local=checks.fmt_local(CFG, STATE.snoozed[key]))
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
        elif cmd == "mt":
            note = set_maint(0 if parts[1] == "off" else time.time() + int(parts[1]))
            await edit(update, alerts_text(), alerts_kb())
        elif cmd == "set":
            sub = parts[1] if len(parts) > 1 else ""
            if sub == "reset":
                S.reset()
                reschedule(ctx.application)
                note = _('Настройки сброшены к config.yaml')
                await edit(update, settings_text(), settings_kb())
            elif sub == "":
                await edit(update, settings_text(), settings_kb())
            else:
                view = {"th": thresholds_view, "int": interval_view, "watch": watch_view,
                        "quiet": quiet_view, "remind": remind_view, "summary": summary_view,
                        "docker": docker_view, "sec": security_view, "probes": probes_view,
                        "smart": smart_view, "backup": backup_view,
                        "update": update_view, "lang": lang_view, "name": name_view}[sub]
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
            note = (_('Отслеживаю: ') if on else _('Не отслеживаю: ')) + name
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
                    note = _('Данных за этот период пока нет')
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
            default = key not in ("ssh_alert_all_logins", "update_alerts_all")
            S.set(key, not CFG.get(key, default))
            await edit(update, *security_view())
        elif cmd == "abthr":
            S.set("autoban_threshold", int(parts[1]))
            await edit(update, *security_view())
        elif cmd == "abh":
            S.set("autoban_hours", int(parts[1]))
            await edit(update, *security_view())
        elif cmd == "secthr":
            S.set("ssh_fail_threshold", int(parts[1]))
            await edit(update, *security_view())
        elif cmd == "secforget":
            STATE.known_ips = []
            STATE.known_ip_at = {}
            STATE.save()
            note = _('Следующий вход с любого IP придёт как новый')
            await edit(update, *security_view())
        elif cmd == "pr":
            sub = parts[1]
            if sub == "del":
                raws = list(CFG.get("probes") or [])
                gone = [r for r in raws if probe_id(r) == parts[2]]
                if gone:
                    S.set("probes", [r for r in raws if r not in gone])
                    note = _('Удалено: ') + gone[0][:150]
                else:
                    note = _('Уже удалено')
            elif sub == "run":
                await asyncio.to_thread(run_probes_now)
                note = _('Проверено')
            elif sub == "fails":
                S.set("probe_fail_count", int(parts[2]))
                note = _('Сохранено')
            elif sub == "ssl":
                S.set("ssl_alerts", not CFG.get("ssl_alerts", True))
                note = _('Сохранено')
            await edit(update, *probes_view())
        elif cmd == "bk":
            sub = parts[1]
            if sub == "make":
                await q.answer(_('Делаю копию…'))
                answered = True
                path, size, removed = await asyncio.to_thread(
                    backups.make_backup, CONFIG_PATH, S.dir, int(CFG.get("backup_keep") or 14))
                await send(update, _('💾 Готово: {escape}, {fmt_bytes}',
                                     escape=html.escape(path.name), fmt_bytes=checks.fmt_bytes(size))
                                   + (_(', удалено старых: {removed}', removed=removed) if removed else ""))
                await edit(update, *backup_view())
            elif sub == "send":
                await q.answer(_('Отправляю…'))
                answered = True
                chat = [update.effective_chat.id]
                if parts[2] == "own":
                    files = backups.own_files(S.dir)
                    if not files:
                        await send(update, _('Копий бота ещё нет. Нажмите «Сделать копию бота сейчас».'))
                    else:
                        await send_backups(ctx.bot, chat, watched=False)
                else:
                    i = int(parts[2])
                    path, err = await asyncio.to_thread(backups.fetch, i)
                    if path is None:
                        await send(update, _('Не удалось: {escape}', escape=html.escape(err)))
                    else:
                        await send_file(ctx.bot, chat[0], path, _('💾 {name} с {HOST}', name=path.name, HOST=host()))
            elif sub == "verify":
                await q.answer(_('Проверяю копии, это может занять пару минут…'))
                answered = True
                text = await run_verify()
                await send(update, text)
            else:
                if sub == "time":
                    S.set("backup_time", None if parts[2] == "off" else ":".join(parts[2:]))
                    reschedule(ctx.application)
                elif sub == "pc":
                    S.set("offsite_max_age_h", None if parts[2] == "off" else int(parts[2]))
                elif sub == "vauto":
                    S.set("verify_backups", not CFG.get("verify_backups", True))
                    reschedule(ctx.application)
                elif sub == "daily":
                    S.set("backup_send_daily", not CFG.get("backup_send_daily"))
                elif sub == "alerts":
                    S.set("backup_alerts", not CFG.get("backup_alerts", True))
                elif sub == "refresh":
                    await asyncio.to_thread(backups.watched_status, None, True)
                note = _('Обновлено') if sub == "refresh" else _('Сохранено')
                await edit(update, *backup_view())
        elif cmd == "net":
            text = await asyncio.to_thread(network.net_text, CFG, HIST, ACT._helper)
            if len(parts) > 1:
                await edit(update, text, net_kb())
            else:
                await send(update, text, net_kb())
        elif cmd == "pkg":
            if parts[1] == "refresh":  # apt-get update бывает долгим, кнопку отпускаем сразу
                await q.answer(_('Обновляю списки пакетов, это может занять минуту…'))
                answered = True
            note = await handle_pkg(update, parts)
        elif cmd == "up":
            sub = parts[1]
            if sub == "check":
                await asyncio.to_thread(check_updates)
                await edit(update, *update_view())
                note = _('Не удалось проверить') if LATEST.get("error") else _('Проверено')
            elif not actions_on():
                note = _('Действия выключены в config.yaml')
            elif sub == "go":
                sha = parts[2]
                if not updater.SHA_RE.match(sha):
                    note = _('Неверная версия')
                else:
                    token = ACT.ask("update", sha, update.effective_chat.id)
                    changes = "".join(f"\n  • {html.escape(c)}" for c in LATEST.get("changes") or [])
                    await send(update, _('⬆️ Обновить бота до <b>{sha}</b>?{changes}\n'
                                         '\n'
                                         'Сначала тесты, потом перезапуск бота (около минуты без ответа). Если '
                                         'что-то пойдёт не так, вернётся прежняя версия.',
                                         sha=sha[:7], changes=changes), confirm_kb(token))
            elif sub == "prune":
                ok, out = await asyncio.to_thread(ACT._helper, "bak-list")
                try:
                    baks = json.loads(out) if ok else None
                except ValueError:
                    baks = None
                if baks is None:
                    await send(update, _('Не удалось получить список: {escape}', escape=html.escape(out)))
                elif len(baks) <= 2:
                    note = _('Старых копий: {len}, удалять нечего', len=len(baks))
                else:
                    extra = baks[2:]
                    size = sum(b["size"] for b in extra)
                    token = ACT.ask("prunebak", "2", update.effective_chat.id)
                    names = "\n".join(f"  • {html.escape(Path(b['path']).name)} ({checks.fmt_bytes(b['size'])})"
                                      for b in extra)
                    await send(update, _('🧽 Удалить {len} старых копий кода и освободить {fmt_bytes}? Две самые '
                                         'новые останутся.\n'
                                         '{names}',
                                         len=len(extra), fmt_bytes=checks.fmt_bytes(size), names=names),
                               confirm_kb(token))
        elif cmd == "act":
            if not actions_on():
                note = _('Действия выключены в config.yaml')
            else:
                await handle_act(update, parts)
        elif cmd in ("do", "no"):
            note = await handle_confirm(update, cmd, parts[1])
        elif cmd == "name":
            if parts[1] == "reset":
                S.unset("server_name")
                note = _('Имя как в config.yaml: {name}', name=host())
            await edit(update, *name_view())
        elif cmd == "lang":
            if parts[1] in i18n.LANGS:
                await set_language(update, ctx, parts[1])
                note = _('Сохранено')
            await edit(update, settings_text(), settings_kb())
        elif cmd == "remind":
            S.set("remind_min", int(parts[1]))
            await edit(update, *remind_view())
        elif cmd == "summary":
            S.set("daily_summary", None if parts[1] == "off" else ":".join(parts[1:]))
            reschedule(ctx.application)
            await edit(update, *summary_view())
        elif cmd == "inc":
            text = await asyncio.to_thread(incidents.incidents_text, INC, CFG)
            if len(parts) > 1:
                await edit(update, text, inc_kb())
            else:
                await send(update, text, inc_kb())
        elif cmd == "wk":
            if parts[1] == "now":
                await q.answer(_('Собираю отчёт…'))
                answered = True
                await send_weekly(ctx.bot, [update.effective_chat.id])
            else:
                S.set("weekly_report", None if parts[2] == "off" else ":".join(parts[2:]))
                reschedule(ctx.application)
                note = _('Сохранено')
                await edit(update, *summary_view())
        if cmd in ("th", "thd", "int", "tg", "quiet", "remind", "summary", "dk", "sec", "secthr", "abthr", "abh", "sm", "bk") and note is None:
            note = _('Сохранено')
    except Exception:
        log.exception("Ошибка кнопки %s", data)
        note = _('Ошибка, подробности в журнале бота')
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
        for pid, name, cpu, __ in rows:
            btns.append([Btn(f"⛔ {name[:20]} (PID {pid}, CPU {cpu:.0f}%)", callback_data=f"act:kill:{pid}")])
        await send(update, _('⛔ <b>Какой процесс остановить?</b> (SIGTERM, процесс может завершиться не сразу)'),
                   InlineKeyboardMarkup(btns) if btns else None)
    elif sub == "ctr":
        name = parts[2]
        token = ACT.ask("ctr", name, chat_id)
        await send(update, _('♻️ Перезапустить контейнер <b>{escape}</b>?\n'
                             'Бот внутри будет недоступен несколько секунд.',
                             escape=html.escape(name)), confirm_kb(token))
    elif sub == "svc":
        name = parts[2]
        if name not in action_services():
            await send(update, _('Служба {escape} не входит в action_services в config.yaml', escape=html.escape(name)))
            return
        token = ACT.ask("svc", name, chat_id)
        await send(update, _('♻️ Перезапустить службу <b>{escape}</b>?', escape=html.escape(name)), confirm_kb(token))
    elif sub == "kill":
        pid = int(parts[2])
        try:
            p = await asyncio.to_thread(lambda: actions.psutil.Process(pid))
            name, created = p.name(), p.create_time()
            cmdline = " ".join(p.cmdline())[:150]
        except Exception:
            await send(update, _('Процесс уже завершился'))
            return
        token = ACT.ask("kill", str(pid), chat_id, name=name, created=created)
        await send(update, _('⛔ Остановить процесс <b>{escape}</b> (PID {pid})?\n'
                             '<code>{escape2}</code>',
                             escape=html.escape(name), pid=pid, escape2=html.escape(cmdline)), confirm_kb(token))
    elif sub == "clean":
        est = await asyncio.to_thread(ACT.cleanup_estimate)
        token = ACT.ask("clean", "", chat_id)
        await send(update, _('🧹 <b>Очистить место?</b>\n'
                             'Удалятся неиспользуемые Docker-образы и кэш сборки, журнал systemd сократится до 200 '
                             'МБ, очистится кэш apt. Контейнеры и их данные не трогаются.\n'
                             '\n'
                             'Сколько можно освободить:\n'
                             '<pre>{escape}</pre>',
                             escape=html.escape(est)),
                   confirm_kb(token))
    elif sub == "ban":
        await ask_ban(update, ":".join(parts[2:]))
    elif sub == "unban":
        ip = ":".join(parts[2:])
        token = ACT.ask("unban", ip, chat_id)
        await send(update, _('Снять блокировку с <b>{escape}</b>?', escape=html.escape(ip)), confirm_kb(token))
    elif sub == "banned":
        ok, out = await asyncio.to_thread(ACT._helper, "banned-list")
        try:
            ips = json.loads(out) if ok else None
        except ValueError:
            ips = None
        if ips is None:
            await send(update, _('Не удалось получить список: {escape}', escape=html.escape(out)))
        elif not ips:
            await send(update, _('🚫 Бот пока никого не блокировал.\nЗаблокировать вручную: <code>/ban IP</code>'))
        else:
            def note(ip: str) -> str:
                if ip not in STATE.autobans:
                    return ""
                until = STATE.autobans[ip]
                return _(' (автобан навсегда)') if not until else _(' (автобан до {local_dt})',
                                                                    local_dt=local_dt(until))
            btns = [[b] for b in (act_btn(_('✅ Разблокировать {ip}',
                                            ip=ip), f"act:unban:{ip}") for ip in ips[:20]) if b]
            await send(update, _('🚫 <b>Заблокированные ботом IP ({len})</b>\n', len=len(ips))
                       + "\n".join(html.escape(i + note(i)) for i in ips[:50]),
                       InlineKeyboardMarkup(btns) if btns else None)
    elif sub == "reboot":
        token = ACT.ask("reboot", "", chat_id)
        await send(update, _('🔄 <b>Перезагрузить сервер?</b>\nСервер и все контейнеры будут недоступны 1–2 минуты.'),
                   confirm_kb(token, _('Да, перезагрузить')))


async def handle_pkg(update: Update, parts: list[str]) -> str | None:
    sub = parts[1]
    if sub == "view":
        view = await asyncio.to_thread(pkg_view)
        await (send if len(parts) > 2 else edit)(update, *view)
    elif sub == "refresh":
        ok, out = await asyncio.to_thread(ACT._helper, "apt-refresh", timeout=600)
        await edit(update, *(await asyncio.to_thread(pkg_view)))
        if not ok:
            await send(update, _('Не удалось обновить списки пакетов: {escape}', escape=html.escape(out[-500:])))
    elif sub == "go":
        mode = parts[2] if len(parts) > 2 else ""
        if not actions_on():
            return _('Действия выключены в config.yaml')
        if mode not in pkgupdates.MODES:
            return _('неизвестное действие')
        if pkgupdates.running():
            return _('установка обновлений уже идёт')
        pkgs = await asyncio.to_thread(pkgupdates.upgradable) or []
        chosen = pkgupdates.installable(pkgs, mode)
        if not chosen:
            return _('Нечего устанавливать')
        token = ACT.ask("apt", mode, update.effective_chat.id)
        await send(update, _('📦 Установить {what}: {len} пак.?\n'
                             '<code>{names}</code>\n'
                             '\n'
                             'Установка идёт в фоне, обычно 1–5 минут. Службы, которые используют обновлённые '
                             'библиотеки, перезапустятся автоматически (это может быть Docker с контейнерами и сам '
                             'бот), поэтому бот может ненадолго замолчать. Настройки пакетов не меняются, пакеты не '
                             'удаляются. Сервер сам не перезагружается: если перезагрузка нужна, бот напишет.',
                             what=pkgupdates.mode_text(mode), len=len(chosen),
                             names=html.escape(pkgupdates.names_short([p.name for p in chosen], 20))),
                   confirm_kb(token, N_('✅ Да, установить')))
    return None


async def ask_ban(update: Update, ip: str) -> None:
    import ipaddress
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        await send(update, _('Это не IP-адрес: {escape}', escape=html.escape(ip)))
        return
    if a.is_private or a.is_loopback:
        await send(update, _('Локальные адреса не блокирую'))
        return
    note = ""
    if ip in STATE.known_ips:
        note = _('\n⚠️ С этого IP раньше был успешный вход по SSH. Если это вы, не блокируйте.')
    token = ACT.ask("ban", ip, update.effective_chat.id)
    await send(update, _('🚫 Заблокировать все входящие соединения с <b>{escape}</b> (ufw)?{note}\n'
                         'IP с открытой сейчас SSH-сессией помощник не заблокирует, чтобы не отрезать вас.',
                         escape=html.escape(ip), note=note),
               confirm_kb(token))


async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    if not actions_on():
        await send(update, _('Действия выключены (actions_enabled: false в config.yaml)'))
        return
    if not ctx.args:
        await send(update, _('Формат: /ban 203.0.113.5. Список заблокированных — «Настройки → 🛡 Безопасность».'))
        return
    await ask_ban(update, ctx.args[0])


async def cmd_net(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await send(update, await asyncio.to_thread(network.net_text, CFG, HIST, ACT._helper), net_kb())


def net_kb() -> InlineKeyboardMarkup:
    return kb([Btn(_('🔄 Обновить'), callback_data="net:r"), Btn(_('📈 График'), callback_data="chart:24h")])


async def handle_confirm(update: Update, cmd: str, token: str) -> str | None:
    chat_id = update.effective_chat.id
    p = ACT.take(token, chat_id)
    if p is None:
        await edit(update, _('⌛ Подтверждение устарело, начните заново'))
        return None
    if cmd == "no":
        ACT.drop(token)
        await edit(update, _('✖️ Отменено: {describe}', describe=actions.describe(p)))
        return _('Отменено')
    if p.kind == "reboot" and p.stage == 1:
        p.stage = 2
        p.expires = time.time() + 60
        await edit(update, _('⚠️ <b>Точно перезагрузить сервер?</b> Это второе и последнее подтверждение.'),
                   confirm_kb(token, _('🔄 Перезагрузить сейчас')))
        return None
    ACT.drop(token)
    await edit(update, _('⏳ Выполняю: {describe}…', describe=actions.describe(p)))
    ok, out = await asyncio.to_thread(ACT.execute, p)
    if p.kind == "apt" and ok:
        await edit(update, _('⏳ Установка обновлений запущена ({what}). Результат пришлю, когда закончится, '
                             'обычно через 1–5 минут.', what=pkgupdates.mode_text(p.arg)))
        return None
    if p.kind == "update" and ok:
        await edit(update, _('⏳ Обновление до {arg} запущено. Сначала тесты новой версии, потом перезапуск. '
                             'Результат пришлю через 1–3 минуты.',
                             arg=p.arg[:7]))
        return None
    out = out.strip()[-1500:] or (_('готово') if ok else _('ошибка без вывода'))
    icon = "✅" if ok else "❌"
    await edit(update, f"{icon} {html.escape(actions.describe(p))}\n<pre>{html.escape(out)}</pre>")
    return None


# ---------- Фоновые задачи ----------

async def broadcast(bot, text: str, markup=None, silent: bool = False) -> None:
    for chat_id in CFG["allowed_chats"]:
        try:
            await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                   disable_notification=silent)
        except (NetworkError, TimedOut):
            STATE.net_fail_at = time.time()  # ошибки в логах контейнеров рядом по времени — тот же сбой связи
            log.exception("Нет связи с Telegram, сообщение в %s не отправлено", chat_id)
        except Exception:
            log.exception("Не удалось отправить сообщение в %s", chat_id)


async def monitor_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        alerts = await asyncio.to_thread(checks.step, CFG, STATE, None, None, HIST, INC)
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
        if not ok:
            STATE.net_fail_at = time.time()
    if STATE.maint_until and not STATE.in_maintenance(time.time()):
        STATE.maint_until = 0.0  # обслуживание кончилось по времени
        STATE.save()
        await broadcast(ctx.bot, maint_end_text())
    try:
        await run_autoban(ctx.bot)
    except Exception:
        log.exception("Ошибка автобана")
    try:
        await report_update(ctx.bot)
    except Exception:
        log.exception("Не удалось сообщить результат обновления")
    try:
        await report_apt(ctx.bot)
    except Exception:
        log.exception("Не удалось сообщить результат установки пакетов")
    alerts = [a for a in alerts if a.send != "journal"]  # деплой и т.п.: только в журнале инцидентов
    if CFG.get("group_alerts", True) and len(alerts) > 3:
        await broadcast(ctx.bot, group_text(alerts), group_kb())
        return
    for a in alerts:
        markup = alert_kb(a.key, a.kind in ("alert", "remind")) if a.kind in ("alert", "remind") or (a.key or "").startswith(("log:", "brute:")) else None
        if a.key == "apt:updates":
            markup = kb([Btn(_('📦 Показать и установить'), callback_data="pkg:view:new")])
        text = f"<b>{html.escape(host())}</b>\n{html.escape(a.text)}"
        if a.send == "quiet":
            await broadcast(ctx.bot, text, markup, silent=True)
        else:
            await broadcast(ctx.bot, text, markup)


def ban_log(ip: str, kind: str, ok: bool, out: str) -> None:
    ACT.log(actions.Pending(kind, ip), ok, out)


def autoban_step(now: float) -> tuple[list[tuple[str, int]], list[str]]:
    """Снять истёкшие автобаны и заблокировать новых подборщиков. ([(ip, попыток)], [снятые ip])."""
    lifted = []
    changed_unban = False
    if not actions_on():
        return [], lifted
    for ip in security.expired_autobans(STATE, now):
        ok, out = ACT._helper("unban-ip", ip, timeout=30)
        ban_log(ip, "unban", ok, _('автобан истёк: ') + out)
        if ok or out == _(actions.HELPER_ERRORS["not banned by monitor-bot"]):  # сняли или правила уже нет
            STATE.autobans.pop(ip, None)
            if ok:
                lifted.append(ip)
        else:  # не забываем бан, иначе он стал бы вечным: повторим через час
            STATE.autobans[ip] = now + security.AUTOBAN_RETRY
            changed_unban = True
            log.warning("Не удалось снять автобан %s: %s", ip, out)
    banned = []

    def ban(target: str, n: int, why: str) -> None:
        ok, out = ACT._helper("ban-ip", target, timeout=30)
        ban_log(target, "ban", ok, _('автобан ({why}): {out}', why=why, out=out))
        if ok and not out.startswith("already banned"):  # ручной бан не превращаем во временный
            hours = security.ban_hours(CFG, STATE, target, now)  # повторно за 30 дней — навсегда
            STATE.autobans[target] = now + hours * 3600 if hours else 0
            security.remember_ban(STATE, target, now)
            banned.append((target, n))
        else:  # уже заблокирован вручную или помощник отказал — час не трогаем
            STATE.autoban_skip[target] = now
            if not ok:
                log.warning("Автобан %s не удался: %s", target, out)
    for ip, n in security.autoban_candidates(CFG, STATE, now):
        ban(ip, n, _('{n} попыток', n=n))
    for net, ips in security.net_candidates(CFG, STATE, now):  # после IP: свежезабаненные IP тоже считаются
        ban(net, len(ips), _('подборщики из одной подсети: ') + ", ".join(ips[:5]))
    STATE.autoban_skip = {ip: ts for ip, ts in STATE.autoban_skip.items() if now - ts < security.AUTOBAN_RETRY}
    if banned or lifted or changed_unban:
        STATE.save()
    return banned, lifted


def autoban_line(target: str, n: int) -> str:
    until = STATE.autobans.get(target)
    if until == 0:
        repeat = (STATE.ban_history.get(target) or [0])[0] > 1
        dur = _('навсегда') + (_(' (уже банили)') if repeat else "")
    else:
        hours = round(((until or 0) - time.time()) / 3600)
        dur = security.ban_duration_text(max(hours, 1))
    if "/" in target:
        return _('подсеть {target} {dur} ({n} IP подбирали пароль)', target=target, dur=dur, n=n)
    return _('{target} {dur} ({n} попыток за час)', target=target, dur=dur, n=n)


async def run_autoban(bot) -> None:
    now = time.time()
    banned, __ = await asyncio.to_thread(autoban_step, now)
    if not banned:
        return
    text = _('🚫 Автобан: ') + "; ".join(autoban_line(t, n) for t, n in banned)
    try:
        await asyncio.to_thread(INC.event, "autoban", text, now)
    except Exception:
        log.exception("Журнал инцидентов: автобан")
    if STATE.muted(now) or checks.in_quiet_hours(CFG, now):
        return  # не критично: видно в журнале, в «Заблокированных IP» и в недельном отчёте
    btns = [[b] for b in (act_btn(_('✅ Разблокировать {ip}', ip=ip), f"act:unban:{ip}") for ip, __ in banned[:5]) if b]
    await broadcast(bot, f"<b>{html.escape(host())}</b>\n{html.escape(text)}", InlineKeyboardMarkup(btns) if btns else None)


async def backup_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        path, size, __ = await asyncio.to_thread(backups.make_backup, CONFIG_PATH, S.dir,
                                                int(CFG.get("backup_keep") or 14))
        log.info("Копия бота: %s (%s байт)", path.name, size)
    except Exception as e:
        log.exception("Не удалось сделать копию бота")
        await broadcast(ctx.bot, _('<b>{escape}</b>\n'
                                   '❌ Не удалось сделать копию бота: {escape2}',
                                   escape=html.escape(host()), escape2=html.escape(str(e))))
        return
    if CFG.get("backup_send_daily"):
        try:
            errors = await send_backups(ctx.bot, CFG["allowed_chats"], own=path)
        except Exception as e:
            log.exception("Не удалось отправить копии")
            errors = [str(e)]
        if errors:
            await broadcast(ctx.bot, _('❌ Не все копии удалось отправить:\n') + html.escape("\n".join(errors)))


async def run_verify() -> str:
    results, err = await asyncio.to_thread(backups.verify_all, S.dir)
    STATE.verify_last = {"ts": time.time(), "results": results, "err": err}
    await asyncio.to_thread(STATE.save)
    return backups.verify_text(results, err, local_dt(STATE.verify_last["ts"]))


async def verify_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Раз в неделю: копии читаются целиком? Пишем, только если что-то не так."""
    try:
        text = await run_verify()
    except Exception as e:
        log.exception("Ошибка проверки копий")
        await broadcast(ctx.bot, _('<b>{escape}</b>\n'
                                   '❌ Не удалось проверить копии: {escape2}',
                                   escape=html.escape(host()), escape2=html.escape(str(e))))
        return
    v = STATE.verify_last
    log.info("Проверка копий: %s", "есть ошибки" if backups.verify_failed(v["results"]) else "ok")
    if backups.verify_failed(v["results"]) or v.get("err"):
        await broadcast(ctx.bot, f"<b>{html.escape(host())}</b>\n{text}")


def weekly_extra() -> list[str]:
    lines = [_('💾 Бэкапы:'), "  " + verify_line(), "  " + backups.offsite_text(CFG, time.time())]
    ok, out = ACT._helper("banned-list", timeout=20)
    try:
        n = len(json.loads(out)) if ok else None
    except ValueError:
        n = None
    if n is not None:
        lines.append(_('🚫 Заблокировано IP ботом: {n}', n=n))
    lines += security.ssh_week_lines(STATE, time.time())
    return lines


async def send_weekly(bot, chat_ids: list[int]) -> None:
    def build():
        now = time.time()
        text = incidents.weekly_text(CFG, INC, HIST, now, weekly_extra())
        try:
            png = history.chart_png(HIST, "7d", checks.tz(CFG), host(), now)
        except Exception:
            log.exception("Не удалось построить график для отчёта")
            png = None
        return text, png
    text, png = await asyncio.to_thread(build)
    text = f"<b>{html.escape(host())}</b>\n{text}"
    for c in chat_ids:
        try:
            await bot.send_message(c, text, parse_mode=ParseMode.HTML)
            if png:
                await bot.send_photo(c, photo=png, caption=_('📈 За 7 дней'))
        except Exception:
            log.exception("Не удалось отправить недельный отчёт в %s", c)


async def weekly_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await send_weekly(ctx.bot, CFG["allowed_chats"])


async def summary_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = await asyncio.to_thread(checks.summary_text, CFG, STATE, None, HIST)
    await broadcast(ctx.bot, text, status_kb())


def reschedule(app: Application) -> None:
    jq = app.job_queue
    for name in ("monitor", "summary", "backup", "verify", "weekly"):
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
    if CFG.get("weekly_report"):  # понедельник (в PTB 1 = понедельник)
        h, m = map(int, CFG["weekly_report"].split(":"))
        jq.run_daily(weekly_job, time=dt.time(h, m, tzinfo=checks.tz(CFG)), days=(1,), name="weekly")
    if CFG.get("verify_backups", True):  # воскресенье (в PTB 0 = воскресенье), после ночных бэкапов
        jq.run_daily(verify_job, time=dt.time(4, 30, tzinfo=checks.tz(CFG)), days=(0,), name="verify")
    log.info("Проверка каждые %s с, сводка: %s", interval, CFG.get("daily_summary") or "выкл")


COMMANDS = [("status", N_('📊 Состояние сервера')), ("procs", N_('🔝 Процессы по CPU')), ("mem", N_('💾 Процессы по памяти')),
            ("services", N_('⚙️ Службы и контейнеры')), ("net", N_('🌐 Сеть и диск')), ("alerts", N_('🚨 Активные алерты')), ("incidents", N_('📒 Журнал инцидентов')), ("chart", N_('📈 График за сутки')),
            ("logs", N_('📜 Логи контейнера')), ("probe", N_('🌐 Сайты и порты')), ("backup", N_('🗄 Резервные копии')),
            ("actions", N_('🧰 Действия с сервером')), ("updates", N_('📦 Обновления пакетов')), ("settings", N_('🛠 Настройки')), ("name", N_('🏷 Имя сервера')), ("mute", N_('🔕 Выключить уведомления на 1 ч')),
            ("unmute", N_('🔔 Включить уведомления')),
            ("maint", N_('🔧 Обслуживание 30 мин (/maint 2h, /maint off)',
                         )), ("lang", LANG_BTN), ("start", N_('👋 Клавиатура и помощь'))]


async def register_commands(bot) -> None:
    """Кнопка «Меню» в Telegram: список команд на текущем языке."""
    try:
        await bot.set_my_commands([BotCommand(c, _(d)) for c, d in COMMANDS])
    except Exception:
        log.exception("Не удалось зарегистрировать команды")


async def on_startup(app: Application) -> None:
    await register_commands(app.bot)
    reboot_msg = checks.check_reboot(STATE)
    parts = []
    if CFG.get("notify_on_start", True):
        parts.append(_('▶️ Мониторинг запущен на {escape}', escape=html.escape(host())))
    if reboot_msg:
        parts.append(reboot_msg)
    if STATE.load_error:
        parts.append(_('⚠️ {load_error}. Состояние начато заново: алерты, известные IP и сроки автобанов сброшены. '
                       'Заблокированные IP видны в «Безопасность → Заблокированные IP».',
                       load_error=STATE.load_error))
    if parts:
        for chat_id in CFG["allowed_chats"]:
            try:
                await app.bot.send_message(chat_id, "\n".join(parts), reply_markup=main_kb())
            except Exception:
                log.exception("Не удалось отправить сообщение о запуске")


def build_app() -> Application:
    # concurrent_updates: долгое действие (очистка места, проверка копий) не задерживает ответы на другие кнопки
    app = Application.builder().token(CFG["token"]).post_init(on_startup).concurrent_updates(8).build()
    for name, fn in [("start", cmd_start), ("help", cmd_start), ("menu", cmd_start), ("status", cmd_status),
                     ("procs", cmd_procs), ("mem", cmd_mem), ("services", cmd_services),
                     ("alerts", cmd_alerts), ("settings", cmd_settings), ("mute", cmd_mute), ("logs", cmd_logs), ("chart", cmd_chart), ("actions", cmd_actions),
                     ("probe", cmd_probe), ("backup", cmd_backup), ("updates", cmd_updates), ("incidents", cmd_incidents), ("net", cmd_net), ("ban", cmd_ban),
                     ("unmute", cmd_unmute), ("maint", cmd_maint), ("lang", cmd_lang), ("name", cmd_name)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    reschedule(app)
    return app


def main() -> None:
    build_app().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
