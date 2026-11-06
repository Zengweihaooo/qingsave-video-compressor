"""Loopback-only browser interface for the local compressor."""
import json
import datetime as dt
import os
import re
import secrets
import threading
import queue
import types
import webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.parse
import uuid
import compressor as c
import osv_pipeline as osv
import sd_import as sd
import library
import applog
import temp_storage as storage
import space
import updater
import folder_tags
import retime
import time
import processing_time
import pipeline_runtime as pipeline

TOKEN = secrets.token_urlsafe(32)
BUILD = 'osv-preview-2'
state = {'busy': False, 'logs': [], 'files': []}
lock = threading.Lock()
worker = types.SimpleNamespace(events=queue.Queue(), cancel=threading.Event(), manifests=[], progress={}, progress_lock=threading.Lock())
task_condition = threading.Condition()
tasks = []
dispatcher_thread = None
queue_paused = False
queue_stopping = False
current_owner = None
active_owners = {}
exiting = False
QUEUE_FILE = 'queue.json'
TIME_ESTIMATOR = processing_time.Estimator()
PREFERRED_PORT = 39390  # fixed so a bookmark / the launcher always finds the running copy
TEMP_PARTIAL_RE = re.compile(r'^\.[0-9a-f]{32}\.partial\.mp4$')
hardware = {'status': 'idle', 'items': [], 'gpu': '', 'auto': ''}
DROP_ROOT = applog.DATA_DIR / 'drop-staging'
DROP_ROOT.mkdir(parents=True, exist_ok=True)
staging = tempfile.TemporaryDirectory(prefix='dji-drop-', dir=DROP_ROOT)
dropped = set()
scan_dates = {}
origin_cache = {}

SKIP_DIRS = {'$RECYCLE.BIN', 'System Volume Information', '.accelerate', 'Temp', '__pycache__', '.venv'}
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.mkv', '.osv'}
if sys.platform == 'win32':
    _, _targets = sd.list_volumes(())
    LIBRARY_ROOTS = tuple(Path(t['mount']) for t in sorted(_targets, key=lambda t: (t['name'].lower()!='data',t['name'].lower())))
else:
    LIBRARY_ROOTS = (Path('/Volumes/Data'), Path('/Volumes/Weihao'))
# 10.12、10.12 第一次见 rebelo、10.13-瞎逛健身房 are the same day's folder.
DATE_DIR_RE = re.compile(r'^(\d{1,2})\.(\d{1,2})(?!\d)(.*)$')

def explicit_compressed(path):
    stem = Path(path).stem
    return bool(re.search(r'(?:^|[_. -])(cmp|compressed)(?:$|[_. -])', stem, re.I))

def origin_profile(path):
    path = Path(path)
    state_key = (str(path), path.stat().st_size, path.stat().st_mtime_ns)
    if state_key in origin_cache: return origin_cache[state_key]
    try:
        info = c.probe(path); stream = c.video(info)
        numerator, denominator = map(float, stream.get('avg_frame_rate', '0/1').split('/'))
        fps = numerator / denominator if denominator else 0
        bitrate = int(stream.get('bit_rate') or info.get('format', {}).get('bit_rate') or 0)
        pixels = int(stream.get('width', 0)) * int(stream.get('height', 0))
        bppf = bitrate / pixels / fps if bitrate and pixels and fps else 0
        codec = stream.get('codec_name', '').lower()
        low = 0.20 if codec in ('h264', 'avc1') else 0.18
        high = 0.23 if codec in ('h264', 'avc1') else 0.20
        kind = 'likely_compressed' if bppf and bppf <= low else ('camera_source' if bppf >= high else 'unknown')
        # OBS is screen-recording output rather than action-camera footage.  A low
        # normalized bitrate is still useful evidence that it should be skipped,
        # but a high value alone must not label it as a camera original.
        if re.search(r'(?i)(?:^|[_.-])OBS(?:[_.-]|$)', path.stem) and kind == 'camera_source':
            kind = 'unknown'
        result = {'kind': kind, 'bitrate': bitrate, 'bppf': bppf, 'codec': codec,
                  'width': int(stream.get('width', 0)), 'height': int(stream.get('height', 0)), 'fps': fps}
    except Exception:
        result = {'kind': 'unknown', 'bitrate': 0, 'bppf': 0, 'codec': '', 'width': 0, 'height': 0, 'fps': 0}
    origin_cache[state_key] = result
    return result

def date_dir_parts(name):
    match = DATE_DIR_RE.match(name)
    if not match:
        return None
    month, day = int(match.group(1)), int(match.group(2))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return month, day, match.group(3)


def library_roots(extra=None):
    roots, seen = [], set()
    for candidate in (extra, *LIBRARY_ROOTS):
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if not path.is_dir():
            continue
        key = os.path.normcase(str(path.resolve()))
        if key in seen:
            continue
        seen.add(key)
        roots.append(path)
    return roots


def find_storage_dir(month, day, roots):
    """Prefer an existing date folder that already carries the day's description."""
    token = f'{month:02d}.{day:02d}'
    described, exact = [], []
    for root in roots:
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir() or child.name.startswith('.') or child.name in SKIP_DIRS:
                continue
            parsed = date_dir_parts(child.name)
            if not parsed or (parsed[0], parsed[1]) != (month, day):
                continue
            (exact if folder_tags.strip_tag(child.name) == token else described).append(child)
    pool = described or exact
    if not pool:
        return None

    def rank(path):
        parent = os.path.normcase(str(path.parent))
        volume = 0 if parent == os.path.normcase('/Volumes/Data') else 1 if parent == os.path.normcase('/Volumes/Weihao') else 2
        return volume, path.name

    return sorted(pool, key=rank)[0]


def media_date(path, root=None, year=None):
    path = Path(path)
    for pattern in (
        r'(?<!\d)(20\d{2})(\d{2})(\d{2})(?:\d{6})?(?!\d)',
        r'(?<!\d)(20\d{2})[-._](\d{2})[-._](\d{2})(?!\d)',
    ):
        match = re.search(pattern, path.stem)
        if match:
            try: return dt.date(*map(int, match.groups()[:3]))
            except ValueError: pass
    try: parts = list(path.parent.relative_to(root).parts) if root else list(path.parent.parts)
    except ValueError: parts = list(path.parent.parts)
    for part in reversed(parts):
        match = re.match(r'^(20\d{2})[-.](\d{1,2})[-.](\d{1,2})(?:\D|$)', part)
        if match:
            try: return dt.date(*map(int, match.groups()))
            except ValueError: pass
    for index, part in enumerate(parts):
        month = re.match(r'^(20\d{2})[.](\d{1,2})(?:Done)?$', part, re.I)
        if not month: continue
        for child in parts[index+1:]:
            day = re.match(r'^(\d{1,2})[.-](\d{1,2})(?:\D|$)', child)
            if day and int(day.group(1)) == int(month.group(2)):
                try: return dt.date(int(month.group(1)), int(month.group(2)), int(day.group(2)))
                except ValueError: pass
    for part in reversed(parts):
        parsed = date_dir_parts(part)
        if not parsed:
            continue
        folder_year = int(year) if year else None
        if folder_year is None:
            for earlier in parts:
                found = re.match(r'^(20\d{2})', earlier)
                if found:
                    folder_year = int(found.group(1))
                    break
        if folder_year is None:
            continue
        try:
            return dt.date(folder_year, parsed[0], parsed[1])
        except ValueError:
            pass
    return None

def scan_library(root, year=None):
    root = Path(root).expanduser().resolve(strict=True)
    if not root.is_dir(): raise ValueError('扫描位置不是文件夹')
    videos, manifests = [], []
    for current, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS and not name.startswith('.')]
        for name in files:
            if name.startswith('.'): continue  # macOS ._ metadata twins on exFAT/FAT drives, hidden temp files
            path = Path(current) / name
            lower = name.lower()
            if lower.endswith('.dji.json'): manifests.append(path)
            elif path.suffix.lower() in VIDEO_EXTENSIONS and '.partial.' not in lower: videos.append(path)
    compressed_sources, known_outputs, records = set(), set(), []
    for manifest in manifests:
        try:
            record = json.loads(manifest.read_text(encoding='utf8'))
            if record.get('verified') and Path(record.get('output', '')).is_file():
                source_key = os.path.normcase(str(Path(record['source']).resolve()))
                compressed_sources.add(source_key)
                known_outputs.add(os.path.normcase(str(Path(record['output']).resolve())))
                records.append(record)
        except (OSError, ValueError, TypeError, KeyError): pass
    groups, skipped, unsupported, unknown, seen_sources = {}, 0, 0, 0, set()
    def group_for(date):
        key = date.isoformat() if date else '日期未知'
        return groups.setdefault(key, {'date': key, 'files': [], 'count': 0, 'pending': 0, 'completed': 0,
                                       'size': 0, 'unsupported': 0, 'explicit_compressed': 0,
                                       'likely_compressed': 0, 'camera_source': 0, 'unknown_source': 0})
    for path in videos:
        normalized = os.path.normcase(str(path.resolve()))
        if normalized in known_outputs:
            skipped += 1; continue
        date = media_date(path, root, year)
        if year and (not date or date.year != int(year)): continue
        if not date: unknown += 1
        group = group_for(date)
        group['count'] += 1; group['size'] += path.stat().st_size
        if normalized in compressed_sources:
            group['completed'] += 1; seen_sources.add(normalized); continue
        if explicit_compressed(path):
            group['explicit_compressed'] += 1; continue
        if path.suffix.lower() == '.osv':
            group['files'].append(str(path)); group['pending'] += 1
            group['camera_source'] += 1
            if date:
                try: c.stamp(path)
                except ValueError: scan_dates[str(path)] = date.strftime('%Y%m%d_000000')
            continue
        profile = origin_profile(path)
        if profile['kind'] == 'likely_compressed':
            group['likely_compressed'] += 1; continue
        group['files'].append(str(path)); group['pending'] += 1
        if profile['kind'] == 'camera_source': group['camera_source'] += 1
        else: group['unknown_source'] += 1
        if date:
            try: c.stamp(path)
            except ValueError: scan_dates[str(path)] = date.strftime('%Y%m%d_000000')
    for record in records:
        source = Path(record['source'])
        source_key = os.path.normcase(str(source.resolve()))
        if source_key in seen_sources: continue
        output = Path(record['output'])
        date = media_date(source, root, year) or media_date(output, root, year)
        if year and (not date or date.year != int(year)): continue
        group = group_for(date)
        group['count'] += 1; group['completed'] += 1
        group['size'] += int(record.get('source_state', {}).get('size', 0) or 0)
        seen_sources.add(source_key)
    ordered = sorted(groups.values(), key=lambda item: item['date'])
    dated = [media_date(path, root, year) for path in videos]
    years = sorted({date.year for date in dated if date}, reverse=True)
    return {'root': str(root), 'days': ordered, 'years': years,
            'candidate_count': sum(g['pending'] for g in ordered),
            'completed_count': sum(g['completed'] for g in ordered),
            'explicit_compressed_count': sum(g['explicit_compressed'] for g in ordered),
            'likely_compressed_count': sum(g['likely_compressed'] for g in ordered),
            'camera_source_count': sum(g['camera_source'] for g in ordered),
            'unknown_source_count': sum(g['unknown_source'] for g in ordered),
            'skipped_count': skipped, 'unsupported_count': unsupported, 'unknown_count': unknown}

WINDOWS_PICKER = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Application]::EnableVisualStyles()
$owner = New-Object System.Windows.Forms.Form -Property @{TopMost = $true; ShowInTaskbar = $false; Opacity = 0; Width = 1; Height = 1; StartPosition = 'CenterScreen'}
$owner.Show()
$owner.Activate()
try {
if ($args[0] -eq 'folder') {
  $d = New-Object System.Windows.Forms.FolderBrowserDialog
  $d.ShowNewFolderButton = $true
  if ($d.ShowDialog($owner) -eq 'OK') { ConvertTo-Json -Compress $d.SelectedPath } else { '""' }
} else {
  $d = New-Object System.Windows.Forms.OpenFileDialog
  $d.Multiselect = $true
  $d.Filter = if ($args[0] -eq 'records') { 'Records|*.dji.json' } else { 'Videos|*.mp4;*.mov;*.mkv;*.osv' }
  if ($d.ShowDialog($owner) -eq 'OK') { ConvertTo-Json -Compress @($d.FileNames) } else { '[]' }
}
} finally { $d.Dispose(); $owner.Close(); $owner.Dispose() }
"""

PICKER_LOCK = threading.Lock()

def picker(kind):
    if kind not in ('folder', 'files', 'records'):
        raise ValueError('未知选择类型')
    if not PICKER_LOCK.acquire(blocking=False):
        raise ValueError('选择窗口已经打开，请先完成或取消上一次选择')
    try:
        return _picker(kind)
    except Exception as exc:
        applog.write('文件选择', str(exc))
        raise
    finally:
        PICKER_LOCK.release()

def _picker(kind):
    if sys.platform == 'win32':
        # Portable Python ships without Tk, so use the native Windows dialogs.
        if kind not in ('folder', 'files', 'records'): raise ValueError('未知选择类型')
        script = WINDOWS_PICKER.replace('$args[0]', f"'{kind}'")
        encoded = __import__('base64').b64encode(script.encode('utf-16-le')).decode()
        powershell = str(Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
        try:
            result = subprocess.run([powershell, '-NoProfile', '-NonInteractive', '-STA', '-EncodedCommand', encoded],
                                    stdin=subprocess.DEVNULL, capture_output=True, timeout=300,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise ValueError('选择窗口等待超过 5 分钟，请重新点击浏览') from None
        if result.returncode:
            detail = result.stderr.decode('utf-8-sig', errors='replace').strip()
            raise ValueError('无法打开选择窗口：' + detail[-1500:])
        value = json.loads(result.stdout.decode('utf-8-sig').strip() or ('""' if kind == 'folder' else '[]'))
        return value if kind == 'folder' or isinstance(value, list) else [value]
    code = '''import tkinter as t,json,sys
from tkinter import filedialog as d
r=t.Tk();r.withdraw();r.attributes('-topmost',True)
k=sys.argv[1]
v=d.askdirectory() if k=='folder' else d.askopenfilenames(filetypes=[('Files','*.dji.json' if k=='records' else '*.mp4 *.MP4 *.mov *.MOV *.mkv *.MKV *.osv *.OSV')])
print(json.dumps(v));r.destroy()
'''
    return json.loads(subprocess.check_output([sys.executable, '-c', code, kind], **({'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {})))

def launch(fn):
    with lock:
        if state['busy']: raise ValueError('已有任务正在运行')
        state['busy'] = True; state['logs'] = []; worker.cancel.clear()
    def job():
        try: fn()
        except Exception as e: worker.events.put(str(e))
        finally:
            worker.events.put('任务结束，请检查结果。')
            with lock: state['busy'] = False
    threading.Thread(target=job, daemon=True).start()

def sort_key(path):
    path = str(path)
    try: timestamp = c.stamp(path)
    except ValueError: timestamp = scan_dates.get(path, '99999999_999999')
    return timestamp, os.path.normcase(path)

def archive_directory(path, output_root=None):
    timestamp = c.stamp(path) if str(path) not in scan_dates else scan_dates[str(path)]
    month, day = int(timestamp[4:6]), int(timestamp[6:8])
    existing = find_storage_dir(month, day, library_roots(output_root))
    if existing:
        return existing
    if not output_root and not LIBRARY_ROOTS: raise ValueError('请先连接 SSD 或选择输出文件夹')
    base = Path(output_root).expanduser() if output_root else LIBRARY_ROOTS[0]
    return base / f'{month:02d}.{day:02d}'

def names(data):
    start, width = int(data['start']), int(data['width'])
    if start < 0 or not 1 <= width <= 10: raise ValueError('起始序号须非负，序号位数须为 1–10')
    paths = sorted(set(data['files']), key=sort_key)
    items = []
    for index, path in enumerate(paths):
        directory = archive_directory(path, data.get('output') or None) if data.get('archive', True) and not data.get('same') else None
        items.append({
            'source': path,
            'name': c.output_name(path, data['suffix'], start + index, width, timestamp=scan_dates.get(path)),
            'folder': directory.name if directory else '',
            'directory': str(directory) if directory else '',
            'size': Path(path).stat().st_size,
        })
    return items

def public_tasks():
    with task_condition:
        return [{key: item.get(key) for key in ('id', 'source', 'output', 'video', 'audio', 'osv', 'status', 'message', 'engine', 'return_path', 'temporary_storage', 'elapsed_seconds', 'attempt_seconds', 'source_size', 'verification_mode', 'kind', 'duplicate_of', 'after', 'estimated_size', 'raw_estimate', 'output_size')} for item in tasks]

def clear_finished_history():
    """Start a clean UI round without touching media or on-disk manifests."""
    with task_condition:
        if state['busy'] or any(item['status'] in ('pending', 'running') for item in tasks):
            raise ValueError('仍有等待或处理中的任务，请等待完成或先停止全部任务')
        if any(item['status'] == 'interrupted' for item in tasks):
            raise ValueError('还有上次没做完的任务，请先选择「继续处理」或「放弃」')
        removed = len(tasks)
        tasks.clear()
        worker.manifests.clear()
        state['logs'] = ['历史已清空，可以开始下一轮。']
        with worker.progress_lock:
            worker.progress.clear()
        task_condition.notify_all()
    return removed

def queue_progress():
    with task_condition:
        visible = [item for item in tasks if item['status'] != 'removed']
        done = sum(item['status'] in ('completed', 'failed', 'cancelled') for item in visible)
        active = next((item for item in visible if item['status'] == 'running'), None)
        owners = dict(active_owners)
        owner = owners.get(active['id']) if active else None
        paused = queue_paused
    detail = {}
    if owner:
        with owner.progress_lock: detail = dict(owner.progress)
    detail['active_tasks'] = []
    for task in visible:
        if task['id'] not in owners: continue
        with owners[task['id']].progress_lock: values = dict(owners[task['id']].progress)
        detail['active_tasks'].append({'id':task['id'],'file':Path(task['source']).name,**values})
    detail['timing'] = TIME_ESTIMATOR.measure(visible, detail, paused)
    detail.update(total=len(visible), processed=done, remaining=max(0, len(visible)-done), paused=paused,
                  task_id=active['id'] if active else '', queue_status='paused' if paused else ('running' if active else 'idle'))
    return detail

def dispatch_loop():
    global dispatcher_thread,queue_stopping
    with task_condition:
        while True:
            if queue_stopping:
                if not exiting:
                    for item in tasks:
                        if item['status']=='pending':item.update(status='cancelled',message='已停止')
                if not active_owners:
                    state['busy']=False;dispatcher_thread=None;queue_stopping=False
                    task_condition.notify_all();return
            elif not queue_paused:
                sources={os.path.normcase(tasks_by_id['source']) for tasks_by_id in tasks if tasks_by_id['id'] in active_owners}
                while len(active_owners)<pipeline.settings['jobs']:
                    waiting={i['id'] for i in tasks if i['status'] in ('pending','running')}
                    item=next((i for i in tasks if i['status']=='pending' and os.path.normcase(i['source']) not in sources
                               and i.get('after') not in waiting),None)
                    if item is None:break
                    item.update(status='running',message='处理中')
                    item['verification_mode'] = item.get('verification_mode') or pipeline.settings['verification']
                    owner=types.SimpleNamespace(events=worker.events,cancel=threading.Event(),manifests=worker.manifests,
                        progress={},progress_lock=threading.Lock(),verification_mode=item.get('verification_mode') or pipeline.settings['verification'])
                    active_owners[item['id']]=owner;sources.add(os.path.normcase(item['source']))
                    threading.Thread(target=process_task,args=(item,owner),daemon=True).start()
            if not active_owners and not any(i['status']=='pending' for i in tasks):
                state['busy']=False;dispatcher_thread=None;task_condition.notify_all();return
            task_condition.wait(.2)

def process_task(item,owner):
    try:
        _process_task(item,owner)
    except Exception as exc:
        with task_condition:
            item.update(status='failed',message=str(exc))
        owner.events.put(f"{Path(item['source']).name}：{exc}")
    finally:
        storage.release_reservation(item['id'])
        with task_condition:
            active_owners.pop(item['id'],None)
            task_condition.notify_all()

def free_target(target):
    """target-2.mp4, target-3.mp4 … the first name not on disk and not taken by the queue."""
    target = Path(target)
    with task_condition:
        taken = {os.path.normcase(i['output']) for i in tasks}
    for number in range(2, 1000):
        candidate = target.with_name(f'{target.stem}-{number}{target.suffix}')
        if not candidate.exists() and os.path.normcase(str(candidate)) not in taken:
            return candidate
    raise ValueError('找不到可用的文件名')

def remove_duplicate(path, mode):
    if mode == 'permanent':
        Path(path).unlink()
        return '已彻底删除这份重复的原片'
    from send2trash import send2trash
    send2trash(str(Path(path).resolve()))
    return '这份重复的原片已移入回收站'

def resolve_clash(item, owner):
    """Same target name as another clip. Returns True when settled here, False to compress under a free name.
    Nothing is deleted unless SHA-256 shows the content is already safe elsewhere."""
    source, target = Path(item['source']), Path(item['output'])
    mode = item['config'][3] if len(item['config']) > 3 else False
    name = source.name

    def finish(message):
        with task_condition:
            item.update(status='completed', message=message, kind=None)
        owner.events.put(f'{name}：{message}')
        applog.event('name_clash', source=item['source'], output=item['output'], result=message)
        return True

    def rename():
        new = free_target(target)
        owner.events.put(f'{name}：内容和同名的不一样，改用 {new.name} 正常压缩')
        with task_condition:
            item.update(output=str(new), kind=None, message='同名但内容不同，改名后压缩')
        return False

    manifest = target.with_suffix('.mp4.dji.json')
    try:
        record = json.loads(manifest.read_text(encoding='utf8'))
    except (OSError, ValueError):
        record = {}
    if item.get('kind') == 'duplicate':
        other = Path(item.get('duplicate_of') or '')
        own = c.digest_progress(owner, source, '核对同名原片：计算这份的哈希')
        if other.is_file() and os.path.normcase(str(other.resolve())) != os.path.normcase(str(source.resolve())):
            same = own == c.digest_progress(owner, other, '核对同名原片：计算另一份的哈希')
            kept = f'{other.name} 还在'
        else:  # the other copy was compressed and removed: compare with what its record says
            same = bool(record.get('verified')) and own == record.get('source_sha256') and target.is_file()
            kept = f'它的压缩版 {target.name} 已校验'
        if not same:
            return rename()
        update_progress(owner, phase='删除重复的原片')
        return finish(f'和另一份内容完全一样（SHA-256 一致，{kept}），{remove_duplicate(source, mode)}')
    # Target exists already: is it this very original, compressed earlier?
    if not (target.is_file() and record.get('verified') and record.get('source_sha256')):
        return rename()
    if c.digest_progress(owner, source, '核对是否已压缩过：计算原片哈希') != record['source_sha256']:
        return rename()
    if not mode:
        return finish(f'已经压缩过（原片哈希与 {target.name} 的记录一致），按设置保留原片')
    check = dict(record, source=str(source), output=str(target), deleted=False)
    result = c.recycle(check, manifest, owner.cancel, mode, owner=owner)  # full integrity check of the existing compressed file first
    return finish(f'已经压缩过：{target.name} 完整性核对通过，{result}')

def update_progress(owner, **values):
    c.update_progress(owner, **values)

def _process_task(item,owner):
    if item.get('kind') in ('already_done', 'duplicate'):
        try:
            if resolve_clash(item, owner):
                return
        except Exception as error:
            with task_condition:
                if owner.cancel.is_set():
                    item.update(status='interrupted' if exiting else 'cancelled',
                                message='退出时中断，下次打开可继续' if exiting else '已取消，可重新加入')
                else:
                    item.update(status='failed', message=f'同名核对失败，未删除任何文件：{error}')
            owner.events.put(f"{Path(item['source']).name}：{error}")
            return
    failure, started = None, time.time()
    owner.last_error = None
    with task_condition:
        item['started_at'] = started
        try: item['source_size'] = Path(item['source']).stat().st_size
        except OSError: item['source_size'] = 0
    applog.write('压缩', f"开始：{item['source']} → {item['output']}")
    try:
        storage.flush(storage_returned)
        output, config = storage.prepare(item, owner)
        with task_condition: item['output'] = str(output)
        if Path(item['source']).suffix.lower() == '.osv':
            osv.process(owner, Path(item['source']), output, config, item['osv'])
        else:
            c.App.compress(owner, [(Path(item['source']), output)], config)
    except Exception as error:
        failure = str(error)
        owner.events.put(f"{Path(item['source']).name}：{failure}")
    manifest = Path(item['output']).with_suffix('.mp4.dji.json')
    verified, record = False, {}
    try:
        record = json.loads(manifest.read_text(encoding='utf8'))
        verified = record.get('verified') and record.get('source') == item['source']
    except (OSError, ValueError): pass
    with owner.progress_lock:
        item['engine'] = record.get('engine') or owner.progress.get('engine')
    with task_condition:
        if owner.cancel.is_set() and exiting:
            item['status'] = 'interrupted'; item['message'] = '退出时中断，下次打开可继续'
        elif owner.cancel.is_set():
            item['status'] = 'cancelled'; item['message'] = '已取消，可重新加入'
        elif not failure and Path(item['output']).is_file() and verified:
            item['status'] = 'completed'; item['message'] = '已完成并验证'
            if item.get('return_path'):
                storage.mark_ready(item['id'])
                item['message'] = '已完整验证，暂存在桌面，等待空间后自动移回'
                if str(manifest) in worker.manifests:
                    worker.manifests.remove(str(manifest))
        else:
            reason = failure or getattr(owner, 'last_error', None)
            item['status'] = 'failed'; item['message'] = (c.explain(reason) if reason else None) or '失败，请查看日志后重试'
        if item['status'] == 'completed':
            item['output_size'] = (record.get('output_state') or {}).get('size')
        item['attempt_seconds'] = max(0, time.time() - started)
        item['elapsed_seconds'] = (item.get('elapsed_seconds') or 0) + item['attempt_seconds']
        item['started_at'] = None
        task_condition.notify_all()
    applog.event('compress', source=item['source'], output=item['output'], status=item['status'], message=item['message'],
                 engine=item.get('engine'), video=item['video'], audio=item['audio'], seconds=round(time.time() - started, 1),
                 source_size=(record.get('source_state') or {}).get('size'), output_size=(record.get('output_state') or {}).get('size'),
                 estimated_size=item.get('estimated_size'), raw_estimate=item.get('raw_estimate'))

def space_summary(data):
    """Drive space before / now / after the queue, plus what adding the current selection would do."""
    with task_condition:
        copies = [dict(item) for item in tasks if item['status'] != 'removed']
    result = space.summary(copies, data.get('selection') or [], data.get('delete_mode', 'keep'))
    with task_condition:
        learned = {item['id']: item for item in copies if item.get('estimated_size')}
        for item in tasks:
            if item['id'] in learned and not item.get('estimated_size'):
                item['estimated_size'] = learned[item['id']]['estimated_size']
                item['raw_estimate'] = learned[item['id']].get('raw_estimate')
    return result

def preferences():
    # defaults: delete originals once verified; tag date folders _ORG / _CMP by what they hold
    return {'delete_mode': 'permanent', 'folder_tags': True, **(applog.load_json('preferences.json', {}) or {})}

TAG_LOCK = threading.Lock()

def working():
    with task_condition:
        busy = state['busy'] or bool(active_owners)
    return busy or sd.job['running'] or library.job['running']

def locked_paths():
    """Folders 轻存 still has to find under their current name: unfinished tasks and files waiting to move back."""
    with task_condition:
        paths = [p for t in tasks if t['status'] in ('pending', 'running', 'interrupted') for p in (t['source'], t['output'])]
    paths += [p for e in storage.snapshot() for p in (e.get('target'), e.get('source')) if p]
    return paths

def retag():
    """Give each date folder the tag that fits its content now (only while nothing is being processed)."""
    if working():
        return {'done': [], 'failed': [], 'skipped': [], 'busy': True}
    with TAG_LOCK:
        changes, skipped = folder_tags.plan(library_roots(), locked_paths())
        done, failed = folder_tags.apply(changes)
    for item in done: applog.write('文件夹后缀', f"{item['from']} → {item['to']}")
    for item in failed: applog.write('文件夹后缀', f"{item['from']} 没能改名：{item['reason']}")
    return {'done': done, 'failed': failed, 'skipped': skipped, 'busy': False}

def retime_request(data):
    """时间校准: plan / apply / undo (see retime.py)."""
    action = data.get('action', 'plan')
    if action == 'last':
        roots = [str(r) for r in library_roots()] + (applog.load_json('retime-roots.json', []) or [])
        last = retime.last_batch(list(dict.fromkeys(r for r in roots if os.path.isdir(r))))
        if not last: return {'last': None}
        root, batch, entries = last
        files = [e for e in entries if 'from' in e]
        folder = next((e['folder_to'] for e in entries if 'folder_to' in e), entries[0].get('folder'))
        return {'last': {'root': str(root), 'batch': batch, 'at': entries[0]['at'], 'folder': folder, 'count': len(files)}}
    if action == 'undo':
        if working(): raise ValueError('正在处理视频或导入，请等处理完再撤销')
        with TAG_LOCK:
            restored = retime.undo(data['root'], data['batch'])
        applog.write('时间校准', f"撤销：{len(restored)} 个视频改回原来的时间")
        return {'restored': len(restored)}
    folder = str(data.get('folder') or '').strip()
    if not folder: raise ValueError('请先选择文件夹')
    options = data.get('options') or {}
    if action == 'apply':
        if working(): raise ValueError('正在处理视频或导入，请等处理完再校准')
        prefix = os.path.normcase(os.path.abspath(folder)) + os.sep
        if any(os.path.normcase(os.path.abspath(p)).startswith(prefix) for p in locked_paths()):
            raise ValueError('队列里还有这个文件夹的视频没处理完，请先处理完或移出队列')
        with TAG_LOCK:
            result = retime.apply(folder, options)
        roots = applog.load_json('retime-roots.json', []) or []
        parent = str(Path(folder).parent)
        if parent not in roots: applog.save_json('retime-roots.json', (roots + [parent])[-20:])
        applog.write('时间校准', f"{Path(folder).name}：{len(result['done'])} 个视频平移 {result['shift_seconds']} 秒"
                     + (f"，文件夹改为 {Path(result['folder']).name}" if result['folder'] != folder else ''))
        return result
    return retime.plan(folder, options)

def tag_loop():
    """Keep folder tags right after compressing / importing finishes."""
    while True:
        time.sleep(60)
        try:
            if preferences().get('folder_tags'):
                retag()
        except Exception as error:
            applog.write('文件夹后缀', f'出错：{error}')

def storage_returned(entry):
    with task_condition:
        item = next((i for i in tasks if i['id'] == entry['id']), None)
        if item:
            item['output'] = entry['target']
            item['return_path'] = None
            item['message'] = '已从桌面移回并校验'
            final_manifest = str(Path(entry['target']).with_suffix('.mp4.dji.json'))
            if final_manifest not in worker.manifests:
                worker.manifests.append(final_manifest)
            task_condition.notify_all()

def snapshot_queue():
    with task_condition:
        return [{key: (list(item[key]) if key == 'config' else item.get(key))
                 for key in ('id', 'source', 'output', 'config', 'osv', 'video', 'audio', 'status', 'message', 'engine', 'return_path', 'temporary_storage', 'elapsed_seconds', 'attempt_seconds', 'source_size', 'verification_mode', 'kind', 'duplicate_of', 'after', 'estimated_size', 'raw_estimate', 'output_size')} for item in tasks]

def queue_saver():
    last = None
    while True:
        text = json.dumps(snapshot_queue(), ensure_ascii=False)
        if text != last:
            try:
                applog.save_json(QUEUE_FILE, json.loads(text)); last = text
            except OSError:
                pass
        time.sleep(1.5)

def load_queue():
    """Bring back the queue from the last run; anything unfinished waits for the user to continue."""
    saved = applog.load_json(QUEUE_FILE, []) or []
    with task_condition:
        for item in saved:
            if item.get('status') in ('pending', 'running', 'interrupted'):
                item['status'] = 'interrupted'; item['message'] = '上次没做完，等待继续'
            item['config'] = tuple(item.get('config') or ())
            tasks.append(item)
    for item in saved:  # older runs only said "see the log": find out whether the original itself is broken
        if item['status'] == 'failed' and item.get('message') == '失败，请查看日志后重试' and Path(item['source']).is_file():
            try:
                c.video(c.probe(item['source']))
            except Exception as error:
                item['message'] = c.explain(str(error)) or item['message']
    processing_time.restore(tasks)
    return sum(item['status'] == 'interrupted' for item in tasks)

def continue_queue():
    global dispatcher_thread, queue_stopping
    resumed = 0
    with task_condition:
        for item in tasks:
            if item['status'] != 'interrupted':
                continue
            source, output = Path(item['source']), Path(item['output'])
            try:
                record = json.loads(output.with_suffix('.mp4.dji.json').read_text(encoding='utf8'))
            except (OSError, ValueError):
                record = {}
            if item.get('kind') in ('already_done', 'duplicate') and source.is_file():
                item['status'] = 'pending'; item['message'] = '继续核对同名文件'; resumed += 1; continue
            if output.is_file() and record.get('verified'):
                item['status'] = 'completed'; item['message'] = '上次已完成并验证'; continue
            if output.exists():
                item['status'] = 'failed'; item['message'] = '目标文件已存在但没有校验记录，请检查后手动处理'; continue
            if not source.is_file():
                item['status'] = 'failed'; item['message'] = '原片不在了（硬盘没插或已被移动）'; continue
            try:  # an encode cut off mid-way leaves a hidden temp file next to the output
                for leftover in output.parent.iterdir():
                    if TEMP_PARTIAL_RE.match(leftover.name):
                        leftover.unlink()
            except OSError:
                pass
            item['status'] = 'pending'; item['message'] = '继续处理（这一个从头压缩）'; resumed += 1
        if resumed:
            queue_stopping = False; state['busy'] = True
            if dispatcher_thread is None or not dispatcher_thread.is_alive():
                dispatcher_thread = threading.Thread(target=dispatch_loop, daemon=True); dispatcher_thread.start()
        task_condition.notify_all()
    applog.write('压缩', f'继续上次没做完的任务：{resumed} 个')
    return resumed

def discard_interrupted():
    with task_condition:
        before = len(tasks)
        tasks[:] = [item for item in tasks if item['status'] != 'interrupted']
        task_condition.notify_all()
        return before - len(tasks)

def drain_events():
    """Move compressor messages to the page log and to the log file, even with no browser open."""
    while True:
        message = worker.events.get()
        with lock:
            state['logs'].append(message); del state['logs'][:-500]
        applog.write('压缩', message)

def probe_hardware():
    hardware.update(status='checking', items=[])
    items = []
    if sys.platform == 'win32':
        smi = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'nvidia-smi.exe'
        try:
            out = subprocess.run([str(smi) if smi.exists() else 'nvidia-smi', '--query-gpu=name,driver_version', '--format=csv,noheader'],
                                 capture_output=True, text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
            hardware['gpu'] = '；'.join(f"{line.split(',')[0].strip()}（驱动 {line.split(',')[-1].strip()}）" for line in out.splitlines() if line.strip())
        except (OSError, subprocess.SubprocessError):
            hardware['gpu'] = ''
    elif sys.platform == 'darwin':
        try:
            hardware['gpu'] = subprocess.run(['sysctl', '-n', 'machdep.cpu.brand_string'], capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    ffmpeg = c.executable('ffmpeg')
    for engine in dict.fromkeys(c.candidates('自动选择') + ['libx265']):
        name, kind = c.ENGINE_INFO[engine]
        entry = {'engine': engine, 'name': name, 'kind': kind, 'ok': False, 'note': '' if ffmpeg else '没有找到 FFmpeg'}
        for tuned in ((True, False) if engine == 'hevc_nvenc' else (True,)):
            if not ffmpeg:
                break
            try:
                c.run([ffmpeg, '-v', 'error', '-nostdin', '-f', 'lavfi', '-i', 'testsrc2=size=1280x720:rate=30', '-t', '0.5']
                      + c.encoding_args(engine, 4_000_000, True, '中等压缩', tuned) + ['-f', 'null', '-'])
                entry.update(ok=True, note='' if tuned or engine != 'hevc_nvenc' else '只能用基础参数（建议更新显卡驱动）')
                break
            except Exception as error:
                entry['note'] = c.short_error(error)
        items.append(entry)
        applog.write('设备检测', f"{name}：{'可用' if entry['ok'] else '不可用'} {entry['note']}")
    auto = next((item for item in items if item['ok']), None)
    hardware.update(status='done', items=items, auto=f"{auto['name']}（{auto['kind']}）" if auto else '没有可用的编码器')

def enqueue(data):
    global dispatcher_thread, queue_stopping
    if not c.executable('ffmpeg') or not c.executable('ffprobe'): raise ValueError('未找到 FFmpeg，请按使用说明安装')
    items = names(data)
    if not items: raise ValueError('请先添加视频')
    same = data.get('same', False)
    deletion = data.get('delete_mode', 'keep')
    if deletion not in ('keep', 'trash', 'permanent'): raise ValueError('无效删除方式')
    if (same or deletion != 'keep') and any(i['source'] in dropped for i in items): raise ValueError('拖入文件请先关联原片文件夹，再使用原目录或删除功能')
    if not same and not data.get('output', '').strip() and not all(item.get('directory') for item in items):
        raise ValueError('请选择输出文件夹')
    dest = Path(data['output']).expanduser().resolve() if data.get('output', '').strip() else None
    jobs = []
    file_options = data.get('file_options') or {}
    export_options = osv.settings(data.get('osv') or {})
    for item in items:
        source = Path(item['source']).resolve()
        if item.get('directory'):
            target = Path(item['directory']) / item['name']
        else:
            base = source.parent if same else dest
            target = base / item['folder'] / item['name'] if item['folder'] else base / item['name']
        options = file_options.get(str(source), file_options.get(item['source'], {}))
        config = (options.get('video', data['video']), options.get('audio', data['audio']), data['engine'],
                  False if deletion == 'keep' else deletion, bool(data['pano']))
        if config[0] not in c.LEVELS or config[1] not in c.LEVELS or config[2] not in c.ENGINES: raise ValueError('无效的档位')
        jobs.append((source, target, config))
    if deletion == 'trash': __import__('send2trash')
    if dest is not None and not same and any(not item.get('directory') for item in items):
        dest.mkdir(parents=True, exist_ok=True)
    with task_condition:
        reserved = {os.path.normcase(item['output']) for item in tasks if item['status'] in ('pending', 'running')}
        reserved.update(os.path.normcase(item['return_path']) for item in tasks if item.get('return_path'))
        reserved.update(os.path.normcase(entry['target']) for entry in storage.snapshot())
        if any(source == target for source, target, _ in jobs):
            raise ValueError('输出文件不能和原片是同一个文件')
        # Same target name: decided later in the queue by SHA-256 instead of refusing the whole batch.
        owners = {os.path.normcase(i['output']): i for i in tasks if i['status'] in ('pending', 'running', 'interrupted')}
        added = []
        for source, target, config in jobs:
            target.parent.mkdir(parents=True, exist_ok=True)
            key = os.path.normcase(str(target))
            clash, earlier = None, owners.get(key)
            if earlier and os.path.normcase(earlier['source']) == os.path.normcase(str(source)):
                continue  # this very file is already queued
            if earlier or key in reserved:
                clash = {'kind': 'duplicate', 'duplicate_of': earlier['source'] if earlier else '', 'after': earlier['id'] if earlier else ''}
            elif target.exists():
                clash = {'kind': 'already_done'}
            item = {'id': uuid.uuid4().hex, 'source': str(source), 'output': str(target), 'config': config,
                    'osv': dict(export_options), 'verification_mode': data.get('verification_mode') or pipeline.settings['verification'], 'temporary_storage': bool(data.get('temporary_storage', True)), 'source_size': source.stat().st_size,
                    'video': config[0], 'audio': config[1], 'status': 'pending', 'message': '等待处理'}
            if clash:
                item.update(clash, message='同名：等待核对哈希' if clash['kind'] == 'duplicate' else '目标已存在：等待核对是否已压缩过')
            else:
                owners[key] = item
            tasks.append(item); added.append(item['id'])
        tasks.sort(key=lambda item: sort_key(item['source']))
        queue_stopping = False; state['busy'] = True
        if dispatcher_thread is None or not dispatcher_thread.is_alive():
            dispatcher_thread = threading.Thread(target=dispatch_loop, daemon=True); dispatcher_thread.start()
        task_condition.notify_all()
    return added

SERVER = {'httpd': None, 'quit_button': True}
DRAINER = {'on': False}
RESTART = {'on': False}

def shutdown():
    """Stop the running step safely (originals untouched), keep the rest of the queue for next time, then exit."""
    global queue_stopping, exiting
    exiting = True
    storage.STOP.set()
    applog.write('轻存', '正在退出')
    with task_condition:
        queue_stopping = True
        owners = list(active_owners.values())
        task_condition.notify_all()
    for owner in owners: c.cancel_work(owner.cancel)
    sd.cancel(); library.job['cancel'].set(); worker.cancel.set()
    for _ in range(100):  # up to 20 s for the running step to clean up
        with task_condition:
            idle = not active_owners
        if idle and not sd.job['running'] and not library.job['running']:
            break
        __import__('time').sleep(0.2)
    applog.save_json(QUEUE_FILE, snapshot_queue())
    print('轻存已退出。')
    applog.write('轻存', '已退出')
    SERVER['httpd'].shutdown()

def library_route(path, data):
    root = data.get('root') or library.job['root']
    if path == '/library/scan':
        if not root: raise ValueError('请先选择硬盘或文件夹')
        library.start_scan(root); return {}
    if path == '/library/state':
        return library.state(bool(data.get('result')))
    if path == '/library/cancel':
        library.job['cancel'].set(); return {}
    if not root: raise ValueError('请先扫描')
    if path == '/library/mark':
        return library.mark_originals(root, data.get('rels', []))
    if path == '/library/undo':
        return library.undo_last_mark(root)
    if path == '/library/verify':
        return library.verify_duplicate(root, data.get('rels', []))
    if path == '/library/export':
        result = library.job['result']
        if not result: raise ValueError('请先扫描')
        return {'path': library.export_page(result, (c.BASE/'素材日历.html').read_text(encoding='utf8'))}
    if path == '/library/open':
        target = (Path(root) / data.get('rel', '')).resolve()
        if Path(root).resolve() not in target.parents or not target.is_file(): raise ValueError('文件不存在')
        if sys.platform == 'win32': os.startfile(str(target))  # noqa: S606 - local file the user clicked
        else: subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', str(target)])
        return {}
    raise ValueError('未知操作')

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def reply(self, obj, status=200):
        data=json.dumps(obj,ensure_ascii=False).encode()
        self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}': self.send_error(403); return
        if self.path == '/health':
            self.reply({'build': BUILD, 'busy': state['busy'] or sd.job['running'] or library.job['running'],
                        'openosv_installed': osv.tool().is_file(), 'can_quit': SERVER['quit_button']}); return
        if self.path == '/library':
            html=(c.BASE/'素材日历.html').read_text(encoding='utf8').replace('__TOKEN__',TOKEN).replace('/*__MODE__*/',"const MODE='live';").encode()
            self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(html); return
        if self.path != '/': self.send_error(404); return
        html=(c.BASE/'界面.html').read_text(encoding='utf8').replace('__TOKEN__',TOKEN).encode()
        self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(html)
    def do_POST(self):
        if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}': self.send_error(403); return
        if self.headers.get('X-App-Token') != TOKEN: self.send_error(403); return
        try:
            length=int(self.headers.get('Content-Length',0))
            if self.path == '/upload':
                name=urllib.parse.unquote(self.headers.get('X-Filename',''))
                if not name or '/' in name or '\\' in name or Path(name).suffix.lower() not in ('.mp4','.mov','.mkv','.osv'): raise ValueError('仅支持 MP4、MOV、MKV、OSV 视频')
                folder=Path(staging.name)/secrets.token_hex(12); folder.mkdir()
                target=folder/name
                try:
                    with target.open('xb') as out:
                        remaining=length
                        while remaining:
                            block=self.rfile.read(min(4*1024*1024,remaining))
                            if not block: raise ValueError('拖拽导入中断，请重新添加')
                            out.write(block); remaining-=len(block)
                    dropped.add(str(target)); self.reply({'source':str(target)}); return
                except Exception:
                    target.unlink(missing_ok=True); raise
            if length>1000000: raise ValueError('请求过大')
            data=json.loads(self.rfile.read(length) or '{}')
            if self.path == '/state':
                while not worker.events.empty() and not DRAINER['on']: state['logs'].append(worker.events.get())
                self.reply({'busy':state['busy'],'logs':state['logs'],'quick_delete_count':len(worker.manifests),
                            'progress':queue_progress() if tasks else {}, 'tasks':public_tasks(), 'temporary_storage':storage.snapshot()}); return
            if self.path == '/space':
                self.reply(space_summary(data)); return
            if self.path == '/preferences':
                prefs = preferences()
                if data.get('delete_mode') in ('keep', 'trash', 'permanent'):
                    prefs['delete_mode'] = data['delete_mode']
                if isinstance(data.get('folder_tags'), bool):
                    prefs['folder_tags'] = data['folder_tags']
                if data: applog.save_json('preferences.json', prefs)
                self.reply(prefs); return
            if self.path == '/folder-tags':
                action = data.get('action', 'plan')
                if action == 'undo':
                    if working(): raise ValueError('正在处理，请等处理完再撤销')
                    with TAG_LOCK:
                        restored = [e for root in library_roots() for e in folder_tags.undo(root)]
                    for e in restored: applog.write('文件夹后缀', f"撤销：{e['to']} → {e['from']}")
                    self.reply({'restored': restored}); return
                if action == 'apply':
                    self.reply(retag()); return
                changes, skipped = folder_tags.plan(library_roots(), locked_paths())
                self.reply({'changes': changes, 'skipped': skipped}); return
            if self.path == '/retime':
                self.reply(retime_request(data)); return
            if self.path == '/pipeline':
                if data:
                    with task_condition:
                        if active_owners or state['busy']:raise ValueError('请在队列空闲时更改并行设置')
                        pipeline.configure(data)
                self.reply(dict(pipeline.settings));return
            if self.path == '/task-verification':
                mode = data.get('mode')
                if mode not in ('cpu', 'gpu'): raise ValueError('无效的检查方式')
                with task_condition:
                    item = next((i for i in tasks if i['id'] == data.get('id')), None)
                    if not item or item['status'] in ('running', 'completed'):
                        raise ValueError('只能修改尚未处理或需要重试的视频')
                    item['verification_mode'] = mode
                self.reply({});return
            if self.path == '/hardware':
                if data.get('recheck') and hardware['status'] != 'checking':
                    threading.Thread(target=probe_hardware, daemon=True).start(); time.sleep(0.05)
                self.reply(dict(hardware)); return
            if self.path == '/queue/continue': self.reply({'resumed': continue_queue()}); return
            if self.path == '/queue/discard': self.reply({'removed': discard_interrupted()}); return
            if self.path == '/open-logs':
                applog.LOG_DIR.mkdir(parents=True, exist_ok=True)
                if sys.platform == 'win32': os.startfile(str(applog.LOG_DIR))  # noqa: S606
                else: subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', str(applog.LOG_DIR)])
                self.reply({'path': str(applog.LOG_DIR)}); return
            if self.path == '/update':
                with task_condition:
                    working = state['busy'] or bool(active_owners)
                if working or sd.job['running'] or library.job['running']:
                    raise ValueError('正在处理视频或导入，请等处理完再更新（更新会重启轻存）')
                result = updater.run()
                if result['changed']:
                    RESTART['on'] = True
                    threading.Thread(target=lambda: (__import__('time').sleep(1), shutdown()), daemon=True).start()
                self.reply(result); return
            if self.path == '/quit':
                threading.Thread(target=shutdown, daemon=True).start()
                self.reply({}); return
            if self.path == '/stop':
                global queue_stopping
                with task_condition:
                    queue_stopping = True
                    owners = list(active_owners.values())
                    task_condition.notify_all()
                for owner in owners: c.cancel_work(owner.cancel)
                self.reply({}); return
            if self.path == '/pause':
                global queue_paused
                with task_condition: queue_paused = True
                self.reply({}); return
            if self.path == '/resume':
                with task_condition:
                    queue_paused = False; task_condition.notify_all()
                self.reply({}); return
            if self.path == '/clear-history':
                self.reply({'removed': clear_finished_history()}); return
            if self.path == '/cancel-task':
                task_id = data.get('id', '')
                with task_condition:
                    item = next((item for item in tasks if item['id'] == task_id), None)
                    if not item: raise ValueError('找不到这个任务')
                    if item['status'] == 'pending': item['status'] = 'cancelled'; item['message'] = '已取消，可重新加入'
                    elif item['status'] == 'running': owner = active_owners.get(task_id)
                    else: owner = None
                    task_condition.notify_all()
                if item['status'] == 'running' and owner: c.cancel_work(owner.cancel)
                self.reply({}); return
            if self.path == '/retry-task':
                task_id = data.get('id', '')
                with task_condition:
                    item = next((item for item in tasks if item['id'] == task_id), None)
                    if not item or item['status'] not in ('cancelled', 'failed'): raise ValueError('只有已取消或失败的任务可以重新加入')
                    if Path(item['output']).exists(): raise ValueError('目标文件已经存在，不能直接重试')
                    item['status'] = 'pending'; item['message'] = '等待重新处理'
                    state['busy'] = True
                    global dispatcher_thread
                    if dispatcher_thread is None or not dispatcher_thread.is_alive():
                        queue_stopping = False
                        dispatcher_thread = threading.Thread(target=dispatch_loop, daemon=True); dispatcher_thread.start()
                    task_condition.notify_all()
                self.reply({}); return
            if self.path == '/sd/state': self.reply(sd.state(LIBRARY_ROOTS)); return
            if self.path == '/sd/plan':
                self.reply(sd.build_plan(LIBRARY_ROOTS, find_storage_dir, data.get('excluded', []))); return
            if self.path == '/sd/start':
                sd.start(data.get('plan_id', ''), bool(data.get('eject'))); self.reply({}); return
            if self.path == '/sd/cancel': sd.cancel(); self.reply({}); return
            if self.path == '/sd/remove-corrupt':
                self.reply({'messages': sd.remove_corrupt(data.get('ids', []), bool(data.get('keep_ssd_copy')))}); return
            if self.path == '/sd/eject': self.reply({'messages': sd.eject(data.get('mounts', []))}); return
            if self.path.startswith('/library/'):
                self.reply(library_route(self.path, data)); return
            if self.path == '/pick': self.reply({'value':picker(data['kind'])}); return
            if self.path == '/preview': self.reply({'items':names(data)}); return
            if self.path == '/scan':
                self.reply(scan_library(data['root'], data.get('year') or None)); return
            if self.path == '/link':
                folder=Path(data['folder']).resolve(); result=[]
                for source in data['files']:
                    if source not in dropped: result.append(source); continue
                    original=folder/Path(source).name
                    if not original.is_file() or original.stat().st_size != Path(source).stat().st_size or c.digest(original) != c.digest(source):
                        raise ValueError('原片文件夹中找不到内容一致的文件：'+Path(source).name)
                    result.append(str(original))
                self.reply({'files':result}); return
            if self.path == '/start':
                self.reply({'added': enqueue(data)}); return
            if self.path == '/quick-delete':
                mode=data.get('mode','trash')
                if mode not in ('trash','permanent'): raise ValueError('请先选择回收站或彻底删除')
                records=list(worker.manifests)
                if not records: raise ValueError('本次运行还没有可快速删除的已验证成品')
                def quick_task():
                    for f in records:
                        if worker.cancel.is_set(): break
                        try: worker.events.put(c.recycle(json.loads(Path(f).read_text(encoding='utf8')),Path(f),worker.cancel,mode,fast=True))
                        except Exception as e: worker.events.put(f'{Path(f).name}：{e}')
                launch(quick_task); self.reply({}); return
            if self.path == '/delete':
                records=data['records']
                mode=data.get('mode','trash')
                if mode not in ('trash','permanent'): raise ValueError('请先选择回收站或彻底删除')
                def task():
                    for f in records:
                        if worker.cancel.is_set(): break
                        try: worker.events.put(c.recycle(json.loads(Path(f).read_text(encoding='utf8')),Path(f),worker.cancel,mode))
                        except Exception as e: worker.events.put(str(e))
                launch(task); self.reply({}); return
            self.send_error(404)
        except Exception as e: self.reply({'error':str(e)},400)

if __name__=='__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=None)
    parser.add_argument('--no-browser', action='store_true')  # inside the Mac app window
    parser.add_argument('--no-open', action='store_true')  # started by launch.py, which opens the page itself
    args = parser.parse_args()
    if sys.stdout is None or sys.stderr is None:  # started without a console (pythonw): keep output in the log folder
        applog.LOG_DIR.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(applog.LOG_DIR / 'console.log', 'a', encoding='utf8', buffering=1)
    # pythonw may inherit redirected cp1252 streams instead of None.
    # Chinese startup/shutdown messages must also be safe in that case.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf8', errors='backslashreplace')
    sys.excepthook = lambda kind, value, tb: applog.write('错误', ''.join(__import__('traceback').format_exception(kind, value, tb)))
    try:
        server=ThreadingHTTPServer(('127.0.0.1', PREFERRED_PORT if args.port is None else args.port),Handler)
    except OSError:
        server=ThreadingHTTPServer(('127.0.0.1', 0),Handler)
    SERVER.update(httpd=server, quit_button=not args.no_browser)  # the Mac app window has its own Quit
    url=f'http://127.0.0.1:{server.server_port}/'
    applog.save_json('instance.json', {'port': server.server_port, 'pid': os.getpid(), 'token': TOKEN,
                                       'started': dt.datetime.now().isoformat(timespec='seconds')})
    pipeline.initialize()
    storage.initialize()
    interrupted = load_queue()
    threading.Thread(target=storage.monitor, args=(storage_returned,), daemon=True).start()
    DRAINER['on'] = True
    for target in (drain_events, queue_saver, probe_hardware, tag_loop):
        threading.Thread(target=target, daemon=True).start()
    applog.write('轻存', f'已启动 {url}' + (f'，上次有 {interrupted} 个任务没做完' if interrupted else ''))
    print('视频压缩页面已启动：'+url+'\n运行期间请保留此窗口。在页面右上角点「退出轻存」，或按 Ctrl+C 退出。')
    if not args.no_browser and not args.no_open: webbrowser.open(url)
    try: server.serve_forever()
    except KeyboardInterrupt: worker.cancel.set()
    server.server_close()
    if (applog.load_json('instance.json') or {}).get('pid') == os.getpid():
        try: (applog.DATA_DIR / 'instance.json').unlink()
        except OSError: pass
    if RESTART['on']:
        applog.write('轻存', '已更新，正在重新启动')
        updater.relaunch(server.server_port)
