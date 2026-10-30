"""Local account login only. Never enter account credentials through the bot."""
import asyncio
from getpass import getpass
import json
import logging
from pathlib import Path
from host_runtime import single_instance, AlreadyRunning

ROOT = Path(__file__).resolve().parent


async def setup():
    from telethon import TelegramClient
    folder = ROOT / '.reader'
    folder.mkdir(exist_ok=True)
    path = folder / 'credentials.json'
    if path.exists():
        credentials = json.loads(path.read_text(encoding='utf-8'))
    else:
        print('Отримайте власні API ID та API Hash: https://my.telegram.org → API development tools')
        api_id = int(input('API ID: ').strip())
        api_hash = getpass('API Hash (ввід прихований): ').strip()
        if api_id <= 0 or len(api_hash) != 32 or any(c not in '0123456789abcdefABCDEF' for c in api_hash):
            raise ValueError('Некоректні API ID / API Hash')
        credentials = {'api_id': api_id, 'api_hash': api_hash}
    client = TelegramClient(str(folder / 'account'), credentials['api_id'], credentials['api_hash'],
                            device_model='Information Channel Reader', flood_sleep_threshold=0)
    try:
        await client.start(phone=lambda: getpass('Номер Telegram з кодом країни (ввід прихований): '),
                           code_callback=lambda: getpass('Код входу Telegram (ввід прихований): '),
                           password=lambda: getpass('Пароль двоетапної перевірки Telegram (ввід прихований): '))
        me = await client.get_me()
        if me.bot:
            raise ValueError('Потрібен користувацький акаунт Telegram')
        path.write_text(json.dumps(credentials), encoding='utf-8')
        print('\nАкаунт підключено. Сесія збережена тільки на цьому хості.')
        print('Канали, на які підписаний акаунт:')
        async for dialog in client.iter_dialogs():
            if getattr(dialog.entity, 'broadcast', False):
                username = getattr(dialog.entity, 'username', None)
                print(f'  {dialog.name} | {"@"+username if username else dialog.id}')
        print('\nЗапустіть бота. У боті: Канали → Джерело через акаунт → посилання або ID.')
        print('Для кожного джерела виберіть погодження або публікацію одразу.')
    finally:
        await client.disconnect()


def main():
    logging.basicConfig(level=logging.ERROR)
    try:
        with single_instance(ROOT / '.runtime' / 'reader.lock'):
            asyncio.run(setup())
    except AlreadyRunning:
        print('Спершу зупиніть бота через Host-Control.cmd → 3. Потім повторіть підключення.')
        return 1
    except (KeyboardInterrupt, EOFError):
        print('\nПідключення скасовано.')
        return 1
    except Exception as exc:
        print('Підключення не завершено: '+type(exc).__name__+'. Перевірте дані та з’єднання. Повторіть пізніше, якщо Telegram обмежив спроби.')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
