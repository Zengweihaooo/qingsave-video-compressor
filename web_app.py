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
import time

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
exiting = False
QUEUE_FILE = 'queue.json'
PREFERRED_PORT = 39390  # fixed so a bookmark / the launcher always finds the running copy
TEMP_PARTIAL_RE = re.compile(r'^\.[0-9a-f]{32}\.partial\.mp4$')
hardware = {'status': 'idle', 'items': [], 'gpu': '', 'auto': ''}
staging = tempfile.TemporaryDirectory(prefix='dji-drop-')
dropped = set()
scan_dates = {}
origin_cache = {}

SKIP_DIRS = {'$RECYCLE.BIN', 'System Volume Information', '.accelerate', 'Temp', '__pycache__', '.venv'}
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.mkv', '.osv'}
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
            (exact if child.name == token else described).append(child)
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
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
$owner = New-Object System.Windows.Forms.Form -Property @{TopMost = $true}
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
"""

def picker(kind):
    if sys.platform == 'win32':
        # Portable Python ships without Tk, so use the native Windows dialogs.
        if kind not in ('folder', 'files', 'records'): raise ValueError('未知选择类型')
        script = WINDOWS_PICKER.replace('$args[0]', f"'{kind}'")
        encoded = __import__('base64').b64encode(script.encode('utf-16-le')).decode()
        out = subprocess.check_output(['powershell', '-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', encoded],
                                      creationflags=subprocess.CREATE_NO_WINDOW)
        return json.loads(out.decode('utf-8-sig').strip() or '""')
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
    base = Path(output_root).expanduser() if output_root else LIBRARY_ROOTS[0]
    return base / f'{month:02d}.{day:02d}'

def names(data):
    start, width = int(data['start']), int(data['width'])
    if start < 0 or not 1 <= width <= 10: raise ValueError('起始序号须非负，序号位数须为 1–10')
    paths = sorted(set(data['files']), key=sort_key)
    items = []
    for index, path in enumerate(paths):
        directory = archive_directory(path, data.get('output') or None) if data.get('archive', True) else None
        items.append({
            'source': path,
            'name': c.output_name(path, data['suffix'], start + index, width) if path not in scan_dates else f'{scan_dates[path]}{("_" + data["suffix"]) if data["suffix"] else ""}_{start + index:0{width}d}.mp4',
            'folder': directory.name if directory else '',
            'directory': str(directory) if directory else '',
            'size': Path(path).stat().st_size,
        })
    return items

def public_tasks():
    with task_condition:
        return [{key: item.get(key) for key in ('id', 'source', 'output', 'video', 'audio', 'osv', 'status', 'message', 'engine')} for item in tasks]

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
        owner = current_owner
        paused = queue_paused
    detail = {}
    if owner:
        with owner.progress_lock: detail = dict(owner.progress)
    detail.update(total=len(visible), processed=done, remaining=max(0, len(visible)-done), paused=paused,
                  task_id=active['id'] if active else '', queue_status='paused' if paused else ('running' if active else 'idle'))
    return detail

def dispatch_loop():
    global dispatcher_thread, queue_stopping, current_owner
    while True:
        with task_condition:
            while queue_paused and not queue_stopping:
                task_condition.wait()
            if queue_stopping:
                if not exiting:  # quitting keeps the queue so it can continue next time
                    for item in tasks:
                        if item['status'] == 'pending': item['status'] = 'cancelled'; item['message'] = '已停止'
                state['busy'] = False; dispatcher_thread = None; queue_stopping = False
                task_condition.notify_all(); return
            item = next((candidate for candidate in tasks if candidate['status'] == 'pending'), None)
            if not item:
                state['busy'] = False; dispatcher_thread = None; current_owner = None
                task_condition.notify_all(); return
            item['status'] = 'running'; item['message'] = '处理中'
            owner = types.SimpleNamespace(events=worker.events, cancel=threading.Event(), manifests=worker.manifests,
                                          progress={}, progress_lock=threading.Lock())
            current_owner = owner
        failure, started = None, time.time()
        applog.write('压缩', f"开始：{item['source']} → {item['output']}")
        try:
            if Path(item['source']).suffix.lower() == '.osv':
                osv.process(owner, Path(item['source']), Path(item['output']), item['config'], item['osv'])
            else:
                c.App.compress(owner, [(Path(item['source']), Path(item['output']))], item['config'])
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
            else:
                item['status'] = 'failed'; item['message'] = failure or '失败，请查看日志后重试'
            current_owner = None
            task_condition.notify_all()
        applog.event('compress', source=item['source'], output=item['output'], status=item['status'], message=item['message'],
                     engine=item.get('engine'), video=item['video'], audio=item['audio'], seconds=round(time.time() - started, 1),
                     source_size=(record.get('source_state') or {}).get('size'), output_size=(record.get('output_state') or {}).get('size'))

def snapshot_queue():
    with task_condition:
        return [{key: (list(item[key]) if key == 'config' else item.get(key))
                 for key in ('id', 'source', 'output', 'config', 'osv', 'video', 'audio', 'status', 'message', 'engine')} for item in tasks]

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
        if any(target.exists() or source == target or os.path.normcase(str(target)) in reserved for source, target, _ in jobs):
            raise ValueError('输出文件已存在或已在队列中，请修改序号或输出目录')
        added = []
        for source, target, config in jobs:
            target.parent.mkdir(parents=True, exist_ok=True)
            item = {'id': uuid.uuid4().hex, 'source': str(source), 'output': str(target), 'config': config,
                    'osv': dict(export_options),
                    'video': config[0], 'audio': config[1], 'status': 'pending', 'message': '等待处理'}
            tasks.append(item); added.append(item['id'])
        tasks.sort(key=lambda item: sort_key(item['source']))
        queue_stopping = False; state['busy'] = True
        if dispatcher_thread is None or not dispatcher_thread.is_alive():
            dispatcher_thread = threading.Thread(target=dispatch_loop, daemon=True); dispatcher_thread.start()
        task_condition.notify_all()
    return added

SERVER = {'httpd': None, 'quit_button': True}
DRAINER = {'on': False}

def shutdown():
    """Stop the running step safely (originals untouched), keep the rest of the queue for next time, then exit."""
    global queue_stopping, exiting
    exiting = True
    applog.write('轻存', '正在退出')
    with task_condition:
        queue_stopping = True
        owner = current_owner
        task_condition.notify_all()
    if owner: c.cancel_work(owner.cancel)
    sd.cancel(); library.job['cancel'].set(); worker.cancel.set()
    for _ in range(100):  # up to 20 s for the running step to clean up
        with task_condition:
            idle = current_owner is None
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
                            'progress':queue_progress() if tasks else {}, 'tasks':public_tasks()}); return
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
            if self.path == '/quit':
                threading.Thread(target=shutdown, daemon=True).start()
                self.reply({}); return
            if self.path == '/stop':
                global queue_stopping
                with task_condition:
                    queue_stopping = True
                    owner = current_owner
                    task_condition.notify_all()
                if owner: c.cancel_work(owner.cancel)
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
                    elif item['status'] == 'running': owner = current_owner
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
    sys.excepthook = lambda kind, value, tb: applog.write('错误', ''.join(__import__('traceback').format_exception(kind, value, tb)))
    try:
        server=ThreadingHTTPServer(('127.0.0.1', PREFERRED_PORT if args.port is None else args.port),Handler)
    except OSError:
        server=ThreadingHTTPServer(('127.0.0.1', 0),Handler)
    SERVER.update(httpd=server, quit_button=not args.no_browser)  # the Mac app window has its own Quit
    url=f'http://127.0.0.1:{server.server_port}/'
    applog.save_json('instance.json', {'port': server.server_port, 'pid': os.getpid(), 'token': TOKEN,
                                       'started': dt.datetime.now().isoformat(timespec='seconds')})
    interrupted = load_queue()
    DRAINER['on'] = True
    for target in (drain_events, queue_saver, probe_hardware):
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
