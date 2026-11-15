"""Telegram-бот мониторинга сервера.

Запуск:  python bot.py [путь_к_config.yaml]
"""
from __future__ import annotations

import asyncio
import html
import logging
import sys
from pathlib import Path

import yaml
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

import checks

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("monitor_bot")

CONFIG_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("config.yaml")


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not cfg.get("token") or "ВАШ" in str(cfg.get("token")):
        sys.exit(f"Укажите token в {CONFIG_PATH}")
    cfg["allowed_chats"] = [int(x) for x in (cfg.get("allowed_chats") or [])]
    return cfg


CFG = load_config()
STATE = checks.AlertState()


def allowed(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat) and chat.id in CFG["allowed_chats"]


async def reply(update: Update, text: str) -> None:
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not allowed(update):
        # Подсказка для первичной настройки: показываем chat_id, больше ничего
        await reply(update, f"Доступ закрыт. Ваш chat_id: <code>{chat_id}</code>\n"
                            "Добавьте его в allowed_chats в config.yaml и перезапустите бота.")
        return
    await reply(update,
                "👋 Бот мониторинга сервера.\n\n"
                "/status — сводка по ресурсам\n"
                "/procs — топ процессов по CPU\n"
                "/mem — топ процессов по RAM\n"
                "/services — состояние служб и процессов\n"
                "/alerts — активные алерты")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await reply(update, await asyncio.to_thread(checks.status_text, CFG))


async def cmd_procs(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await reply(update, await asyncio.to_thread(checks.top_processes, 10, "cpu"))


async def cmd_mem(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await reply(update, await asyncio.to_thread(checks.top_processes, 10, "mem"))


async def cmd_services(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await reply(update, await asyncio.to_thread(checks.services_text, CFG))


async def cmd_alerts(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        active = sorted(STATE.active)
        await reply(update, "🚨 Активные алерты:\n" + html.escape("\n".join(active)) if active else "✅ Активных алертов нет")


async def monitor_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        messages = await asyncio.to_thread(checks.step, CFG, STATE)
    except Exception:
        log.exception("Ошибка проверки")
        return
    if not messages:
        return
    text = f"<b>{checks.platform.node()}</b>\n" + "\n".join(html.escape(m) for m in messages)
    for chat_id in CFG["allowed_chats"]:
        try:
            await ctx.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
        except Exception:
            log.exception("Не удалось отправить алерт в %s", chat_id)


async def on_startup(app: Application) -> None:
    if CFG.get("notify_on_start", True):
        for chat_id in CFG["allowed_chats"]:
            try:
                await app.bot.send_message(chat_id, f"▶️ Мониторинг запущен на {checks.platform.node()}")
            except Exception:
                log.exception("Не удалось отправить сообщение о запуске")


def main() -> None:
    app = Application.builder().token(CFG["token"]).post_init(on_startup).build()
    for name, fn in [("start", cmd_start), ("help", cmd_start), ("status", cmd_status),
                     ("procs", cmd_procs), ("mem", cmd_mem), ("services", cmd_services),
                     ("alerts", cmd_alerts)]:
        app.add_handler(CommandHandler(name, fn))
    interval = int(CFG.get("check_interval_sec", 60))
    app.job_queue.run_repeating(monitor_job, interval=interval, first=10)
    log.info("Бот запущен, проверка каждые %s сек", interval)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
