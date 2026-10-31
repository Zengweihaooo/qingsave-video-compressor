"""Where 轻存 keeps its own files, and the log that is written to disk as things happen.

  Windows  %APPDATA%\\QingSave\\
  macOS    ~/Library/Application Support/QingSave/

  logs/YYYY-MM-DD.log        every message shown on the pages, one line each, for reading later
  logs/events-YYYY-MM.jsonl  one JSON object per finished task / import (engine, sizes, seconds) for analysis
  queue.json                 the compression queue, so it can continue after a restart
  instance.json              port / token of the running copy, used by launch.py
"""
import datetime as dt
import json
import os
import sys
import threading
from pathlib import Path

if sys.platform == 'win32' and (Path(__file__).resolve().parent / '.portable-data').exists():
    DATA_DIR = Path(__file__).resolve().parent.parent / 'QingSave-data'
elif sys.platform == 'win32':
    DATA_DIR = Path(os.environ.get('APPDATA') or Path.home()) / 'QingSave'
elif sys.platform == 'darwin':
    DATA_DIR = Path.home() / 'Library' / 'Application Support' / 'QingSave'
else:
    DATA_DIR = Path.home() / '.qingsave'
LOG_DIR = DATA_DIR / 'logs'
_lock = threading.Lock()


def _append(path, line):
    with _lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a', encoding='utf8') as handle:
                handle.write(line + '\n')
        except OSError:
            pass  # logging must never break copying or encoding


def write(source, message):
    now = dt.datetime.now()
    for line in str(message).splitlines() or ['']:
        _append(LOG_DIR / f'{now:%Y-%m-%d}.log', f'{now:%Y-%m-%d %H:%M:%S}  [{source}] {line}')


def event(kind, **fields):
    now = dt.datetime.now()
    _append(LOG_DIR / f'events-{now:%Y-%m}.jsonl',
            json.dumps({'time': now.isoformat(timespec='seconds'), 'kind': kind, **fields}, ensure_ascii=False))


def save_json(name, data):
    path = DATA_DIR / name
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf8')
        os.replace(temp, path)


def load_json(name, default=None):
    try:
        return json.loads((DATA_DIR / name).read_text(encoding='utf8'))
    except (OSError, ValueError):
        return default
