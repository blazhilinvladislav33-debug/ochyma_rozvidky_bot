"""Interactive local setup. Never prints or exports the OIDC client secret."""
from getpass import getpass
import json
from pathlib import Path
from dotenv import dotenv_values
from web_panel import PanelConfig

ROOT=Path(__file__).resolve().parent


def main():
    path=ROOT/'.panel.env';old=dotenv_values(path)
    print('Налаштування вебпанелі. Дані зберігатимуться лише на цьому ПК.')
    print('Панель GitHub та сервер бота мають окремі адреси. Див. WEB_PANEL_README.md.')
    base=input('HTTPS-адреса сервера (або Enter для локальної http://127.0.0.1:8787): ').strip() or old.get('WEB_BASE_URL') or 'http://127.0.0.1:8787'
    front=input('Адреса панелі на GitHub Pages (Enter — панель на цьому ПК): ').strip() or old.get('WEB_FRONTEND_URL') or base+'/'
    print('У BotFather → ваш бот → Login Widget додайте адресу панелі й адресу зворотного входу:')
    print(base.rstrip('/')+'/auth/callback')
    client=input('Client ID від BotFather (Enter — залишити поточний): ').strip() or old.get('WEB_CLIENT_ID') or ''
    secret=getpass('Client Secret (не показується; Enter — залишити поточний): ') or old.get('WEB_CLIENT_SECRET') or ''
    if client and not client.isdigit():raise ValueError('Client ID має бути числом.')
    values={'WEB_ENABLED':'1','WEB_BASE_URL':base.rstrip('/'),'WEB_FRONTEND_URL':front,
            'WEB_CLIENT_ID':client,'WEB_CLIENT_SECRET':secret,'WEB_HOST':'127.0.0.1','WEB_PORT':'8787'}
    # Validate before replacing any existing configuration.
    from unittest.mock import patch
    with patch('web_panel.dotenv_values',return_value=values):PanelConfig.load(ROOT)
    path.write_text('\n'.join(k+'='+json.dumps(v,ensure_ascii=False) for k,v in values.items())+'\n',encoding='utf-8')
    print('Збережено. Перезапустіть Host-Control → 4. Секрети не додавайте на GitHub.')
    if not client or not secret:print('Вхід через Telegram залишиться вимкненим, доки не введете Client ID та Client Secret.')


if __name__=='__main__':
    try:main()
    except (ValueError,KeyboardInterrupt) as exc:print('Налаштування не збережено:',str(exc))
