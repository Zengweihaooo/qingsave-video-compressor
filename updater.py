"""Pull the latest 轻存 from GitHub and restart onto it.

A git checkout (the Mac) updates with `git fetch` + fast-forward.  The portable Windows copy has no .git, so it
downloads the branch as a zip and overlays the program files; runtime/, bin/ and the data folder are never touched.
"""
import ast
import io
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO = 'Zengweihaooo/qingsave-video-compressor'
BRANCH = 'main'
ROOT = Path(__file__).resolve().parent
VERSION_FILE = ROOT / '.version'
BACKUP = ROOT / '.update-backup'
PROGRAM_DIRS = ('vendor', 'tools')
SKIP_FILES = {'.gitignore', '.gitattributes', '.version', '.portable-data'}


def _get(url, accept=None):
    request = urllib.request.Request(url, headers={'User-Agent': 'QingSave-updater', **({'Accept': accept} if accept else {})})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except OSError as error:
        raise RuntimeError(f'连不上 GitHub：{error}') from error


def _git(*args, check=True):
    kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    result = subprocess.run(['git', '-C', str(ROOT), *args], capture_output=True, text=True, encoding='utf8', timeout=120,
                            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}, **kwargs)
    if check and result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[-400:] or 'git 出错')
    return result.stdout.strip()


def uses_git():
    return (ROOT / '.git').exists() and shutil.which('git') is not None


def current():
    if uses_git():
        return _git('rev-parse', 'HEAD')
    try:
        return VERSION_FILE.read_text(encoding='utf8').strip()
    except OSError:
        return ''


def check_syntax():
    """A broken download must never replace a working program: refuse to restart onto code that does not even parse."""
    for path in ROOT.glob('*.py'):
        try:
            ast.parse(path.read_text(encoding='utf8'), str(path))
        except (SyntaxError, ValueError, OSError) as error:
            raise RuntimeError(f'新版本 {path.name} 有语法错误，已恢复旧版：{error}') from error


def _update_git():
    old = _git('rev-parse', 'HEAD')
    _git('fetch', 'origin', BRANCH)
    _git('merge', '--ff-only', 'FETCH_HEAD')
    new = _git('rev-parse', 'HEAD')
    if new != old:
        try:
            check_syntax()
        except RuntimeError:
            _git('reset', '--keep', old)
            raise
    return old, new, _git('log', '-1', '--format=%s') if new != old else ''


def _update_zip():
    old = current()
    latest = json.loads(_get(f'https://api.github.com/repos/{REPO}/commits/{BRANCH}', 'application/vnd.github+json'))
    new, subject = latest['sha'], latest['commit']['message'].splitlines()[0]
    if new == old:
        return old, new, ''
    archive = zipfile.ZipFile(io.BytesIO(_get(f'https://codeload.github.com/{REPO}/zip/refs/heads/{BRANCH}')))
    names = [n for n in archive.namelist() if not n.endswith('/')]
    prefix = names[0].split('/')[0] + '/'
    shutil.rmtree(BACKUP, ignore_errors=True)
    BACKUP.mkdir()
    written = []

    def put(relative, data):
        target = ROOT / relative
        if target.exists():
            (BACKUP / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, BACKUP / relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        written.append(relative)

    try:
        for name in names:
            relative = name[len(prefix):]
            top = relative.split('/')[0]
            if (('/' in relative and top in PROGRAM_DIRS) or ('/' not in relative and relative not in SKIP_FILES)) and not relative.startswith('.'):
                put(relative, archive.read(name))
        check_syntax()
    except Exception:
        for relative in written:
            saved = BACKUP / relative
            if saved.exists():
                shutil.copy2(saved, ROOT / relative)
            else:
                (ROOT / relative).unlink(missing_ok=True)
        raise
    VERSION_FILE.write_text(new, encoding='utf8')
    return old, new, subject


def run():
    """Fetch and apply the newest version.  Returns what changed; the caller restarts when `changed`."""
    old, new, subject = _update_git() if uses_git() else _update_zip()
    return {'changed': old != new, 'old': old[:7], 'new': new[:7], 'subject': subject, 'method': 'git' if uses_git() else 'zip'}


def relaunch(port):
    """Start the same command again on the same port (the page reloads there)."""
    argv = list(getattr(sys, 'orig_argv', None) or [sys.executable, *sys.argv])
    argv[0] = sys.executable
    cleaned, skip = [], False
    for item in argv:
        if skip:
            skip = False
        elif item == '--port':
            skip = True
        elif not item.startswith('--port='):
            cleaned.append(item)
    cleaned += ['--port', str(port)]
    if os.name == 'nt':
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        subprocess.Popen(cleaned, cwd=ROOT, creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        os.chdir(ROOT)
        os.execv(cleaned[0], cleaned)  # same pid, so the Mac app window keeps supervising it
