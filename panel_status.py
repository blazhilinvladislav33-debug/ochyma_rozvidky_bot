"""Read-only local panel health. Safe for the Host-Control status screen."""
import json
from urllib.request import urlopen
from web_panel import PanelConfig

try:
    config=PanelConfig.load()
    if not config.enabled:print('Web panel: disabled')
    else:
        try:
            with urlopen('http://127.0.0.1:'+str(config.port)+'/health',timeout=2) as response:
                data=json.load(response)
            print('Web panel: running | Telegram login: '+('configured' if data.get('login_configured') else 'run Configure-Panel.cmd'))
        except Exception:print('Web panel: not responding')
        print('Panel address: '+config.frontend_url)
except Exception:print('Web panel: configuration needs attention')
