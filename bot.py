# bot.py
import os
import asyncio
import logging
import signal

from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.exceptions import TelegramConflictError

# ---------- базовая настройка логов ----------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("cryptosignals-bot")


# ---------- простые хендлеры ----------
async def cmd_start(message: Message):
    await message.answer("Я на связи ✅. Жду событий и буду присылать сообщения сюда.")


async def any_text(message: Message):
    # Заглушка: тут может быть твоя логика
    await message.answer("Ок, принял.")


# ---------- health endpoint для Koyeb ----------
async def health(request: web.Request):
    return web.json_response({"status": "ok"})


def create_health_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", health)
    return app


# ---------- graceful shutdown ----------
def install_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event):
    def _handle_sigterm():
        log.info("Получен SIGTERM — останавливаемся…")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_sigterm)
        except NotImplementedError:
            # Windows: сигналов может не быть — просто пропустим
            pass


# ---------- основной запуск ----------
async def run_bot():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Переменная окружения BOT_TOKEN не задана")

    bot = Bot(token=token, parse_mode="HTML")
    dp = Dispatcher()

    # Регистрация маршрутов
    dp.message.register(cmd_start, F.text == "/start")
    dp.message.register(any_text, F.text)

    # Внутренний веб-сервер здоровья (для Koyeb)
    health_app = create_health_app()
    runner = web.AppRunner(health_app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
    await site.start()
    log.info("Health server on :%s", os.getenv("PORT", "8000"))

    # ВАЖНО: снимаем вебхук и очищаем накопившиеся апдейты
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        log.warning("Не удалось снять вебхук: %s", e)

    # Луп перезапуска при конфликте getUpdates (если вдруг параллельный инстанс)
    backoff = 1.0
    max_backoff = 15.0

    while True:
        try:
            log.info("Start polling")
            await dp.start_polling(bot, allowed_updates=None)
            # Если polling вышел без исключения — значит нас корректно остановили
            break
        except TelegramConflictError:
            # Другой инстанс уже забрал long polling. Подождём и попробуем снова.
            log.error(
                "Conflict: другой инстанс бота уже запущен (getUpdates). "
                "Проверяю ещё раз через %.1f сек…", backoff
            )
            await asyncio.sleep(backoff)
            backoff = min(max_backoff, backoff * 1.5)
        except Exception as e:
            log.exception("Неожиданная ошибка в polling: %s", e)
            await asyncio.sleep(3)

    # Чистое закрытие
    await runner.cleanup()
    log.info("Bot stopped cleanly.")


def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    stop_event = asyncio.Event()
    install_signal_handlers(loop, stop_event)

    async def _main():
        # Запускаем бота и параллельно ждём сигнала остановки
        bot_task = asyncio.create_task(run_bot())

        # Ждём, пока либо bot_task завершится, либо придёт SIGTERM
        done, pending = await asyncio.wait(
            {bot_task, asyncio.create_task(stop_event.wait())},
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Если пришёл SIGTERM — аккуратно отменим polling
        if stop_event.is_set():
            log.info("Останавливаем приложение…")
            for t in pending:
                t.cancel()
            # Дадим задачам аккуратно завершиться
            await asyncio.gather(*pending, return_exceptions=True)

    try:
        loop.run_until_complete(_main())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
