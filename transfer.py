import asyncio
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
from telethon.errors import SessionPasswordNeededError
from telethon.tl.functions.messages import ImportChatInviteRequest

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID")) if os.getenv("ADMIN_ID") else None
DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)

API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"
SESSION_PATH = os.path.join(DATA_DIR, "user_transfer_session")

router = Router()
active_clients = {}
transfer_running = False


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
        [red_btn("🗑 Сбросить сессию аккаунта", callback_data="reset_session")],
    ])


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ закрыт.")
        return
    await message.answer(
        "👋 <b>Бот-переносчик Telegram-каналов готов!</b>\n\n"
        "1. Сначала подключите технический аккаунт Telegram.\n"
        "2. Затем укажите старый и новый каналы для переноса постов.",
        parse_mode="HTML",
        reply_markup=main_menu()
    )


@router.callback_query(F.data == "back_main")
async def cb_back_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if not is_admin(callback.from_user.id):
        return
    await callback.message.edit_text(
        "👋 <b>Главное меню переносчика</b>\n\nВыберите действие:",
        parse_mode="HTML",
        reply_markup=main_menu()
    )
    await callback.answer()


@router.callback_query(F.data == "reset_session")
async def cb_reset_session(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    for ext in ("", ".session", ".session-journal"):
        path = SESSION_PATH + ext
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass
    await callback.answer("✅ Сессия удалена! Можно подключить новый аккаунт.", show_alert=True)


@router.callback_query(F.data == "auth_start")
async def cb_auth_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        await client.disconnect()
        await callback.answer(f"✅ Аккаунт уже подключён: {me.first_name}", show_alert=True)
        return
    await client.disconnect()

    await state.set_state(AuthStates.waiting_phone)
    await callback.message.edit_text(
        "📱 <b>Шаг 1:</b> Отправьте номер телефона технического аккаунта Telegram в международном формате.\n\n"
        "Например: <code>+79991234567</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[red_btn("❌ Отмена", callback_data="back_main")]])
    )
    await callback.answer()


@router.message(AuthStates.waiting_phone)
async def process_phone(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    phone = message.text.strip().replace(" ", "").replace("-", "")
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        active_clients[message.from_user.id] = {
            "client": client,
            "phone": phone,
            "phone_code_hash": sent.phone_code_hash
        }
        await state.set_state(AuthStates.waiting_code)
        await message.answer(
            "🔑 <b>Шаг 2:</b> Код подтверждения отправлен в Telegram!\n\n"
            "Пришлите его сюда (чтобы Telegram не сжёг код, лучше введите с пробелом или точкой, например <code>12 345</code>):",
            parse_mode="HTML"
        )
    except Exception as e:
        await client.disconnect()
        await message.answer(f"❌ Ошибка отправки кода: {e}")


@router.message(AuthStates.waiting_code)
async def process_code(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    ctx = active_clients.get(message.from_user.id)
    if not ctx:
        await state.clear()
        await message.answer("⚠️ Сессия истекла, начните заново.", reply_markup=main_menu())
        return

    code = "".join(ch for ch in message.text if ch.isdigit())
    client: TelegramClient = ctx["client"]
    try:
        await client.sign_in(phone=ctx["phone"], code=code, phone_code_hash=ctx["phone_code_hash"])
        me = await client.get_me()
        await client.disconnect()
        active_clients.pop(message.from_user.id, None)
        await state.clear()
        await message.answer(
            f"✅ Аккаунт <b>{me.first_name}</b> успешно подключён!\nТеперь можно запускать перенос постов.",
            parse_mode="HTML",
            reply_markup=main_menu()
        )
    except SessionPasswordNeededError:
        await state.set_state(AuthStates.waiting_password)
        await message.answer("🔐 На аккаунте стоит облачный пароль (2FA). Отправьте его сюда:")
    except Exception as e:
        await message.answer(f"❌ Ошибка входа: {e}")


@router.message(AuthStates.waiting_password)
async def process_password(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    ctx = active_clients.get(message.from_user.id)
    if not ctx:
        await state.clear()
        return
    client: TelegramClient = ctx["client"]
    try:
        await client.sign_in(password=message.text.strip())
        me = await client.get_me()
        await client.disconnect()
        active_clients.pop(message.from_user.id, None)
        await state.clear()
        await message.answer(
            f"✅ Аккаунт <b>{me.first_name}</b> успешно подключён!",
            parse_mode="HTML",
            reply_markup=main_menu()
        )
    except Exception as e:
        await message.answer(f"❌ Неверный пароль или ошибка: {e}")


@router.callback_query(F.data == "transfer_start")
async def cb_transfer_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    global transfer_running
    if transfer_running:
        await callback.answer("⏳ Перенос уже идёт! Дождитесь завершения.", show_alert=True)
        return

    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    await client.connect()
    authorized = await client.is_user_authorized()
    await client.disconnect()
    if not authorized:
        await callback.answer("⚠️ Сначала подключите аккаунт (кнопка 1)!", show_alert=True)
        return

    await state.set_state(TransferStates.waiting_source)
    await callback.message.edit_text(
        "📥 <b>Шаг 1 из 3:</b> Отправьте ссылку или <code>@username</code> канала, <b>ОТКУДА</b> нужно перенести посты.\n\n"
        "Подойдёт и публичный канал (<code>@channel</code>), и закрытый по ссылке-приглашению (<code>https://t.me/+...</code>).",
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
        "📤 <b>Шаг 2 из 3:</b> Отправьте ссылку или <code>@username</code> нового канала, <b>КУДА</b> заливать посты.\n\n"
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
        "🔢 <b>Шаг 3 из 3:</b> Сколько последних постов перенести?\n\n"
        "Напишите число (например, <code>5</code> для теста или <code>0</code>, чтобы перенести вообще все посты канала снизу вверх).",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[red_btn("❌ Отмена", callback_data="back_main")]])
    )


@router.message(TransferStates.waiting_limit)
async def process_limit(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    try:
        limit = int(message.text.strip())
        if limit < 0:
            raise ValueError
    except ValueError:
        await message.answer("⚠️ Введите целое число (например, <code>5</code> или <code>0</code>).", parse_mode="HTML")
        return

    data = await state.get_data()
    await state.clear()
    await message.answer("⏳ <b>Начинаю перенос постов...</b> Я пришлю отчёт по завершении!", parse_mode="HTML", reply_markup=main_menu())
    asyncio.create_task(run_transfer_task(bot, message.from_user.id, data["source"], data["target"], limit))


async def resolve_entity(client: TelegramClient, link: str):
    if "t.me/+" in link or "joinchat/" in link:
        invite_hash = link.split("+")[-1].split("/")[-1].strip()
        try:
            updates = await client(ImportChatInviteRequest(invite_hash))
            if hasattr(updates, "chats") and updates.chats:
                return updates.chats[0]
        except Exception:
            pass
    return await client.get_entity(link)


async def run_transfer_task(bot: Bot, admin_id: int, source_link: str, target_link: str, limit: int):
    global transfer_running
    transfer_running = True
    temp_dir = os.path.join(DATA_DIR, "temp_transfer_media")
    os.makedirs(temp_dir, exist_ok=True)
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    copied = 0
    errors = 0

    try:
        await client.connect()
        src = await resolve_entity(client, source_link)
        dst = await resolve_entity(client, target_link)

        fetch_limit = limit if limit > 0 else None
        messages = []
        async for msg in client.iter_messages(src, limit=fetch_limit):
            if msg.action:
                continue
            if msg.text or msg.media:
                messages.append(msg)

        messages.reverse()
        await bot.send_message(admin_id, f"📦 Найдено постов для переноса: <b>{len(messages)}</b>. Начинаю публикацию...", parse_mode="HTML")

        for msg in messages:
            try:
                text = msg.text or ""
                if msg.media:
                    file_path = await client.download_media(msg, file=temp_dir)
                    if file_path:
                        await client.send_file(dst, file=file_path, caption=text)
                        try:
                            os.remove(file_path)
                        except Exception:
                            pass
                    elif text:
                        await client.send_message(dst, text)
                elif text:
                    await client.send_message(dst, text)

                copied += 1
                await asyncio.sleep(3)
            except Exception as e:
                errors += 1
                logging.error(f"Ошибка поста #{msg.id}: {e}")
                await asyncio.sleep(5)

        await bot.send_message(
            admin_id,
            f"✅ <b>Перенос завершён!</b>\n\nУспешно перенесено: <b>{copied}</b>\nПропущено с ошибкой: <b>{errors}</b>",
            parse_mode="HTML"
        )
    except Exception as e:
        await bot.send_message(admin_id, f"❌ <b>Ошибка при переносе:</b>\n<code>{e}</code>", parse_mode="HTML")
    finally:
        await client.disconnect()
        shutil.rmtree(temp_dir, ignore_errors=True)
        transfer_running = False


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
