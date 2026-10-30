"""Unattended entry point. No Telegram calls are made by --check."""
import argparse
import asyncio
from contextlib import closing
from datetime import datetime,timezone
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import sqlite3
import sys
import time
from host_runtime import single_instance,AlreadyRunning

ROOT=Path(__file__).resolve().parent
TOKEN_PATTERN=re.compile(r'(?<!\d)\d{5,}:[A-Za-z0-9_-]{20,}')


class PrivateFormatter(logging.Formatter):
    def format(self,record):
        return TOKEN_PATTERN.sub('[BOT_TOKEN]',super().format(record))


def setup_logging(root=ROOT):
    folder=Path(root)/'logs';folder.mkdir(parents=True,exist_ok=True)
    formatter=PrivateFormatter('%(asctime)s %(levelname)s %(name)s: %(message)s')
    file=RotatingFileHandler(folder/'bot.log',maxBytes=5*1024*1024,backupCount=5,encoding='utf-8')
    file.setFormatter(formatter)
    logger=logging.getLogger();logger.setLevel(logging.INFO);logger.addHandler(file)
    if sys.stderr is not None:
        console=logging.StreamHandler();console.setFormatter(formatter);logger.addHandler(console)


def check_install(root=ROOT):
    root=Path(root).resolve()
    from dotenv import dotenv_values
    from aiogram.utils.token import validate_token
    from zoneinfo import ZoneInfo
    import tzdata
    import telethon
    import jwt
    config=dotenv_values(root/'.env')
    if not config.get('BOT_TOKEN'):
        raise RuntimeError('Не знайдено BOT_TOKEN у файлі .env.')
    try:validate_token(config['BOT_TOKEN'].strip())
    except Exception:raise RuntimeError('Перевірте формат BOT_TOKEN у .env.') from None
    ZoneInfo('Europe/Kyiv')
    runtime=root/'.runtime';runtime.mkdir(exist_ok=True)
    import tempfile
    with tempfile.TemporaryFile(dir=runtime):pass
    dbpath=root/'tickets.sqlite3'
    if dbpath.exists():
        uri=dbpath.as_uri()+'?mode=ro'
        with closing(sqlite3.connect(uri,uri=True)) as db:
            result=db.execute('PRAGMA quick_check').fetchone()[0]
            if result!='ok':raise RuntimeError('База даних не пройшла перевірку. Використайте справну резервну копію.')
    return True


async def supervise(run,close_session,wait=asyncio.sleep):
    delay=5
    while True:
        started=time.monotonic()
        try:
            await run()
            return
        except asyncio.CancelledError:raise
        except Exception:
            if time.monotonic()-started>=300:delay=5
            logging.exception('Бот зупинився. Наступна спроба запуску через %s с.',delay)
            try:await close_session()
            except Exception:logging.exception('Не вдалося закрити попереднє з’єднання.')
            await wait(delay)
            delay=min(60,delay*2)


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--check',action='store_true',help='Перевірка файлів без підключення до Telegram')
    args=parser.parse_args(argv)
    if args.check:
        check_install()
        print('OK: залежності, налаштування, часовий пояс і база даних перевірені.')
        return 0
    setup_logging()
    try:
        with single_instance(ROOT/'.runtime'/'bot.lock'):
            check_install()
            import bot as application
            status=ROOT/'.runtime'/'host.json'
            status.write_text(json.dumps({'pid':__import__('os').getpid(),'started_utc':datetime.now(timezone.utc).isoformat()},indent=2),encoding='utf-8')
            logging.info('Запуск у режимі Windows-хоста.')
            from web_panel import run_services
            try:asyncio.run(run_services(application))
            finally:application.store.db.close()
    except AlreadyRunning as exc:
        logging.warning('%s',exc)
        return 0
    except KeyboardInterrupt:
        logging.info('Зупинено користувачем.')
        return 0
    except Exception:
        logging.exception('Не вдалося запустити хост. Перевірте файли та налаштування.')
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
