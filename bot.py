from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Awaitable, Optional, TypeVar

from telegram import (
    Bot,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import (
    BadRequest,
    Conflict,
    Forbidden,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from advent_bot.config import CONFIG
from advent_bot.db import UserRepository
from advent_bot.initial_post import InitialPostStorage
from advent_bot.posts import PostStorage, ScheduledPost

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

_T = TypeVar("_T")
_TRANSIENT_ERRORS = (TimedOut, NetworkError, RetryAfter)
_IGNORED_BAD_REQUEST_SNIPPETS = ("query is too old", "query id is invalid", "message is not modified")


class PromptTracker:
    def __init__(self) -> None:
        self._prompt_ids: dict[int, str] = {}
        self._start_state: dict[int, str] = {}

    def add(self, message_id: int, kind: str) -> None:
        self._prompt_ids[message_id] = kind

    def get(self, message_id: int) -> str | None:
        return self._prompt_ids.get(message_id)

    def consume(self, message_id: int) -> None:
        self._prompt_ids.pop(message_id, None)

    def set_start_state(self, user_id: int, state: str) -> None:
        self._start_state[user_id] = state

    def get_start_state(self, user_id: int) -> str | None:
        return self._start_state.get(user_id)

    def clear_start_state(self, user_id: int) -> None:
        self._start_state.pop(user_id, None)


def get_storage(application: Application) -> PostStorage:
    return application.bot_data["post_storage"]


def get_user_repo(application: Application) -> UserRepository:
    return application.bot_data["user_repo"]


def get_prompt_tracker(application: Application) -> PromptTracker:
    return application.bot_data["prompt_tracker"]


def get_initial_post_store(application: Application) -> InitialPostStorage:
    return application.bot_data["initial_post_store"]


def _should_ignore_bad_request(message: str, snippets: tuple[str, ...]) -> bool:
    lowered = message.lower()
    return any(snippet in lowered for snippet in snippets)


async def _guard_telegram_call(
    awaitable: Awaitable[_T],
    action: str,
    ignored_bad_request_messages: tuple[str, ...] | None = None,
) -> Optional[_T]:
    try:
        return await awaitable
    except _TRANSIENT_ERRORS as exc:
        logger.warning("Transient error while %s: %s", action, exc, exc_info=True)
    except Forbidden as exc:
        logger.info("Forbidden while %s: %s", action, exc)
    except BadRequest as exc:
        message = str(exc)
        if ignored_bad_request_messages and _should_ignore_bad_request(message, ignored_bad_request_messages):
            logger.info("Ignoring BadRequest while %s: %s", action, message)
            return None
        logger.warning("BadRequest while %s: %s", action, exc, exc_info=True)
    except TelegramError as exc:
        logger.error("Unexpected TelegramError while %s: %s", action, exc, exc_info=True)
    return None


async def safe_send_message(
    bot: Bot,
    chat_id: int,
    text: str,
    **kwargs,
) -> Optional[Message]:
    return await _guard_telegram_call(
        bot.send_message(chat_id=chat_id, text=text, **kwargs),
        action=f"sending message to {chat_id}",
    )


async def safe_reply_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    **kwargs,
) -> Optional[Message]:
    if update.message:
        return await _guard_telegram_call(
            update.message.reply_text(text, **kwargs),
            action="replying with message",
        )
    if update.effective_chat:
        kwargs.setdefault("allow_sending_without_reply", True)
        return await safe_send_message(context.bot, update.effective_chat.id, text, **kwargs)
    return None


async def safe_edit_message_text(
    query: CallbackQuery,
    text: str,
    **kwargs,
) -> Optional[Message]:
    return await _guard_telegram_call(
        query.edit_message_text(text=text, **kwargs),
        action="editing message text",
        ignored_bad_request_messages=_IGNORED_BAD_REQUEST_SNIPPETS,
    )


async def safe_answer_callback_query(
    query: CallbackQuery,
    text: str | None = None,
    **kwargs,
) -> bool:
    action = f"answering callback query {query.id}"
    try:
        await query.answer(text=text, **kwargs)
        return True
    except _TRANSIENT_ERRORS as exc:
        logger.warning("Transient error while %s: %s", action, exc, exc_info=True)
    except Forbidden as exc:
        logger.info("Forbidden while %s: %s", action, exc)
    except BadRequest as exc:
        message = str(exc)
        if _should_ignore_bad_request(message, _IGNORED_BAD_REQUEST_SNIPPETS):
            logger.info("Ignoring stale callback query %s: %s", query.id, message)
            return False
        logger.warning("BadRequest while %s: %s", action, exc, exc_info=True)
    except TelegramError as exc:
        logger.error("Unexpected TelegramError while %s: %s", action, exc, exc_info=True)
    return False


async def safe_copy_message(
    bot: Bot,
    chat_id: int,
    from_chat_id: int,
    message_id: int,
    **kwargs,
) -> Optional[Message]:
    return await _guard_telegram_call(
        bot.copy_message(
            chat_id=chat_id,
            from_chat_id=from_chat_id,
            message_id=message_id,
            **kwargs,
        ),
        action=f"copying message {message_id} to {chat_id}",
    )


async def send_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except TelegramError as exc:  # pragma: no cover - best effort
        logger.debug("Failed to send typing action to %s: %s", chat_id, exc)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling update %s", update, exc_info=context.error)
    err = context.error
    if err is None:
        return

    if isinstance(err, Conflict):
        logger.error("Another bot instance is running. Stopping polling: %s", err)
        if context.application:
            await context.application.stop()
        return

    if isinstance(err, _TRANSIENT_ERRORS):
        logger.warning("Transient Telegram/network error: %s", err)
        return

    if isinstance(err, Forbidden):
        logger.info("User blocked the bot or is deactivated: %s", err)
        return

    if isinstance(err, BadRequest) and _should_ignore_bad_request(str(err), _IGNORED_BAD_REQUEST_SNIPPETS):
        logger.info("Got an old/invalid callback query: %s", err)
        return

    if isinstance(err, TelegramError):
        logger.error("Unexpected TelegramError: %s", err, exc_info=True)
        return

    logger.exception("Unexpected non-Telegram error", exc_info=True)


async def send_initial_post_to_user(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    init_store = get_initial_post_store(context.application)
    initial_post = init_store.load()
    if initial_post and CONFIG.admin_chat_id is not None:
        await safe_copy_message(
            context.bot,
            chat_id=chat_id,
            from_chat_id=CONFIG.admin_chat_id,
            message_id=initial_post.message_id,
        )
    else:
        logger.warning("Initial post not configured; skipping for chat %s", chat_id)
        await safe_send_message(
            context.bot,
            chat_id=chat_id,
            text="Пока первый выпуск не настроен, но я пришлю его, как только он появится!",
        )


def format_days_until_new_year(now: datetime) -> tuple[int, str, str]:
    target = date(2026, 1, 1)
    days_left = max((target - now.date()).days, 0)
    word = select_russian_day_word(days_left)
    verb = select_russian_remaining_verb(days_left)
    return days_left, word, verb


def select_russian_day_word(value: int) -> str:
    if 11 <= value % 100 <= 14:
        return "дней"
    last_digit = value % 10
    if last_digit == 1:
        return "день"
    if last_digit in {2, 3, 4}:
        return "дня"
    return "дней"


def select_russian_remaining_verb(value: int) -> str:
    if 11 <= value % 100 <= 14:
        return "осталось"
    if value % 10 == 1:
        return "остался"
    return "осталось"


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat:
        return
    if update.effective_chat.type != "private":
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if chat_id == CONFIG.admin_chat_id and update.effective_user.is_bot:
        return

    repo = get_user_repo(context.application)
    user = await repo.get_user(user_id)

    if user and user.status == "active":
        await send_typing(context, chat_id)
        await safe_send_message(
            context.bot,
            chat_id=chat_id,
            text="Не переживай, новый выпуск прилетит под елочку сегодня в 19:00, Санта помнит о тебе ☃️",
        )
        return

    tracker = get_prompt_tracker(context.application)
    now = datetime.now(tz=CONFIG.timezone)
    days_left, word, verb = format_days_until_new_year(now)

    if user and user.status == "stop":
        await repo.upsert_user(user_id, update.effective_user.username, "active")
        await send_typing(context, chat_id)
        await safe_send_message(
            context.bot,
            chat_id=chat_id,
            text="С возвращением! Теперь у тебя снова будет по одному новому выпуску каждый день, в 19:00 по Москве ⛄",
        )
        return

    await repo.upsert_user(user_id, update.effective_user.username, "active")

    intro = (
        f"Йоп, Ян! 🎄\n\nДо Нового года {verb} {days_left} {word}. И мы в ТОПЛЕС создали свой календарь до конца 2026 года\n\nКаждый день ровно я буду отправлять один из наших выпусков. Вспомним все самое крутое, что выходило у нас на канале за последние 10 лет!"
    )
    await send_typing(context, chat_id)
    if not await safe_send_message(context.bot, chat_id=chat_id, text=intro):
        return

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⛄ Да!", callback_data="init_yes"),
                InlineKeyboardButton("🎇 Конечно!", callback_data="init_yes"),
            ]
        ]
    )
    await send_typing(context, chat_id)
    confirmation_message = await safe_send_message(
        context.bot,
        chat_id=chat_id,
        text=(
            "Хочешь получить первый выпуск уже сейчас? Заодно расскажу тебе о нем то, о чем мы ни разу не говорили публично"
        ),
        reply_markup=keyboard,
    )
    if confirmation_message:
        tracker.set_start_state(user_id, "waiting_init_confirm")


async def stop_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat:
        return
    if update.effective_chat.type != "private":
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    repo = get_user_repo(context.application)
    user = await repo.get_user(user_id)

    if not user or user.status != "active":
        await send_typing(context, chat_id)
        await safe_send_message(
            context.bot,
            chat_id=chat_id,
            text="🐧 Не переживай, я больше не буду отправлять тебе новые выпуски",
        )
        return

    await repo.upsert_user(user_id, update.effective_user.username, "stop")
    await send_typing(context, chat_id)
    await safe_send_message(
        context.bot,
        chat_id=chat_id,
        text="🐧 Хорошо, больше писать не буду. Но если захочешь снова получать наши самые лучшие выпуски, пиши /start",
    )


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or update.effective_chat.type != "private":
        return
    await safe_reply_text(
        update,
        context,
        text=(
            "Я отправляю тебе любимые выпуски ТОПЛЕС. Используй /start, чтобы получать их каждый день,"
            " и /stop, чтобы приостановить рассылку"
        ),
    )


async def id_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    chat = update.effective_chat
    message = [f"Chat ID: {chat.id}"]
    if chat.type == "private" and update.effective_user:
        message.append(f"Your user ID: {update.effective_user.id}")
    await safe_send_message(context.bot, chat_id=chat.id, text="\n".join(message))


async def set_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or update.effective_chat.id != CONFIG.admin_chat_id:
        return
    tracker = get_prompt_tracker(context.application)
    response = await safe_reply_text(update, context, "Хорошо, жду от тебя новогодний пост 🎄")
    if response:
        tracker.add(response.message_id, "schedule")


async def init_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or update.effective_chat.id != CONFIG.admin_chat_id:
        return
    tracker = get_prompt_tracker(context.application)
    response = await safe_reply_text(update, context, "Пришли мне первый пост, который я буду отправлять после приветствия 🌟")
    if response:
        tracker.add(response.message_id, "init")


async def media_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.reply_to_message:
        return
    if update.effective_chat.id != CONFIG.admin_chat_id:
        return

    tracker = get_prompt_tracker(context.application)
    reply_id = update.message.reply_to_message.message_id
    prompt_type = tracker.get(reply_id)
    if not prompt_type:
        return

    if not (update.message.photo or update.message.video):
        await safe_reply_text(update, context, "Нужно прислать фото или видео")
        return

    text = update.message.caption or update.message.text or ""

    if prompt_type == "init":
        init_store = get_initial_post_store(context.application)
        init_store.save(update.message.message_id, text)
        tracker.consume(reply_id)
        await safe_reply_text(
            update,
            context,
            "Запомнил стартовый пост! Теперь буду делиться им с новыми подписчиками ✨",
        )
        return

    storage = get_storage(context.application)
    slot = storage.next_available_slot(CONFIG.schedule_start, CONFIG.schedule_end, CONFIG.timezone)
    if not slot:
        await safe_reply_text(update, context, "Все даты заняты!")
        return

    scheduled = ScheduledPost(run_at=slot, text=text, message_id=update.message.message_id)
    storage.schedule_post(scheduled)
    tracker.consume(reply_id)

    await safe_reply_text(
        update,
        context,
        f"Запомнил! Опубликую его в ближайший доступный день: {slot.strftime('%d.%m.%Y')} в 19:00",
    )

    if (
        storage.all_slots_filled(CONFIG.schedule_start, CONFIG.schedule_end)
        and CONFIG.admin_chat_id is not None
    ):
        await safe_send_message(
            context.bot,
            chat_id=CONFIG.admin_chat_id,
            text="Ура, Advent Calendar завершен! ☃️",
        )


async def start_flow_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message or not query.from_user:
        return
    user_id = query.from_user.id
    if query.message.chat.type != "private":
        await safe_answer_callback_query(query)
        return

    tracker = get_prompt_tracker(context.application)
    state = tracker.get_start_state(user_id)
    if not state:
        await safe_answer_callback_query(query)
        return

    answered = await safe_answer_callback_query(query)
    if not answered:
        return

    chat_id = query.message.chat_id
    message_text = query.message.text or query.message.caption or ""

    if state == "waiting_init_confirm" and query.data == "init_yes":
        await safe_edit_message_text(query, text=message_text, reply_markup=None)
        await send_typing(context, chat_id)
        await send_initial_post_to_user(context, chat_id)

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🎄 Давай!", callback_data="final_yes"),
                    InlineKeyboardButton("❄️ Не хочу :(", callback_data="final_no"),
                ]
            ]
        )
        await send_typing(context, chat_id)
        sent = await safe_send_message(
            context.bot,
            chat_id=chat_id,
            text=(
                "Такие истории я буду рассказывать тебе каждый день вплоть до Нового года. Один день, одна история, один выпуск. По рукам?"
            ),
            reply_markup=keyboard,
        )
        if sent:
            tracker.set_start_state(user_id, "waiting_final_confirm")
        return

    if state == "waiting_final_confirm" and query.data in {"final_yes", "final_no"}:
        await safe_edit_message_text(query, text=message_text, reply_markup=None)
        await send_typing(context, chat_id)
        repo = get_user_repo(context.application)

        if query.data == "final_yes":
            await safe_send_message(
                context.bot,
                chat_id=chat_id,
                text=(
                    "Тогда по рукам! Второй выпуск прилетит к тебе под елочку уже сегодня, примерно в 19:00 по Москве. Ну а если захочешь отписаться от этих сообщений, пиши /stop"
                ),
            )
            tracker.clear_start_state(user_id)
            return

        await repo.upsert_user(user_id, query.from_user.username, "stop")
        await safe_send_message(
            context.bot,
            chat_id=chat_id,
            text="Жаль... Тогда не буду надоедать тебе своими сообщениями. Но если захочешь получать выпуски с нашими историями, нажми /start еще раз",
        )
        tracker.clear_start_state(user_id)


async def fallback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    if update.effective_chat.type != "private":
        return

    text = "Не переживай, новый выпуск прилетит к 19:00 по Москве, Санта помнит про тебя 🎄"
    await safe_reply_text(update, context, text)


async def unknown_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or update.effective_chat.type != "private":
        return
    await safe_reply_text(update, context, "Я знаю только /start, /stop и /help. Попробуешь ещё раз?")


async def _broadcast_post(
    context: ContextTypes.DEFAULT_TYPE,
    post: ScheduledPost,
    user_ids: list[int],
) -> None:
    if CONFIG.admin_chat_id is None:
        logger.warning("Admin chat ID missing; cannot copy scheduled post")
        return

    for user_id in user_ids:
        await safe_copy_message(
            context.bot,
            chat_id=user_id,
            from_chat_id=CONFIG.admin_chat_id,
            message_id=post.message_id,
        )


async def publish_due_posts_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    storage = get_storage(context.application)
    repo = get_user_repo(context.application)
    now = datetime.now(tz=CONFIG.timezone)
    due_posts = storage.get_due_posts(now)
    if not due_posts:
        return

    user_ids = await repo.get_active_user_ids()
    if not user_ids:
        return

    for post in due_posts:
        await _broadcast_post(context, post, user_ids)
        storage.mark_sent(post.run_at)


def build_application(
    storage: PostStorage,
    repo: UserRepository,
    initial_post_store: InitialPostStorage,
) -> Application:
    application = ApplicationBuilder().token(CONFIG.bot_token).build()
    application.bot_data["post_storage"] = storage
    application.bot_data["user_repo"] = repo
    application.bot_data["prompt_tracker"] = PromptTracker()
    application.bot_data["initial_post_store"] = initial_post_store

    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("stop", stop_handler))
    application.add_handler(CommandHandler("help", help_handler))
    application.add_handler(CommandHandler("id", id_handler))
    application.add_handler(CommandHandler("set", set_command))
    application.add_handler(CommandHandler("init", init_command))
    application.add_handler(
        MessageHandler(filters.REPLY & (filters.PHOTO | filters.VIDEO), media_reply_handler)
    )
    application.add_handler(
        CallbackQueryHandler(start_flow_callback, pattern="^(init_yes|final_yes|final_no)$")
    )
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, fallback_handler)
    )
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.COMMAND, unknown_command_handler))
    application.add_error_handler(error_handler)

    application.job_queue.run_repeating(
        publish_due_posts_job,
        interval=60,
        first=0,
        name="minute-publisher",
    )

    return application


def main() -> None:
    storage = PostStorage(
        CONFIG.posts_file,
        CONFIG.sent_log_file,
        publish_hour=CONFIG.publish_time.hour,
    )
    initial_post_store = InitialPostStorage(CONFIG.initial_post_file)
    repo = UserRepository(CONFIG.database_dsn)

    retry_delay = 5
    while True:
        application = build_application(storage, repo, initial_post_store)
        try:
            application.run_polling(drop_pending_updates=True)
            break
        except Conflict as exc:
            logger.error("Another bot instance is already running. Shutting down: %s", exc)
            break
        except _TRANSIENT_ERRORS as exc:
            logger.warning(
                "Polling interrupted due to network issue (%s). Restarting in %s seconds...",
                exc,
                retry_delay,
            )
            time.sleep(retry_delay)
        except TelegramError as exc:
            logger.exception("Unexpected Telegram error in polling loop: %s", exc)
            time.sleep(retry_delay)
        except KeyboardInterrupt:
            logger.info("Received interrupt signal. Shutting down.")
            break
        except Exception:
            logger.exception("Unexpected fatal error in polling loop. Stopping.")
            break


if __name__ == "__main__":
    main()
