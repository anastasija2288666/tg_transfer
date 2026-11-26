import asyncio
import json
import logging
import os
import shutil

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telethon import TelegramClient
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.tl.functions.messages import ImportChatInviteRequest

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID")) if os.getenv("ADMIN_ID") else None
DATA_DIR = os.getenv("DATA_DIR", "/app/data") if os.path.exists("/app/data") else "."

API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"

SESSION_PATH = os.path.join(DATA_DIR, "user_session")
STATE_FILE = os.path.join(DATA_DIR, "transfer_history.json")
TEMP_DIR = os.path.join(DATA_DIR, "temp_media")

router = Router()
pending_clients: dict[int, TelegramClient] = {}
is_transferring = False


class AuthStates(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()


class TransferStates(StatesGroup):
    waiting_source = State()
    waiting_target = State()
    waiting_limit = State()


def is_admin(user_id: int) -> bool:
    return ADMIN_ID is not None and user_id == ADMIN_ID


def green_btn(text: str, callback_data: str) -> InlineKeyboardButton:
    try:
        return InlineKeyboardButton(text=text, callback_data=callback_data, style="success")
    except TypeError:
        return InlineKeyboardButton(text=text, callback_data=callback_data)


def red_btn(text: str, callback_data: str) -> InlineKeyboardButton:
    try:
        return InlineKeyboardButton(text=text, callback_data=callback_data, style="danger")
    except TypeError:
        return InlineKeyboardButton(text=text, callback_data=callback_data)


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [green_btn("📱 1. Подключить аккаунт", callback_data="auth_start")],
        [green_btn("🚀 2. Запустить перенос постов", callback_data="transfer_start")],
        [red_btn("🧹 Сбросить историю переносов", callback_data="reset_history")],
        [red_btn("🗑 Сбросить сессию аккаунта", callback_data="logout_session")],
    ])


def load_history() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_history(data: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


async def resolve_entity(client: TelegramClient, raw_link: str):
    link = raw_link.strip()
    if "t.me/+" in link or "joinchat/" in link:
        invite_hash = link.split("+")[-1].split("/")[-1].strip()
        try:
            updates = await client(ImportChatInviteRequest(invite_hash))
            if hasattr(updates, "chats") and updates.chats:
                return updates.chats[0]
        except Exception:
            pass
    return await client.get_entity(link)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ закрыт.")
        return

    session_exists = os.path.exists(SESSION_PATH + ".session")
    status_text = "✅ Технический аккаунт подключён." if session_exists else "⚠️ Технический аккаунт ещё не подключён."

    await message.answer(
        f"👋 <b>Бот-переносчик постов готов к работе!</b>\n\n"
        f"{status_text}\n\n"
        f"Бот запоминает последний перенесённый пост для каждой пары каналов и при повторном запуске продолжает с места остановки без дублей.",
        parse_mode="HTML",
        reply_markup=main_menu()
    )


@router.callback_query(F.data == "back_main")
async def cb_back_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if not is_admin(callback.from_user.id):
        return
    session_exists = os.path.exists(SESSION_PATH + ".session")
    status_text = "✅ Технический аккаунт подключён." if session_exists else "⚠️ Технический аккаунт ещё не подключён."
    await callback.message.edit_text(
        f"👋 <b>Главное меню переноса постов</b>\n\n{status_text}",
        parse_mode="HTML",
        reply_markup=main_menu()
    )
    await callback.answer()


@router.callback_query(F.data == "reset_history")
async def cb_reset_history(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
    await callback.answer("🧹 История переносов очищена! Теперь каналы будут переноситься с самого начала.", show_alert=True)


@router.callback_query(F.data == "logout_session")
async def cb_logout_session(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    for ext in (".session", ".session-journal"):
        path = SESSION_PATH + ext
        if os.path.exists(path):
            os.remove(path)
    await callback.answer("🗑 Сессия аккаунта удалена.", show_alert=True)
    await cb_back_main(callback, FSMContext)


@router.callback_query(F.data == "auth_start")
async def cb_auth_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(AuthStates.waiting_phone)
    await callback.message.edit_text(
        "📱 <b>Подключение технического аккаунта</b>\n\n"
        "Отправьте номер телефона рабочего аккаунта Telegram в международном формате (например, <code>+79991234567</code>):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[red_btn("❌ Отмена", callback_data="back_main")]])
    )
    await callback.answer()


@router.message(AuthStates.waiting_phone)
async def process_phone(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    phone = message.text.strip()
    os.makedirs(DATA_DIR, exist_ok=True)
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        pending_clients[message.from_user.id] = client
        await state.update_data(phone=phone, phone_code_hash=sent.phone_code_hash)
        await state.set_state(AuthStates.waiting_code)
        await message.answer(
            "🔑 Код подтверждения отправлен в Telegram!\n\n"
            "Введите его сюда (можно с пробелами или дефисом, например <code>12-345</code>):",
            parse_mode="HTML"
        )
    except Exception as e:
        await client.disconnect()
        await message.answer(f"❌ Ошибка отправки кода: {e}")


@router.message(AuthStates.waiting_code)
async def process_code(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    code = message.text.replace("-", "").replace(" ", "").strip()
    data = await state.get_data()
    client = pending_clients.get(message.from_user.id)
    if not client:
        await message.answer("⚠️ Сессия устарела. Нажмите /start и начните заново.")
        await state.clear()
        return

    try:
        await client.sign_in(phone=data["phone"], code=code, phone_code_hash=data["phone_code_hash"])
        await client.disconnect()
        pending_clients.pop(message.from_user.id, None)
        await state.clear()
        await message.answer("✅ <b>Аккаунт успешно подключён!</b> Теперь можно запускать перенос.", parse_mode="HTML", reply_markup=main_menu())
    except SessionPasswordNeededError:
        await state.set_state(AuthStates.waiting_password)
        await message.answer("🔐 На аккаунте включена двухэтапная аутентификация (облачный пароль). Введите ваш пароль:")
    except Exception as e:
        await message.answer(f"❌ Неверный код или ошибка входа: {e}")


@router.message(AuthStates.waiting_password)
async def process_password(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    client = pending_clients.get(message.from_user.id)
    if not client:
        await message.answer("⚠️ Сессия устарела. Нажмите /start.")
        await state.clear()
        return
    try:
        await client.sign_in(password=message.text.strip())
        await client.disconnect()
        pending_clients.pop(message.from_user.id, None)
        await state.clear()
        await message.answer("✅ <b>Аккаунт успешно подключён!</b>", parse_mode="HTML", reply_markup=main_menu())
    except Exception as e:
        await message.answer(f"❌ Ошибка пароля: {e}")


@router.callback_query(F.data == "transfer_start")
async def cb_transfer_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    if is_transferring:
        await callback.answer("⏳ Перенос уже идёт! Дождитесь завершения.", show_alert=True)
        return
    if not os.path.exists(SESSION_PATH + ".session"):
        await callback.answer("⚠️ Сначала подключите технический аккаунт!", show_alert=True)
        return

    await state.set_state(TransferStates.waiting_source)
    await callback.message.edit_text(
        "1️⃣ <b>Шаг 1 из 3:</b> Отправьте ссылку или <code>@username</code> канала-источника, ОТКУДА брать посты\n"
        "(для закрытого канала отправьте ссылку-приглашение вида <code>https://t.me/+...</code>):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[red_btn("❌ Отмена", callback_data="back_main")]])
    )
    await callback.answer()


@router.message(TransferStates.waiting_source)
async def process_source(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(source=message.text.strip())
    await state.set_state(TransferStates.waiting_target)
    await message.answer(
        "2️⃣ <b>Шаг 2 из 3:</b> Отправьте ссылку или <code>@username</code> нового канала, КУДА заливать посты.\n\n"
        "⚠️ Подключённый аккаунт должен быть добавлен туда администратором с правом публикации!",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[red_btn("❌ Отмена", callback_data="back_main")]])
    )


@router.message(TransferStates.waiting_target)
async def process_target(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(target=message.text.strip())
    await state.set_state(TransferStates.waiting_limit)
    await message.answer(
        "3️⃣ <b>Шаг 3 из 3:</b> Сколько постов перенести за этот запуск?\n\n"
        "Напишите число (например, <code>10</code> или <code>50</code> — бот запомнит последний пост и при следующем запуске продолжит с него без повторов)\n"
        "Или отправьте <code>0</code>, чтобы перенести все оставшиеся посты канала снизу вверх.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[red_btn("❌ Отмена", callback_data="back_main")]])
    )


@router.message(TransferStates.waiting_limit)
async def process_limit(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    try:
        limit_val = int(message.text.strip())
        if limit_val < 0:
            raise ValueError
    except ValueError:
        await message.answer("⚠️ Введите целое число (например, <code>10</code> или <code>0</code>).", parse_mode="HTML")
        return

    data = await state.get_data()
    await state.clear()

    status_msg = await message.answer("⏳ Подключаюсь к каналам и проверяю историю переносов...")
    asyncio.create_task(run_transfer_task(bot, message.chat.id, status_msg.message_id, data["source"], data["target"], limit_val))


async def run_transfer_task(bot: Bot, chat_id: int, status_msg_id: int, source_str: str, target_str: str, limit_val: int):
    global is_transferring
    is_transferring = True
    os.makedirs(TEMP_DIR, exist_ok=True)
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            await bot.send_message(chat_id, "❌ Сессия аккаунта слетела. Подключите аккаунт заново.", reply_markup=main_menu())
            return

        source_entity = await resolve_entity(client, source_str)
        target_entity = await resolve_entity(client, target_str)

        pair_key = f"{source_entity.id}->{target_entity.id}"
        history = load_history()
        last_transferred_id = int(history.get(pair_key, 0))

        messages_to_send = []
        async for msg in client.iter_messages(source_entity, min_id=last_transferred_id, reverse=True):
            if msg.action is not None:
                continue
            if not msg.text and not msg.media:
                continue
            messages_to_send.append(msg)
            if limit_val > 0 and len(messages_to_send) >= limit_val:
                break

        total = len(messages_to_send)
        if total == 0:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_msg_id,
                text="✅ <b>Новых постов для переноса нет!</b> Все доступные посты из этого канала уже перенесены.",
                parse_mode="HTML",
                reply_markup=main_menu()
            )
            return

        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg_id,
            text=f"📦 Найдено новых постов для переноса: <b>{total}</b> (начинаю с ID #{messages_to_send[0].id}).\n⏳ Перенесено: <b>0 из {total}</b>...",
            parse_mode="HTML"
        )

        sent_count = 0
        for idx, msg in enumerate(messages_to_send, start=1):
            file_path = None
            try:
                caption_text = msg.text or ""
                if msg.media:
                    if hasattr(msg.media, "webpage"):
                        await client.send_message(target_entity, caption_text, formatting_entities=msg.entities, link_preview=True)
                    else:
                        file_path = await client.download_media(msg, file=TEMP_DIR)
                        if file_path:
                            await client.send_file(target_entity, file_path, caption=caption_text, formatting_entities=msg.entities)
                        elif caption_text:
                            await client.send_message(target_entity, caption_text, formatting_entities=msg.entities)
                elif caption_text:
                    await client.send_message(target_entity, caption_text, formatting_entities=msg.entities)

                sent_count += 1
                history[pair_key] = msg.id
                save_history(history)

                if idx % 3 == 0 or idx == total:
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=status_msg_id,
                            text=f"⏳ Идёт перенос постов...\nПеренесено: <b>{sent_count} из {total}</b> (последний ID #{msg.id})",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

                await asyncio.sleep(3)

            except FloodWaitError as fw:
                await bot.send_message(chat_id, f"⏸ Telegram попросил паузу {fw.seconds} сек. Жду и продолжаю автоматически...")
                await asyncio.sleep(fw.seconds + 2)
            except Exception as post_err:
                logging.error(f"Ошибка на посте #{msg.id}: {post_err}")
                history[pair_key] = msg.id
                save_history(history)
            finally:
                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except Exception:
                        pass

        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"🎉 <b>Перенос успешно завершён!</b>\n\n"
                f"✅ Перенесено постов за этот запуск: <b>{sent_count} из {total}</b>\n"
                f"📌 Запомнена точка остановки (пост <b>#{history.get(pair_key)}</b>).\n"
                f"При следующем запуске бот продолжит строго со следующего поста без повторов!"
            ),
            parse_mode="HTML",
            reply_markup=main_menu()
        )

    except Exception as e:
        await bot.send_message(chat_id, f"❌ <b>Ошибка во время переноса:</b>\n<code>{e}</code>", parse_mode="HTML", reply_markup=main_menu())
    finally:
        is_transferring = False
        await client.disconnect()
        if os.path.exists(TEMP_DIR):
            shutil.rmtree(TEMP_DIR, ignore_errors=True)


async def main():
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN:
        raise ValueError("Не задан BOT_TOKEN!")
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
