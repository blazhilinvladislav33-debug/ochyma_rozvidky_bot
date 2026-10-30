"""Build a private, portable Windows deployment with a consistent SQLite snapshot."""
from contextlib import closing
from pathlib import Path
import os
import json
import sqlite3
import tempfile
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parent
FILES = (
    'bot.py', 'tickets.py', 'bookings.py', 'bookings_store.py',
    'channel_bridge.py', 'publications.py', 'posts_store.py', 'editorial.py',
    'news.py', 'requirements.txt', '.env', 'host_runtime.py', 'host_runner.py',
    'Install-Host.ps1', 'Install-Host.cmd', 'Host-Control.ps1', 'Host-Control.cmd',
    'export_host.py', 'Refresh-Bundle.cmd', 'HOST_README.md',
    'account_reader.py', 'reader_setup.py', 'Connect-Reader.cmd', 'READER_README.md',
    'source_health.py', 'CHANNEL_FEATURES.md',
    'ROADMAP.md',
    'FUTURE_ALERT_RULES.md',
    'alert_engine.py', 'alert_rules.py', 'ALERT_RULES_README.md',
    'web_panel.py', 'panel_setup.py', 'panel_status.py', 'Configure-Panel.cmd',
    'Update-Host.ps1', 'Update-Host.cmd', 'WEB_PANEL_README.md',
    'web_ui/index.html', 'web_ui/style.css', 'web_ui/app.js', 'web_ui/config.js',
    '.github/workflows/pages.yml', '.gitignore',
)


def referenced_media(snapshot, root):
    from account_reader import media_path
    names = set()
    def collect(value):
        if isinstance(value,dict):
            file_id=value.get('file_id')
            if isinstance(file_id,str) and file_id.startswith('reader:'):
                names.add(media_path(file_id,root))
            for item in value.values():collect(item)
        elif isinstance(value,list):
            for item in value:collect(item)
    with closing(sqlite3.connect(snapshot)) as db:
        tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table,column in (('posts','media'),('channel_deliveries','payload'),('reader_news','media'),('saved_templates','media'),('messages','media')):
            if table in tables:
                for row in db.execute(f'SELECT {column} FROM {table}'):
                    collect(json.loads(row[0] or '[]'))
    return sorted(names)


def build_bundle(root=ROOT):
    root = Path(root).resolve()
    for name in FILES:
        if not (root / name).is_file():
            raise FileNotFoundError(f'Missing deployment file: {name}')
    output = root / 'dist'
    output.mkdir(exist_ok=True)
    destination = output / 'TelegramBot-host.zip'
    with tempfile.TemporaryDirectory(prefix='host-build-', dir=output) as folder:
        staging = Path(folder)
        database = root / 'tickets.sqlite3'
        snapshot = staging / 'tickets.sqlite3'
        if database.exists():
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as source:
                with closing(sqlite3.connect(snapshot)) as target:
                    source.backup(target)
                    if target.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise RuntimeError('Database snapshot failed verification')
        archive = staging / 'bundle.zip'
        with ZipFile(archive, 'w', compression=ZIP_DEFLATED) as bundle:
            for name in FILES:
                bundle.write(root / name, name)
            if snapshot.exists():
                bundle.write(snapshot, snapshot.name)
                for media in referenced_media(snapshot, root):
                    bundle.write(media, 'reader_media/'+media.name)
        os.replace(archive, destination)
    return destination


def build_update(root=ROOT):
    """Code-only update: never replace a live host's database, login or settings."""
    root=Path(root).resolve()
    files=[name for name in FILES if name!='.env']
    for name in files:
        if not (root/name).is_file():raise FileNotFoundError(f'Missing deployment file: {name}')
    output=root/'dist';output.mkdir(exist_ok=True)
    destination=output/'TelegramBot-update.zip'
    with tempfile.TemporaryDirectory(prefix='update-build-',dir=output) as folder:
        temporary=Path(folder)/'update.zip'
        with ZipFile(temporary,'w',compression=ZIP_DEFLATED) as bundle:
            for name in files:bundle.write(root/name,name)
        os.replace(temporary,destination)
    return destination


if __name__ == '__main__':
    print(build_bundle())
    print(build_update())
    # This is the ONLY source archive intended for uploading to GitHub.
    with ZipFile(ROOT/'dist'/'TelegramBot-GitHub-source.zip','w',compression=ZIP_DEFLATED) as bundle:
        for name in FILES:
            if name!='.env':bundle.write(ROOT/name,name)
