import asyncio
import os
import shutil
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import ImportChatInviteRequest

API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"

SOURCE_CHANNEL = "@dizainerykarti"
TARGET_CHANNEL = "@testhubing230"

DELAY_SECONDS = 10
LIMIT_POSTS = 5

TEMP_DIR = "temp_media"


async def resolve_source(client: TelegramClient, source: str):
    if "t.me/+" in source or "joinchat/" in source:
        invite_hash = source.split("+")[-1].split("/")[-1].strip()
        try:
            updates = await client(ImportChatInviteRequest(invite_hash))
            if hasattr(updates, "chats") and updates.chats:
                return updates.chats[0]
        except Exception:
            pass
    return await client.get_entity(source)


async def main():
    if API_ID == 12345678 or "вставь_сюда" in API_HASH:
        print("ОШИБКА: Сначала впиши свои API_ID и API_HASH в начале файла transfer.py!")
        return

    os.makedirs(TEMP_DIR, exist_ok=True)

    client = TelegramClient("transfer_session", API_ID, API_HASH)
    await client.start()
    print("Успешный вход в аккаунт!")

    source_entity = await resolve_source(client, SOURCE_CHANNEL)
    target_entity = await client.get_entity(TARGET_CHANNEL)

    print(f"Откуда: {getattr(source_entity, 'title', SOURCE_CHANNEL)}")
    print(f"Куда:   {getattr(target_entity, 'title', TARGET_CHANNEL)}")
    print("Собираю список постов... Подождите немного.")

    messages = []
    async for msg in client.iter_messages(source_entity, limit=LIMIT_POSTS, reverse=True):
        if msg.action is not None:
            continue
        if not msg.text and not msg.media:
            continue
        messages.append(msg)

    total = len(messages)
    print(f"Найдено постов для переноса: {total}")

    copied = 0
    for idx, msg in enumerate(messages, start=1):
        downloaded_path = None
        try:
            caption = msg.text or ""

            if msg.media and not msg.web_preview:
                print(f"[{idx}/{total}] Скачиваю медиа...")
                downloaded_path = await client.download_media(msg, file=TEMP_DIR)

            if downloaded_path:
                await client.send_file(
                    target_entity,
                    file=downloaded_path,
                    caption=caption,
                    formatting_entities=msg.entities,
                )
            elif caption:
                await client.send_message(
                    target_entity,
                    message=caption,
                    formatting_entities=msg.entities,
                )
            else:
                continue

            copied += 1
            print(f"[{idx}/{total}] Перенесён успешно (всего: {copied})")
            await asyncio.sleep(DELAY_SECONDS)

        except FloodWaitError as e:
            print(f"Пауза от Telegram на {e.seconds} сек...")
            await asyncio.sleep(e.seconds + 2)
        except Exception as e:
            print(f"[{idx}/{total}] Пропущен из-за ошибки: {e}")
        finally:
            if downloaded_path and os.path.exists(downloaded_path):
                try:
                    os.remove(downloaded_path)
                except Exception:
                    pass

    if os.path.exists(TEMP_DIR):
        shutil.rmtree(TEMP_DIR, ignore_errors=True)

    print(f"ГОТОВО! Успешно перенесено постов: {copied} из {total}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
