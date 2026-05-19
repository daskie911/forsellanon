import logging
import asyncio
import os
from dotenv import load_dotenv

# Загружаем .env ДО всех os.getenv()
load_dotenv()

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto, InputMediaVideo
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler
)
from datetime import datetime
from collections import defaultdict

from database import db
from rate_limiter import rate_limiter

# ═══════════════════════════════════════════
#  Настройки
# ═══════════════════════════════════════════

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TOKEN_HERE")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
BOT_USERNAME = os.getenv("BOT_USERNAME", "your_bot_username")

# Временное хранилище медиагрупп (в памяти — это нормально)
media_groups: dict = defaultdict(lambda: {'messages': [], 'task': None})


# ═══════════════════════════════════════════
#  Хелперы
# ═══════════════════════════════════════════

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def format_message(text: str) -> str:
    return (
        f"💬 У тебя новое анонимное сообщение!\n\n"
        f"{text}\n\n"
        f"⬅️ Свайпни для ответа\n\n"
        f"@{BOT_USERNAME}"
    )


def format_media_caption(caption: str = None) -> str:
    base = "💬 У тебя новое анонимное сообщение!"
    tail = f"\n\n⬅️ Свайпни для ответа\n\n@{BOT_USERNAME}"
    if caption:
        return f"{base}\n\n{caption}{tail}"
    return f"{base}{tail}"


def get_block_keyboard(recipient_msg_id: int) -> InlineKeyboardMarkup:
    """Клавиатура под анонимным сообщением: Заблокировать"""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🚫 Заблокировать", callback_data=f"block_{recipient_msg_id}")
    ]])


async def check_rate_limit(message, user_id: int) -> bool:
    """Проверить rate-limit. Возвращает True если ЗАБЛОКИРОВАН."""
    if is_admin(user_id):
        return False  # админы без лимита

    if not rate_limiter.is_allowed(user_id):
        wait = rate_limiter.get_wait_time(user_id)
        await message.reply_text(
            f"⏳ Слишком много сообщений!\n"
            f"Подожди {wait} сек. перед следующим."
        )
        return True
    return False


async def send_anon_message(context, recipient_id: int, sender_id: int, **kwargs):
    """
    Универсальная отправка анонимного сообщения.
    Проверяет блокировку, отправляет, создаёт маппинг.
    Возвращает True если отправлено, False если заблокировано.
    """
    # Проверяем блокировку
    if await db.is_blocked(recipient_id, sender_id):
        return None  # заблокирован, но не говорим отправителю

    send_method = kwargs.pop('method')
    try:
        sent_msg = await send_method(chat_id=recipient_id, **kwargs)

        msg_id = sent_msg.message_id if not isinstance(sent_msg, list) else sent_msg[0].message_id
        await db.add_mapping(recipient_id, msg_id, sender_id)
        return sent_msg
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")
        return False


# ═══════════════════════════════════════════
#  Команды
# ═══════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"Команда /start от {user.id}")

    # Регистрируем пользователя в БД
    await db.upsert_user(user.id, user.username or "", user.first_name)

    if context.args:
        try:
            recipient_id = int(context.args[0])
            context.user_data['recipient_id'] = recipient_id

            link = f"https://t.me/{BOT_USERNAME}?start={user.id}"
            await update.message.reply_text(
                f"Начни получать анонимные сообщения прямо сейчас 🚀\n\n"
                f"Твоя ссылка 👇\n\n"
                f"{link}\n\n"
                f"Размести эту ссылку ☝️ в описании профиля "
                f"Telegram/TikTok/Instagram, чтобы начать получать "
                f"анонимные сообщения 💬"
            )
            return
        except (ValueError, IndexError):
            pass

    welcome = (
        f"👋 Привет, {user.first_name}!\n\n"
        f"Я анонимный бот для получения сообщений.\n\n"
        f"📝 Ваша персональная ссылка:\n"
        f"https://t.me/{BOT_USERNAME}?start={user.id}\n\n"
        f"Отправьте эту ссылку друзьям!\n\n"
        f"🔹 Команды:\n"
        f"/mylink — Получить ссылку\n"
        f"/stats — Статистика\n"
        f"/blocked — Список заблокированных\n"
        f"/cancel — Отменить"
    )
    if is_admin(user.id):
        welcome += "\n\n👑 /admin — Админ-панель"

    await update.message.reply_text(welcome)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'recipient_id' in context.user_data:
        del context.user_data['recipient_id']
        await update.message.reply_text("❌ Отправка отменена.\n\n/start")
    else:
        await update.message.reply_text("/start")


async def cmd_mylink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    link = f"https://t.me/{BOT_USERNAME}?start={user.id}"
    await update.message.reply_text(
        f"📎 Ваша ссылка:\n\n`{link}`\n\nПоделитесь ей!",
        parse_mode='Markdown'
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    sent = await db.count_user_sent(user_id)
    received = await db.count_user_received(user_id)
    first_seen = await db.get_user_first_seen(user_id)
    blocked_count = await db.count_blocked(user_id)

    if first_seen:
        date_str = datetime.fromisoformat(first_seen).strftime('%d.%m.%Y %H:%M')
    else:
        date_str = "—"

    text = (
        f"📊 Твоя статистика:\n\n"
        f"📤 Отправлено: {sent}\n"
        f"📥 Получено: {received}\n"
        f"🚫 Заблокировано: {blocked_count}\n"
        f"📅 С нами с: {date_str}\n"
        f"🆔 ID: {user_id}"
    )
    await update.message.reply_text(text)


async def cmd_blocked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать список заблокированных и кнопку разблокировки"""
    user_id = update.effective_user.id
    blocked_list = await db.get_blocked_list(user_id)

    if not blocked_list:
        await update.message.reply_text("✅ У вас нет заблокированных пользователей.")
        return

    keyboard = []
    for i, blocked_id in enumerate(blocked_list, 1):
        keyboard.append([
            InlineKeyboardButton(
                f"🔓 Разблокировать #{i} (ID: ...{str(blocked_id)[-4:]})",
                callback_data=f"unblock_{blocked_id}"
            )
        ])

    text = f"🚫 Заблокировано: {len(blocked_list)}\n\nНажмите чтобы разблокировать:"
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


# ═══════════════════════════════════════════
#  Обработка reply (цепочка ответов)
# ═══════════════════════════════════════════

async def handle_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Обработка ответа на анонимное сообщение. Возвращает True если обработано."""
    user = update.effective_user
    message = update.message

    if not message.reply_to_message:
        return False

    original_sender_id = await db.get_sender(user.id, message.reply_to_message.message_id)
    if not original_sender_id:
        return False

    # Проверяем блокировку (в обе стороны)
    if await db.is_blocked(original_sender_id, user.id):
        await message.reply_text("❌ Не удалось отправить ответ.")
        return True

    # Rate-limit
    if await check_rate_limit(message, user.id):
        return True

    logger.info(f"Обработка ответа от {user.id} к {original_sender_id}")

    try:
        sent_msg = await forward_content(
            context, message, original_sender_id, user.id
        )
        if sent_msg and sent_msg is not None:
            await message.reply_text("✅ Ответ отправлен!")
            await send_admin_notification(context, user, 'ответ', 'ответ на сообщение', original_sender_id)
        else:
            await message.reply_text("✅ Отправлено!")
        return True
    except Exception as e:
        logger.error(f"Ошибка отправки ответа: {e}")
        await message.reply_text("❌ Не удалось отправить ответ.")
        return True


async def forward_content(context, message, recipient_id: int, sender_id: int):
    """Универсальная пересылка любого контента + кнопка блокировки"""

    if message.text:
        sent = await context.bot.send_message(
            chat_id=recipient_id,
            text=format_message(message.text)
        )
    elif message.photo:
        sent = await context.bot.send_photo(
            chat_id=recipient_id,
            photo=message.photo[-1].file_id,
            caption=format_media_caption(message.caption)
        )
    elif message.video:
        sent = await context.bot.send_video(
            chat_id=recipient_id,
            video=message.video.file_id,
            caption=format_media_caption(message.caption)
        )
    elif message.video_note:
        await context.bot.send_video_note(
            chat_id=recipient_id,
            video_note=message.video_note.file_id
        )
        sent = await context.bot.send_message(
            chat_id=recipient_id,
            text=f"💬 У тебя новое анонимное сообщение!\n\n⬅️ Свайпни для ответа\n\n@{BOT_USERNAME}"
        )
    elif message.voice:
        sent = await context.bot.send_voice(
            chat_id=recipient_id,
            voice=message.voice.file_id,
            caption=format_media_caption(None)
        )
    elif message.audio:
        sent = await context.bot.send_audio(
            chat_id=recipient_id,
            audio=message.audio.file_id,
            caption=format_media_caption(message.caption),
            title=message.audio.title,
            performer=message.audio.performer
        )
    elif message.animation:
        sent = await context.bot.send_animation(
            chat_id=recipient_id,
            animation=message.animation.file_id,
            caption=format_media_caption(message.caption)
        )
    elif message.document:
        sent = await context.bot.send_document(
            chat_id=recipient_id,
            document=message.document.file_id,
            caption=format_media_caption(message.caption)
        )
    else:
        return None

    # Маппинг + кнопка "Заблокировать"
    await db.add_mapping(recipient_id, sent.message_id, sender_id)

    block_kb = get_block_keyboard(sent.message_id)
    await context.bot.send_message(
        chat_id=recipient_id,
        text="👆 Ответь свайпом на сообщение выше",
        reply_markup=block_kb
    )

    return sent


# ═══════════════════════════════════════════
#  Медиагруппы (альбомы)
# ═══════════════════════════════════════════

async def send_media_group_task(context, group_key: str, user, recipient_id):
    """Ожидает 2 секунды и отправляет медиагруппу"""
    await asyncio.sleep(2)

    if group_key not in media_groups:
        return

    group_data = media_groups[group_key]
    messages = group_data['messages']

    if not messages:
        return

    # Проверяем блокировку
    if await db.is_blocked(recipient_id, user.id):
        await context.bot.send_message(chat_id=user.id, text="✅ Отправлено!")
        if group_key in media_groups:
            del media_groups[group_key]
        return

    try:
        media_list = []
        for idx, msg in enumerate(messages):
            caption = format_media_caption(msg.get('caption')) if idx == 0 else None
            if msg['type'] == 'photo':
                media_list.append(InputMediaPhoto(media=msg['file_id'], caption=caption))
            else:
                media_list.append(InputMediaVideo(media=msg['file_id'], caption=caption))

        sent_messages = await context.bot.send_media_group(chat_id=recipient_id, media=media_list)

        if sent_messages:
            await db.add_mapping(recipient_id, sent_messages[0].message_id, user.id)

            block_kb = get_block_keyboard(sent_messages[0].message_id)
            await context.bot.send_message(
                chat_id=recipient_id,
                text="👆 Ответь свайпом на сообщение выше",
                reply_markup=block_kb
            )

        await context.bot.send_message(
            chat_id=user.id,
            text=f"✅ Отправлено {len(messages)} медиафайлов!\n\n/cancel"
        )

        await send_admin_notification(context, user, 'медиагруппа', f'{len(messages)} файлов', recipient_id)

        await db.log_message(user.id, recipient_id, 'media_group', f'{len(messages)} файлов')

    except Exception as e:
        logger.error(f"Ошибка отправки медиагруппы: {e}")
        await context.bot.send_message(chat_id=user.id, text="❌ Не удалось отправить медиагруппу.")

    if group_key in media_groups:
        del media_groups[group_key]


async def handle_media_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Обработка медиагруппы (альбом фото/видео)"""
    user = update.effective_user
    message = update.message
    media_group_id = message.media_group_id

    if not media_group_id:
        return False

    recipient_id = context.user_data.get('recipient_id')
    if not recipient_id:
        return False

    if user.id == recipient_id:
        await message.reply_text("❌ Нельзя отправить самому себе!")
        return True

    group_key = f"{user.id}_{media_group_id}"

    media_info = {
        'type': 'photo' if message.photo else 'video',
        'file_id': message.photo[-1].file_id if message.photo else message.video.file_id,
        'caption': message.caption
    }

    media_groups[group_key]['messages'].append(media_info)

    if media_groups[group_key]['task']:
        media_groups[group_key]['task'].cancel()

    task = asyncio.create_task(send_media_group_task(context, group_key, user, recipient_id))
    media_groups[group_key]['task'] = task

    return True


# ═══════════════════════════════════════════
#  Универсальная обработка сообщений
# ═══════════════════════════════════════════

async def _handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE, msg_type: str):
    """Универсальный обработчик для всех типов сообщений"""
    user = update.effective_user
    message = update.message

    # 1. Медиагруппа?
    if message.media_group_id and msg_type in ('photo', 'video'):
        handled = await handle_media_group(update, context)
        if handled:
            return

    # 2. Reply?
    if message.reply_to_message:
        handled = await handle_reply(update, context)
        if handled:
            return

    # 3. Есть получатель?
    if 'recipient_id' not in context.user_data:
        await message.reply_text("❌ Используйте персональную ссылку. /mylink")
        return

    recipient_id = context.user_data['recipient_id']

    # 4. Не самому себе
    if user.id == recipient_id:
        await message.reply_text("❌ Нельзя отправить самому себе!")
        return

    # 5. Rate-limit
    if await check_rate_limit(message, user.id):
        return

    # 6. Блокировка
    if await db.is_blocked(recipient_id, user.id):
        await message.reply_text("✅ Сообщение отправлено!\n\n/cancel")
        return  # тихо "проглатываем" — не говорим что заблокирован

    # 7. Регистрация + лог
    username = user.username or "no_username"
    await db.upsert_user(user.id, username, user.first_name)

    content = _get_content_preview(message, msg_type)
    await db.log_message(user.id, recipient_id, msg_type, content)
    await send_admin_notification(context, user, msg_type, content, recipient_id)

    # 8. Отправка
    try:
        sent = await forward_content(context, message, recipient_id, user.id)
        if sent is not None:
            await message.reply_text(f"✅ Отправлено!\n\n/cancel")
        else:
            await message.reply_text(f"✅ Отправлено!\n\n/cancel")
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await message.reply_text("❌ Не удалось отправить.")


def _get_content_preview(message, msg_type: str) -> str:
    """Получить текстовое превью контента для логирования"""
    if msg_type == 'text':
        return message.text or ""
    elif message.caption:
        return message.caption
    elif msg_type == 'audio' and message.audio and message.audio.title:
        return message.audio.title
    elif msg_type == 'document' and message.document and message.document.file_name:
        return message.document.file_name
    return msg_type


# Отдельные хендлеры — просто вызывают _handle_message
async def handle_text(update, context):
    await _handle_message(update, context, 'text')

async def handle_photo(update, context):
    await _handle_message(update, context, 'photo')

async def handle_video(update, context):
    await _handle_message(update, context, 'video')

async def handle_video_note(update, context):
    await _handle_message(update, context, 'video_note')

async def handle_voice(update, context):
    await _handle_message(update, context, 'voice')

async def handle_audio(update, context):
    await _handle_message(update, context, 'audio')

async def handle_animation(update, context):
    await _handle_message(update, context, 'animation')

async def handle_document(update, context):
    await _handle_message(update, context, 'document')


# ═══════════════════════════════════════════
#  Уведомления админам
# ═══════════════════════════════════════════

async def send_admin_notification(context, user, media_type: str, content: str, recipient_id):
    admin_text = (
        f"🔔 НОВОЕ СООБЩЕНИЕ\n\n"
        f"👤 От:\n"
        f"├ ID: {user.id}\n"
        f"├ @{user.username if user.username else 'нет'}\n"
        f"├ Имя: {user.first_name}\n"
        f"└ Время: {datetime.now().strftime('%H:%M:%S')}\n\n"
        f"👥 Кому: {recipient_id}\n"
        f"📎 Тип: {media_type}\n"
        f"💬 {content[:100]}\n"
        f"━━━━━━━━━━━━━━━━━"
    )
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=admin_text)
        except Exception as e:
            logger.error(f"Ошибка уведомления админу {admin_id}: {e}")


# ═══════════════════════════════════════════
#  Callback-обработчики (блокировка/разблокировка + админка)
# ═══════════════════════════════════════════

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # --- Блокировка ---
    if data.startswith("block_"):
        msg_id = int(data.split("_")[1])
        user_id = query.from_user.id
        sender_id = await db.get_sender(user_id, msg_id)

        if not sender_id:
            await query.edit_message_text("⚠️ Не удалось найти отправителя.")
            return

        await db.block_user(user_id, sender_id)
        await query.edit_message_text(
            "🚫 Пользователь заблокирован!\n\n"
            "Он больше не сможет отправлять вам сообщения.\n"
            "Разблокировать: /blocked"
        )
        return

    # --- Разблокировка ---
    if data.startswith("unblock_"):
        blocked_id = int(data.split("_")[1])
        user_id = query.from_user.id

        await db.unblock_user(user_id, blocked_id)
        await query.edit_message_text(
            f"✅ Пользователь разблокирован!\n\n"
            f"Он снова может отправлять вам анонимные сообщения."
        )
        return

    # --- Админ-панель ---
    if not is_admin(query.from_user.id):
        return

    if data == "admin_stats":
        total_users = await db.count_users()
        total_messages = await db.count_messages()
        await query.edit_message_text(
            f"📊 СТАТИСТИКА\n\n"
            f"👥 Пользователей: {total_users}\n"
            f"📨 Сообщений: {total_messages}"
        )

    elif data == "admin_users":
        top_users = await db.get_top_users(10)
        if not top_users:
            await query.edit_message_text("📭 Нет пользователей")
            return

        text = "👥 ТОП ПОЛЬЗОВАТЕЛЕЙ:\n\n"
        for i, u in enumerate(top_users, 1):
            name = u['first_name'] or '—'
            uname = u['username'] or 'нет'
            text += f"{i}. {name} (@{uname})\n   ID: {u['user_id']} | Сообщений: {u['msg_count']}\n\n"

        await query.edit_message_text(text[:4000])

    elif data == "admin_recent":
        recent = await db.get_recent_messages(5)
        if not recent:
            await query.edit_message_text("📭 Нет сообщений")
            return

        text = "📝 ПОСЛЕДНИЕ:\n\n"
        for i, m in enumerate(recent, 1):
            t = datetime.fromisoformat(m['timestamp']).strftime('%H:%M')
            name = m.get('first_name') or '—'
            text += f"{i}. {name} ({t})\n   {m['message_type']}: {(m.get('content') or '')[:50]}\n\n"

        await query.edit_message_text(text[:4000])

    elif data == "admin_cleanup":
        await db.cleanup_old_mappings(days=30)
        await query.edit_message_text("🧹 Старые маппинги очищены (>30 дней)")


# ═══════════════════════════════════════════
#  Админ-панель
# ═══════════════════════════════════════════

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Нет доступа.")
        return

    keyboard = [
        [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("👥 Пользователи", callback_data="admin_users")],
        [InlineKeyboardButton("📝 Последние", callback_data="admin_recent")],
        [InlineKeyboardButton("🧹 Очистить маппинги", callback_data="admin_cleanup")],
    ]
    await update.message.reply_text(
        "👑 АДМИН-ПАНЕЛЬ:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ═══════════════════════════════════════════
#  Запуск
# ═══════════════════════════════════════════

def main():
    # Отключаем прокси
    for key in ('NO_PROXY', 'no_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
        os.environ[key] = '*' if 'NO' in key.upper() else ''

    print("=" * 50)
    print("🚀 ЗАПУСК БОТА...")
    print("=" * 50)
    print(f"✅ Токен: {BOT_TOKEN[:10]}...")
    print(f"👑 Админы: {', '.join(map(str, ADMIN_IDS))}")
    print(f"🤖 Bot: @{BOT_USERNAME}")

    try:
        application = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()

        # Команды
        application.add_handler(CommandHandler("start", cmd_start))
        application.add_handler(CommandHandler("mylink", cmd_mylink))
        application.add_handler(CommandHandler("stats", cmd_stats))
        application.add_handler(CommandHandler("admin", cmd_admin))
        application.add_handler(CommandHandler("cancel", cmd_cancel))
        application.add_handler(CommandHandler("blocked", cmd_blocked))

        # Callbacks (блокировка + админка)
        application.add_handler(CallbackQueryHandler(callback_handler))

        # Сообщения
        application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
        application.add_handler(MessageHandler(filters.VIDEO & ~filters.VIDEO_NOTE, handle_video))
        application.add_handler(MessageHandler(filters.VIDEO_NOTE, handle_video_note))
        application.add_handler(MessageHandler(filters.ANIMATION, handle_animation))
        application.add_handler(MessageHandler(filters.VOICE, handle_voice))
        application.add_handler(MessageHandler(filters.AUDIO, handle_audio))
        application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

        print("=" * 50)
        print("✅ БОТ ЗАПУЩЕН!")
        print("✅ SQLite база данных")
        print("✅ Блокировка пользователей")
        print("✅ Rate-limit (8 сообщений / минута)")
        print("✅ Поддержка всех типов медиа + альбомы")
        print("Для остановки нажмите Ctrl+C")
        print("=" * 50)

        application.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

    except Exception as e:
        print(f"❌ ОШИБКА: {e}")
        import traceback
        traceback.print_exc()


async def post_init(application):
    """Вызывается после инициализации бота — подключаем БД"""
    await db.connect()
    logger.info("✅ Database initialized")


async def post_shutdown(application):
    """Вызывается при остановке — закрываем БД"""
    await db.close()
    logger.info("❌ Database closed")


if __name__ == '__main__':
    main()
