from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from advent_bot.config import CONFIG
from advent_bot.db import UserRepository
from advent_bot.posts import PostStorage, ScheduledPost
from advent_bot.initial_post import InitialPostStorage

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

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


async def send_initial_post_to_user(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    init_store = get_initial_post_store(context.application)
    initial_post = init_store.load()
    if initial_post and CONFIG.admin_chat_id is not None:
        try:
            await context.bot.copy_message(
                chat_id=chat_id,
                from_chat_id=CONFIG.admin_chat_id,
                message_id=initial_post.message_id,
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to send initial post %s: %s", initial_post.message_id, exc)
    else:
        logger.warning("Initial post not configured; skipping for chat %s", chat_id)
        await context.bot.send_message(
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


async def send_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int, seconds: int) -> None:
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    await asyncio.sleep(seconds)


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat:
        return
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if update.effective_chat.type != "private":
        return
    if chat_id == CONFIG.admin_chat_id and update.effective_user.is_bot:
        return

    repo = get_user_repo(context.application)
    user = await repo.get_user(user_id)
    if user and user.status == "active":
        await context.bot.send_message(
            chat_id=chat_id,
            text="Не переживай, новый выпуск прилетит под елочку сегодня в 19:00, Санта помнит о тебе ☃️",
        )
        return

    tracker = get_prompt_tracker(context.application)

    now = datetime.now(tz=CONFIG.timezone)
    days_left, word, verb = format_days_until_new_year(now)

    if user and user.status == "stop":
        await repo.upsert_user(user_id, update.effective_user.username, "active")
        await send_typing(context, chat_id, 5)
        await context.bot.send_message(chat_id=chat_id, text="С возвращением! Теперь у тебя снова будет по одному новому выпуску каждый день, в 19:00 по Москве ⛄")
        return

    await repo.upsert_user(user_id, update.effective_user.username, "active")

    intro = (
        f"Йоп, Ян! 🎄\n\nДо Нового года {verb} {days_left} {word}. И мы в ТОПЛЕС создали свой календарь до конца 2026 года\n\nКаждый день ровно я буду отправлять один из наших выпусков. Вспомним все самое крутое, что выходило у нас на канале за последние 10 лет!"
    )
    await context.bot.send_message(chat_id=chat_id, text=intro)

    await send_typing(context, chat_id, 10)

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⛄ Да!", callback_data="init_yes"),
                InlineKeyboardButton("🎇 Конечно!", callback_data="init_yes"),
            ]
        ]
    )
    prompt_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="Хочешь получить первый выпуск уже сейчас? Заодно расскажу тебе о нем то, о чем мы ни разу не говорили публично",
        reply_markup=keyboard,
    )
    tracker.set_start_state(user_id, "waiting_init_confirm")


async def stop_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat:
        return
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if update.effective_chat.type != "private":
        return
    repo = get_user_repo(context.application)
    user = await repo.get_user(user_id)
    if not user or user.status != "active":
        await send_typing(context, chat_id, 2)
        await context.bot.send_message(
            chat_id=chat_id,
            text="🐧 Не переживай, я больше не буду отправлять тебе новые выпуски",
        )
        return

    await repo.upsert_user(user_id, update.effective_user.username, "stop")
    await send_typing(context, chat_id, 2)
    await context.bot.send_message(
        chat_id=chat_id,
        text="🐧 Хорошо, больше писать не буду. Но если захочешь снова получать наши самые лучшие выпуски, пиши /start",
    )


async def id_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    chat = update.effective_chat
    message = [f"Chat ID: {chat.id}"]
    if chat.type == "private" and update.effective_user:
        message.append(f"Your user ID: {update.effective_user.id}")
    await context.bot.send_message(chat_id=chat.id, text="\n".join(message))


async def set_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or update.effective_chat.id != CONFIG.admin_chat_id:
        return
    if not update.message:
        return
    tracker = get_prompt_tracker(context.application)
    response = await update.message.reply_text("Хорошо, жду от тебя новогодний пост 🎄")
    tracker.add(response.message_id, "schedule")


async def init_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or update.effective_chat.id != CONFIG.admin_chat_id:
        return
    if not update.message:
        return
    tracker = get_prompt_tracker(context.application)
    response = await update.message.reply_text("Пришли мне первый пост, который я буду отправлять после приветствия 🌟")
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
        await update.message.reply_text("Нужно прислать фото или видео")
        return

    text = update.message.caption or update.message.text or ""

    if prompt_type == "init":
        init_store = get_initial_post_store(context.application)
        init_store.save(update.message.message_id, text)
        tracker.consume(reply_id)
        await update.message.reply_text("Запомнил стартовый пост! Теперь буду делиться им с новыми подписчиками ✨")
        return

    storage = get_storage(context.application)

    slot = storage.next_available_slot(CONFIG.schedule_start, CONFIG.schedule_end, CONFIG.timezone)
    if not slot:
        await update.message.reply_text("Все даты заняты!")
        return

    scheduled = ScheduledPost(run_at=slot, text=text, message_id=update.message.message_id)
    storage.schedule_post(scheduled)
    tracker.consume(reply_id)

    await update.message.reply_text(
        f"Запомнил! Опубликую его в ближайший доступный день: {slot.strftime('%d.%m.%Y')} в 19:00"
    )

    if storage.all_slots_filled(CONFIG.schedule_start, CONFIG.schedule_end):
        await context.bot.send_message(chat_id=CONFIG.admin_chat_id, text="Ура, Advent Calendar завершен! ☃️")


async def start_flow_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message or not query.from_user:
        return
    if query.message.chat.type != "private":
        await query.answer()
        return

    user_id = query.from_user.id
    chat_id = query.message.chat_id
    tracker = get_prompt_tracker(context.application)
    state = tracker.get_start_state(user_id)
    if not state:
        await query.answer()
        return

    await query.answer()

    if state == "waiting_init_confirm" and query.data == "init_yes":
        await query.edit_message_reply_markup(reply_markup=None)
        await send_typing(context, chat_id, 5)
        await send_initial_post_to_user(context, chat_id)

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🎄 Давай!", callback_data="final_yes"),
                    InlineKeyboardButton("❄️ Не хочу :(", callback_data="final_no"),
                ]
            ]
        )
        await send_typing(context, chat_id, 10)
        await context.bot.send_message(
            chat_id=chat_id,
            text="Такие истории я буду рассказывать тебе каждый день вплоть до Нового года. Один день, одна история, один выпуск. По рукам?",
            reply_markup=keyboard,
        )
        tracker.set_start_state(user_id, "waiting_final_confirm")
        return

    if state == "waiting_final_confirm" and query.data in {"final_yes", "final_no"}:
        await query.edit_message_reply_markup(reply_markup=None)
        await send_typing(context, chat_id, 5)
        repo = get_user_repo(context.application)

        if query.data == "final_yes":
            await context.bot.send_message(
                chat_id=chat_id,
                text="Тогда по рукам! Второй выпуск прилетит к тебе под елочку уже сегодня, примерно в 19:00 по Москве. Ну а если захочешь отписаться от этих сообщений, пиши /stop",
            )
            tracker.clear_start_state(user_id)
            return

        if query.data == "final_no":
            await repo.upsert_user(user_id, query.from_user.username, "stop")
            await context.bot.send_message(
                chat_id=chat_id,
                text="Жаль... Тогда не буду надоедать тебе своими сообщениями. Но если захочешь получать выпуски с нашими историями, нажми /start еще раз",
            )
            tracker.clear_start_state(user_id)
            return

async def _broadcast_post(context: CallbackContext, post: ScheduledPost, user_ids: list[int]) -> None:
    if CONFIG.admin_chat_id is None:
        logger.warning("Admin chat ID missing; cannot copy scheduled post")
        return

    for user_id in user_ids:
        try:
            await context.bot.copy_message(
                chat_id=user_id,
                from_chat_id=CONFIG.admin_chat_id,
                message_id=post.message_id,
            )
        except Exception as exc:  # pragma: no cover - network issues
            logger.warning("Failed to send post %s to %s: %s", post.message_id, user_id, exc)


async def publish_due_posts_job(context: CallbackContext) -> None:
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


def build_application() -> Application:
    storage = PostStorage(
        CONFIG.posts_file,
        CONFIG.sent_log_file,
        publish_hour=CONFIG.publish_time.hour,
    )
    initial_post_store = InitialPostStorage(CONFIG.initial_post_file)
    repo = UserRepository(CONFIG.database_dsn)

    application = ApplicationBuilder().token(CONFIG.bot_token).build()
    application.bot_data["post_storage"] = storage
    application.bot_data["user_repo"] = repo
    application.bot_data["prompt_tracker"] = PromptTracker()
    application.bot_data["initial_post_store"] = initial_post_store

    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("stop", stop_handler))
    application.add_handler(CommandHandler("id", id_handler))
    application.add_handler(CommandHandler("set", set_command))
    application.add_handler(CommandHandler("init", init_command))
    application.add_handler(
        MessageHandler(filters.REPLY & (filters.PHOTO | filters.VIDEO), media_reply_handler)
    )
    application.add_handler(CallbackQueryHandler(start_flow_callback, pattern="^(init_yes|final_yes|final_no)$"))

    application.job_queue.run_repeating(
        publish_due_posts_job,
        interval=60,
        first=0,
        name="minute-publisher",
    )

    return application


def main() -> None:
    application = build_application()
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
