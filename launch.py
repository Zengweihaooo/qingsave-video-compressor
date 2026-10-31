"""Start 轻存 in the background, or reopen its page if it is already running.

The server keeps running when the browser is closed; this script just finds it again.

  launch.py           start if needed, then open the page
  launch.py --quit    stop the running copy safely (the unfinished queue is kept for next time)
"""
import http.client
import json
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, 'reconfigure'):
        stream.reconfigure(encoding='utf8', errors='backslashreplace')

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import applog  # noqa: E402


def running():
    info = applog.load_json('instance.json')
    if not info:
        return None
    try:
        connection = http.client.HTTPConnection('127.0.0.1', info['port'], timeout=2)
        connection.request('GET', '/health', headers={'Host': f"127.0.0.1:{info['port']}"})
        if connection.getresponse().status == 200:
            return info
    except (OSError, KeyError):
        pass
    return None


def message(text):
    """Show a short note even when started without a console (pythonw)."""
    if sys.platform == 'win32' and (sys.stdout is None or not sys.stdout.isatty()):
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, '轻存', 0x40)
    elif sys.stdout is not None:
        print(text)


def start():
    python = Path(sys.executable)
    if sys.platform == 'win32':
        windowless = python.with_name('pythonw.exe')
        command = [str(windowless if windowless.exists() else python), '-X', 'utf8', str(HERE / 'web_app.py'), '--no-open']
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        subprocess.Popen(command, cwd=HERE, creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        log = applog.LOG_DIR / 'console.log'
        log.parent.mkdir(parents=True, exist_ok=True)
        subprocess.Popen([str(python), '-X', 'utf8', str(HERE / 'web_app.py'), '--no-open'], cwd=HERE, start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=open(log, 'a'), stderr=subprocess.STDOUT)
    for _ in range(60):  # up to 15 s
        time.sleep(0.25)
        info = running()
        if info:
            return info
    return None


def main():
    if '--quit' in sys.argv:
        info = running()
        if not info:
            message('轻存没有在运行。'); return
        connection = http.client.HTTPConnection('127.0.0.1', info['port'], timeout=30)
        connection.request('POST', '/quit', body='{}', headers={'Host': f"127.0.0.1:{info['port']}", 'X-App-Token': info['token'],
                                                             'Content-Type': 'application/json'})
        connection.getresponse()
        message('轻存正在安全退出：正在处理的那一个会停下，没做完的下次打开可以继续。')
        return
    info = running() or start()
    if not info:
        message(f'轻存没能启动。请查看日志文件夹：{applog.LOG_DIR}'); return
    webbrowser.open(f"http://127.0.0.1:{info['port']}/")


if __name__ == '__main__':
    main()
