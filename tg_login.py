# -*- coding: utf-8 -*-
"""Вход в Telegram без интерактива: двухшаговая схема.

  python tg_login.py --send          отправляет код на номер из .env (TG_PHONE)
  python tg_login.py --check 12345   вход по коду, который пришёл в Telegram
При включённой 2FA скрипт попросит пароль из .env (TG_PASSWORD) отдельным шагом.
Состояние в tg_login_state.json, сессия в tg_session.session — оба не коммитятся.
"""
import os
import sys
import json
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")

API_ID = os.getenv("TG_API_ID", "")
API_HASH = os.getenv("TG_API_HASH", "")
PHONE = os.getenv("TG_PHONE", "")
PASSWORD = os.getenv("TG_PASSWORD", "")
STATE_FILE = BASE / "tg_login_state.json"
SESSION_FILE = "tg_session"


async def main():
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError

    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if not API_ID or not API_HASH:
        print("ERR: в .env нет TG_API_ID / TG_API_HASH")
        return
    client = TelegramClient(SESSION_FILE, int(API_ID), API_HASH)
    await client.connect()
    try:
        if cmd == "--send":
            if not PHONE:
                print("ERR: добавь в .env строку TG_PHONE=+7XXXXXXXXXX")
                return
            sent = await client.send_code_request(PHONE)
            hash_ = sent.phone_code_hash
            STATE_FILE.write_text(json.dumps({"phone": PHONE, "hash": hash_}), "utf-8")
            digits = PHONE[-2:] if len(PHONE) >= 2 else "**"
            print(f"OK код отправлен на номер, оканчивающийся на …{digits}. Жду код.")
            return
        if cmd == "--check":
            if not STATE_FILE.exists():
                print("ERR: сначала --send")
                return
            st = json.loads(STATE_FILE.read_text("utf-8"))
            code = sys.argv[2].strip() if len(sys.argv) > 2 else ""
            if not code.isdigit():
                print("ERR: код должен быть цифрами")
                return
            try:
                await client.sign_in(st["phone"], code, phone_code_hash=st["hash"])
            except SessionPasswordNeededError:
                if not PASSWORD:
                    print("ERR: включена 2FA — добавь в .env TG_PASSWORD=твойпароль и повтори")
                    return
                await client.sign_in(password=PASSWORD)
            me = await client.get_me()
            STATE_FILE.unlink(missing_ok=True)
            uname = f"@{me.username}" if me.username else "—"
            print(f"OK вход выполнен: {me.first_name} {uname}. Сессия сохранена (tg_session.session).")
            return
        print("Команды: --send | --check <код>")
    finally:
        await client.disconnect()


def run():
    import asyncio
    try:
        asyncio.run(main())
    except Exception as e:
        name = type(e).__name__
        seconds = getattr(e, "seconds", None)
        if seconds:
            print(f"FloodWait: Telegram просит ждать {seconds} секунд")
        else:
            print(f"{name}: {e}")


if __name__ == "__main__":
    run()
