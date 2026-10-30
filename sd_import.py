"""Move camera footage from SD cards onto the SSD library, sorted into MM.DD folders.

Every file is hashed while it is copied, read back from the SSD (bypassing the
page cache) and compared before the card copy is removed.  Proxy files anywhere on
the card with the same name as a main video (LRF / THM / SCR) are not copied; they
are removed from the card only once that main video has been verified.
"""
import datetime as dt
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import applog
import compressor as c

MAIN_EXTENSIONS = {'.mp4', '.osv'}
PROXY_EXTENSIONS = {'.lrf', '.thm', '.scr'}
TAG = 'ORG'
WIDTH = 3
FREE_MARGIN = 2 * 1024**3
BLOCK = 8 * 1024 * 1024
CHUNK = 64 * 1024 * 1024  # resumable copies are checked chunk by chunk
try:
    import fcntl  # macOS / Linux: bypass the page cache when reading back
    F_NOCACHE = getattr(fcntl, 'F_NOCACHE', 48)
except ImportError:
    fcntl = None
SUPPORTED = sys.platform in ('darwin', 'win32')
CARD_SKIP_DIRS = {'LOST.DIR', 'LOST+FOUND', 'System Volume Information', '$RECYCLE.BIN'}
LOG_PATH = applog.DATA_DIR / 'sd-imports.jsonl'

_info_cache = {}
_plans = {}
_lock = threading.Lock()
job = {'running': False, 'cancel': threading.Event(), 'logs': [], 'progress': {}, 'result': None, 'corrupt': []}


def log(message):
    with _lock:
        job['logs'].append(f'{dt.datetime.now():%H:%M:%S}  {message}')
        del job['logs'][:-300]
    applog.write('SD 导入', message)


def volume_info(mount):
    try:
        key = (str(mount), os.stat(mount).st_dev)
    except OSError:
        return None
    if key not in _info_cache:
        try:
            raw = subprocess.run(['diskutil', 'info', '-plist', str(mount)], capture_output=True, timeout=10).stdout
            _info_cache[key] = plistlib.loads(raw)
        except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException):
            _info_cache[key] = None
    return _info_cache[key]


def free_bytes(path):
    return shutil.disk_usage(path).free


def _no_cache(handle):
    if fcntl is not None:
        try:
            fcntl.fcntl(handle.fileno(), F_NOCACHE, 1)
        except OSError:
            pass


def _windows_volumes():
    """Drive letters as (cards, targets): removable drives are cards, fixed non-system drives are targets."""
    import ctypes
    kernel = ctypes.windll.kernel32
    system = (os.environ.get('SystemDrive') or 'C:').upper()
    cards, targets = [], []
    mask = kernel.GetLogicalDrives()
    for index in range(26):
        if not mask & (1 << index):
            continue
        letter = f'{chr(65 + index)}:'
        root = letter + '\\'
        kind = kernel.GetDriveTypeW(root)
        if kind not in (2, 3) or letter.upper() == system:  # 2 removable, 3 fixed
            continue
        label = ctypes.create_unicode_buffer(261)
        serial = ctypes.c_uint32()
        if not kernel.GetVolumeInformationW(root, label, 261, ctypes.byref(serial), None, None, None, 0):
            continue  # card reader with no card inserted
        try:
            os.listdir(root)
            free = free_bytes(root)
        except OSError:
            continue
        entry = {'mount': root, 'name': label.value or letter, 'uuid': f'{serial.value:08X}', 'disk': letter,
                 'free': free, 'total': shutil.disk_usage(root).total}
        (cards if kind == 2 else targets).append(entry)
    return cards, targets


def list_volumes(library_roots):
    """Split mounted volumes into SD cards (removable) and SSD targets (fixed)."""
    cards, targets = [], []
    if not SUPPORTED:
        return cards, targets
    if sys.platform == 'win32':
        cards, targets = _windows_volumes()
        return cards, _prioritise(targets, library_roots)
    try:
        mounts = sorted(Path('/Volumes').iterdir())
    except OSError:
        return cards, targets
    for mount in mounts:
        if mount.is_symlink() or not mount.is_dir() or not os.path.ismount(mount):
            continue
        info = volume_info(mount)
        if not info or info.get('BusProtocol') == 'Disk Image' or not info.get('WritableVolume'):
            continue
        try:
            os.listdir(mount)  # Time Machine and other protected volumes refuse this.
        except OSError:
            continue
        entry = {'mount': str(mount), 'name': mount.name, 'uuid': info.get('VolumeUUID') or info.get('DiskUUID') or mount.name,
                 'disk': info.get('ParentWholeDisk', ''), 'free': free_bytes(mount), 'total': info.get('TotalSize') or 0}
        if info.get('RemovableMedia') or info.get('Removable'):
            cards.append(entry)
        elif not info.get('Internal'):
            targets.append(entry)
    return cards, _prioritise(targets, library_roots)


def _prioritise(targets, library_roots):
    # Match by volume name so "Data" wins both as /Volumes/Data and as a Windows drive labelled Data.
    priority = [Path(root).name.lower() for root in library_roots]
    return sorted(targets, key=lambda t: (priority.index(t['name'].lower()) if t['name'].lower() in priority else len(priority), t['name']))


def _eject(mount, name):
    if sys.platform == 'win32':
        letter = mount.rstrip('\\')
        script = f"(New-Object -ComObject Shell.Application).Namespace(17).ParseName('{letter}').InvokeVerb('Eject')"
        result = subprocess.run(['powershell', '-NoProfile', '-Command', script], capture_output=True, text=True,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    else:
        result = subprocess.run(['diskutil', 'eject', mount], capture_output=True, text=True)
    return f'已推出 {name}' if result.returncode == 0 else f'推出 {name} 失败：{result.stderr.strip() or result.stdout.strip()}'


def read_log():
    # Keyed without the volume UUID: some cards report a different UUID on each
    # mount, which made earlier runs copy the same file twice.  The SHA-256 check
    # in _process still guards against two cards sharing a name, size and time.
    records = {}
    try:
        lines = LOG_PATH.read_text(encoding='utf8').splitlines()
    except OSError:
        return records
    for line in lines:
        try:
            record = json.loads(line)
            records.setdefault((record['relative'], record['size'], record['mtime_ns']), []).append(record)
        except (ValueError, KeyError):
            pass
    return records


def append_log(record):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open('a', encoding='utf8') as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + '\n')
        handle.flush(); os.fsync(handle.fileno())


def recording_time(path, stat):
    try:
        return c.stamp(path), '文件名'
    except ValueError:
        moment = getattr(stat, 'st_birthtime', None) or stat.st_mtime
        return dt.datetime.fromtimestamp(moment).strftime('%Y%m%d_%H%M%S'), '文件时间'


def scan_card(card):
    root = Path(card['mount'])
    mains, proxies = [], {}
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in CARD_SKIP_DIRS]
        for name in files:
            if name.startswith('.'):
                continue
            path = Path(current) / name
            ext = path.suffix.lower()
            if ext in MAIN_EXTENSIONS:
                mains.append(path)
            elif ext in PROXY_EXTENSIONS:
                proxies.setdefault(path.stem.upper(), []).append(path)
    items = []
    for path in mains:
        stat = path.stat()
        timestamp, basis = recording_time(path, stat)
        items.append({'card': card['mount'], 'card_name': card['name'], 'volume': card['uuid'],
                      'source': str(path), 'relative': str(path.relative_to(root)),
                      'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns, 'timestamp': timestamp, 'basis': basis,
                      'proxies': [str(p) for p in proxies.pop(path.stem.upper(), [])]})
    orphans = [str(p) for group in proxies.values() for p in group]
    return items, orphans


def existing_numbers(folder):
    numbers = set()
    try:
        for name in os.listdir(folder):
            match = re.search(rf'_{TAG}_(\d+)\.[^.]+$', name, re.I)
            if match:
                numbers.add(int(match.group(1)))
    except OSError:
        pass
    return numbers


def build_plan(library_roots, find_storage_dir, excluded=()):
    cards, targets = list_volumes(library_roots)
    excluded = set(excluded)
    history = read_log()
    items, orphans, card_summary = [], [], []
    for card in cards:
        found, lost = ([], []) if card['mount'] in excluded else scan_card(card)
        items += found; orphans += lost
        card_summary.append({**card, 'excluded': card['mount'] in excluded, 'count': len(found),
                             'size': sum(i['size'] for i in found),
                             'proxies': sum(len(i['proxies']) for i in found), 'orphans': len(lost)})
    items.sort(key=lambda i: (i['timestamp'], i['card'], i['relative']))
    remaining = {t['mount']: t['free'] - FREE_MARGIN for t in targets}
    folders, numbers, problems = {}, {}, []
    for item in items:
        copies = [r for r in history.get((item['relative'], item['size'], item['mtime_ns']), [])
                  if Path(r['dest']).is_file() and Path(r['dest']).stat().st_size == item['size']]
        if copies:
            # Copied earlier but the card copy was never removed (app closed mid-way, or a damaged video).
            previous = copies[0]
            item.update(status='copied', dest=previous['dest'], sha256=previous['sha256'],
                        copies=list(dict.fromkeys(r['dest'] for r in copies if r['sha256'] == previous['sha256'])),
                        target=str(Path(previous['dest']).parents[1]), folder=Path(previous['dest']).parent.name,
                        name=Path(previous['dest']).name)
            continue
        resumable = {t['mount']: _partial_paths(t['mount'], item)[0] for t in targets}
        resumable = {mount: part.stat().st_size for mount, part in resumable.items() if part.is_file()}
        # Continue an interrupted copy on the drive that already holds its first part.
        target = next((t for t in sorted(targets, key=lambda t: t['mount'] not in resumable)
                       if remaining[t['mount']] + resumable.get(t['mount'], 0) >= item['size']), None)
        item['resume_bytes'] = resumable.get(target['mount'], 0) if target else 0
        if not target:
            item['status'] = 'no_space'; problems.append(Path(item['source']).name); continue
        remaining[target['mount']] -= item['size']
        month, day = int(item['timestamp'][4:6]), int(item['timestamp'][6:8])
        key = (target['mount'], month, day)
        if key not in folders:
            folders[key] = find_storage_dir(month, day, [Path(target['mount'])]) or Path(target['mount']) / f'{month:02d}.{day:02d}'
        folder = folders[key]
        used = numbers.setdefault(str(folder), existing_numbers(folder))
        number = max(used, default=0) + 1
        ext = Path(item['source']).suffix.lower()
        while (folder / f"{item['timestamp']}_{TAG}_{number:0{WIDTH}d}{ext}").exists():
            number += 1
        used.add(number)
        name = f"{item['timestamp']}_{TAG}_{number:0{WIDTH}d}{ext}"
        item.update(status='pending', target=target['mount'], target_name=target['name'], folder=folder.name, name=name, dest=str(folder / name))
    plan_id = uuid.uuid4().hex
    with _lock:
        _plans.clear(); _plans[plan_id] = items
    planned = [i for i in items if i['status'] in ('pending', 'copied')]
    usage = {}
    for item in planned:
        if item['status'] == 'pending':
            usage[item['target']] = usage.get(item['target'], 0) + item['size']
    return {'plan_id': plan_id, 'cards': card_summary,
            'targets': [{**t, 'planned': usage.get(t['mount'], 0)} for t in targets],
            'items': [{k: i.get(k) for k in ('source', 'card_name', 'size', 'timestamp', 'basis', 'status', 'target', 'target_name', 'folder', 'name', 'proxies', 'resume_bytes')} for i in items],
            'count': len(planned), 'size': sum(i['size'] for i in planned),
            'proxy_count': sum(len(i['proxies']) for i in planned), 'orphans': orphans, 'no_space': problems}


def card_signature(library_roots):
    cards, _ = list_volumes(library_roots)
    return [c_['mount'] for c_ in cards]


def _progress(card, **values):
    with _lock:
        job['progress'].setdefault(card, {}).update(values)


def _hash_file(path, cancel, on_bytes):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        _no_cache(handle)
        while block := handle.read(BLOCK):
            if cancel.is_set(): raise RuntimeError('已取消')
            digest.update(block); on_bytes(len(block))
    return digest.hexdigest()


def _copy_hashed(source, partial, cancel, on_bytes):
    digest = hashlib.sha256()
    with open(source, 'rb') as reader, open(partial, 'xb') as writer:
        _no_cache(reader); _no_cache(writer)
        while block := reader.read(BLOCK):
            if cancel.is_set(): raise RuntimeError('已取消')
            digest.update(block); writer.write(block); on_bytes(len(block))
        writer.flush(); os.fsync(writer.fileno())
    return digest.hexdigest()


def _partial_paths(target_root, item):
    key = hashlib.sha256(f"{item['relative']}|{item['size']}|{item['mtime_ns']}".encode()).hexdigest()[:24]
    folder = Path(target_root) / '.qingsave' / 'partials'
    return folder / f'{key}.part', folder / f'{key}.json'


def _copy_resumable(item, part, meta_path, cancel, step):
    """Copy in 64 MB chunks, hashing each chunk as it comes off the card and saving the list of chunk
    hashes after every chunk, so an interrupted copy (quit, logout, card pulled) continues later."""
    source = Path(item['source'])
    chunks = []
    try:
        meta = json.loads(meta_path.read_text(encoding='utf8'))
    except (OSError, ValueError):
        meta = {}
    same = meta.get('relative') == item['relative'] and meta.get('size') == item['size'] and meta.get('mtime_ns') == item['mtime_ns']
    if part.is_file() and same:
        with open(part, 'rb') as handle:  # keep only the chunks that read back intact
            _no_cache(handle)
            for expected in meta.get('chunks', []):
                block = handle.read(CHUNK)
                if len(block) != min(CHUNK, item['size'] - len(chunks) * CHUNK) or hashlib.sha256(block).hexdigest() != expected:
                    break
                chunks.append(expected); step(len(block))
        os.truncate(part, len(chunks) * CHUNK)
        if chunks:
            log(f'↻ {source.name} 接着上次复制，已有 {len(chunks) * CHUNK / 1e9:.1f} GB 校验无误')
    else:
        part.unlink(missing_ok=True)
    with open(source, 'rb') as reader, open(part, 'ab' if chunks else 'wb') as writer:
        _no_cache(reader); _no_cache(writer)
        reader.seek(len(chunks) * CHUNK)
        while block := reader.read(CHUNK):
            if cancel.is_set(): raise RuntimeError('已取消')
            writer.write(block); writer.flush(); os.fsync(writer.fileno())
            chunks.append(hashlib.sha256(block).hexdigest()); step(len(block))
            temp = meta_path.with_suffix('.tmp')
            temp.write_text(json.dumps({'relative': item['relative'], 'size': item['size'], 'mtime_ns': item['mtime_ns'],
                                        'source': item['source'], 'chunks': chunks}), encoding='utf8')
            os.replace(temp, meta_path)
    return chunks


def _verify_chunks(part, chunks, cancel, step):
    """Read the copy back past the cache; returns its SHA-256 only if every chunk matches the card."""
    whole = hashlib.sha256()
    with open(part, 'rb') as handle:
        _no_cache(handle)
        for expected in chunks:
            if cancel.is_set(): raise RuntimeError('已取消')
            block = handle.read(CHUNK)
            if hashlib.sha256(block).hexdigest() != expected:
                return None
            whole.update(block); step(len(block))
        if handle.read(1):
            return None
    return whole.hexdigest()


def _problem(path):
    """None when FFprobe reads a video track with a duration, otherwise a short reason."""
    try:
        info = c.probe(path)
    except Exception as error:
        return '录制被中断，视频索引（moov）缺失' if 'moov atom not found' in str(error) else 'FFprobe 无法读取'
    if not any(s.get('codec_type') == 'video' for s in info.get('streams', [])):
        return '没有视频轨'
    try:
        if float(info.get('format', {}).get('duration') or 0) > 0:
            return None
    except ValueError:
        pass
    return '几乎没有内容（读不出时长）'


def _save_lrf(item, dest, cancel, step):
    """Keep the camera's low-res proxy next to a damaged main video; returns (path, playable)."""
    lrf = next((Path(p) for p in item['proxies'] if p.lower().endswith('.lrf')), None)
    if not lrf or not lrf.is_file():
        return None, False
    target = dest.with_name(dest.stem + '_LRF.mp4')
    if target.is_file() and target.stat().st_size == lrf.stat().st_size:
        if _hash_file(target, cancel, step) != _hash_file(lrf, cancel, step):
            raise ValueError(f'{target.name} 已存在但内容不同，未覆盖')
    else:
        partial = target.parent / f'.{target.name}.qingsave-partial'
        try:
            source_hash = _copy_hashed(lrf, partial, cancel, step)
            if _hash_file(partial, cancel, step) != source_hash:
                raise ValueError('LRF 读回校验不一致')
            if target.exists():
                raise ValueError(f'{target.name} 已被占用')
            os.rename(partial, target)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        stat = lrf.stat()
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    return str(target), _problem(target) is None


def _delete_from_card(item):
    os.unlink(item['source'])
    removed = 0
    for proxy in item['proxies']:
        try:
            os.unlink(proxy); removed += 1
        except FileNotFoundError:
            pass
    return removed


def _process(item, cancel, step):
    source, dest = Path(item['source']), Path(item['dest'])
    stat = source.stat()
    if stat.st_size != item['size'] or stat.st_mtime_ns != item['mtime_ns']:
        raise ValueError('卡上文件在确认后发生了变化，已跳过')
    if item['status'] == 'copied':
        _progress(item['card'], phase='核对上次已复制的文件', file=source.name)
        if _hash_file(dest, cancel, step) != item['sha256'] or _hash_file(source, cancel, step) != item['sha256']:
            raise ValueError('上次的复制件与卡上原片不一致，已保留两边文件')
        return _finish(item, dest, cancel, step)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part, meta = _partial_paths(item['target'], item)
    part.parent.mkdir(parents=True, exist_ok=True)
    c.hidden_file(part.parents[1])
    try:
        _progress(item['card'], phase='接着上次复制' if item.get('resume_bytes') else '复制并计算校验值', file=source.name)
        chunks = _copy_resumable(item, part, meta, cancel, step)
        _progress(item['card'], phase='从固态硬盘读回校验', file=source.name)
        source_hash = _verify_chunks(part, chunks, cancel, step) if part.stat().st_size == item['size'] else None
        if not source_hash:
            raise ValueError('读回校验不一致，卡上原片保留')
        if dest.exists():
            raise ValueError('目标文件名已被占用，卡上原片保留')
        os.rename(part, dest)
        meta.unlink(missing_ok=True)
    except ValueError:
        part.unlink(missing_ok=True); meta.unlink(missing_ok=True)  # bad data is not worth resuming
        raise
    # Cancel, quit or a pulled card keep the checked part for next time.
    os.utime(dest, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    append_log({'volume': item['volume'], 'relative': item['relative'], 'size': item['size'], 'mtime_ns': item['mtime_ns'],
                'dest': str(dest), 'sha256': source_hash, 'verified_at': dt.datetime.now().astimezone().isoformat()})
    item['copies'] = [str(dest)]
    return _finish(item, dest, cancel, step)


def _finish(item, dest, cancel, step):
    """Delete the card copy of a readable video; park a damaged one for the user's decision."""
    _progress(item['card'], phase='检查视频可读取', file=Path(item['source']).name)
    problem = _problem(dest)
    if not problem:
        return _delete_from_card(item), None
    _progress(item['card'], phase='视频损坏，保存 LRF 到固态硬盘', file=Path(item['source']).name)
    lrf, lrf_ok = _save_lrf(item, dest, cancel, step)
    entry = {'id': uuid.uuid4().hex, 'source': item['source'], 'size': item['size'], 'mtime_ns': item['mtime_ns'],
             'proxies': item['proxies'], 'copies': item.get('copies') or [str(dest)], 'reason': problem,
             'lrf': lrf, 'lrf_ok': lrf_ok}
    with _lock:
        job['corrupt'] = [e for e in job['corrupt'] if e['source'] != item['source']] + [entry]
    return 0, entry


def _run_card(card, items, cancel, totals):
    def step(count):
        with _lock:
            job['progress'][card]['bytes_done'] += count
    work = 2 * sum(i['size'] for i in items)  # one pass from the card, one read-back from the SSD
    _progress(card, bytes_done=0, bytes_total=work, done=0, total=len(items))
    for item in items:
        if cancel.is_set():
            break
        name = Path(item['source']).name
        started, outcome = dt.datetime.now(), 'failed'
        try:
            proxies, damaged = _process(item, cancel, step)
            outcome = 'damaged' if damaged else 'moved'
            target = f"{item.get('target_name') or Path(item['target']).name}/{item['folder']}/{item['name']}"
            if not damaged:
                totals['moved'] += 1
                log(f'✓ {name} → {target}' + (f'（另删 {proxies} 个代理文件）' if proxies else ''))
            else:
                totals['kept'] += 1
                saved = (f"LRF 已存到 {Path(damaged['lrf']).name}" + ('' if damaged['lrf_ok'] else '（但 LRF 也读不出）')) if damaged['lrf'] else '没有可保存的 LRF'
                log(f"⚠ {name} 视频损坏：{damaged['reason']}。{saved}。请在下方确认是否移除损坏文件")
        except Exception as error:
            if cancel.is_set():
                totals['paused'] += 1; outcome = 'paused'
                log(f'⏸ {name} 已暂停，已复制并校验的部分会保留，下次插卡确认后接着复制')
            else:
                totals['failed'] += 1
                log(f'✗ {name}：{error}')
        applog.event('sd_import', source=item['source'], dest=item.get('dest'), size=item['size'], status=outcome,
                     seconds=round((dt.datetime.now() - started).total_seconds(), 1))
        with _lock:
            job['progress'][card]['done'] += 1


def start(plan_id, eject=False):
    with _lock:
        if job['running']:
            raise ValueError('导入正在进行')
        items = [i for i in _plans.get(plan_id, []) if i['status'] in ('pending', 'copied')]
        if plan_id not in _plans:
            raise ValueError('导入计划已过期，请重新检测')
        if not items:
            raise ValueError('没有需要移动的文件')
        _plans.clear()
        job.update(running=True, logs=[], progress={}, result=None)
        job['cancel'] = threading.Event()
    cancel = job['cancel']
    by_card = {}
    for item in items:
        by_card.setdefault(item['card'], []).append(item)

    def controller():
        totals = {'moved': 0, 'kept': 0, 'failed': 0, 'paused': 0}
        log(f'开始移动 {len(items)} 个视频，来自 {len(by_card)} 张卡；逐个复制、校验、删除')
        for card, group in by_card.items():  # strictly one file at a time, card after card
            if cancel.is_set():
                break
            _run_card(card, group, cancel, totals)
        summary = f"完成：移动 {totals['moved']} 个" + (f"，{totals['kept']} 个视频损坏，等待你确认" if totals['kept'] else '') + (f"，失败 {totals['failed']} 个" if totals['failed'] else '')
        if cancel.is_set(): summary = '已暂停。' + summary + '。剩下的视频还在卡上，下次插卡会接着处理'
        log(summary)
        if eject and not cancel.is_set() and not totals['failed'] and not totals['kept']:
            for card, group in by_card.items():
                log(_eject(card, group[0]['card_name']))
        with _lock:
            job['running'] = False; job['result'] = {**totals, 'summary': summary}

    threading.Thread(target=controller, daemon=True).start()


def cancel():
    job['cancel'].set()


def state(library_roots):
    with _lock:
        progress = {card: dict(values) for card, values in job['progress'].items()}
        snapshot = {'running': job['running'], 'logs': list(job['logs']), 'progress': progress, 'result': job['result'],
                    'corrupt': [dict(entry) for entry in job['corrupt']]}
    snapshot['cards'] = card_signature(library_roots)
    snapshot['supported'] = SUPPORTED
    return snapshot


def remove_corrupt(ids, keep_ssd_copy=False):
    """After the user confirms: drop damaged videos from the card and SSD, keeping the saved LRF."""
    with _lock:
        if job['running']:
            raise ValueError('导入进行中，请等待完成后再移除')
        chosen = [e for e in job['corrupt'] if e['id'] in set(ids)]
    if not chosen:
        raise ValueError('没有选中的损坏文件')
    messages = []
    for entry in chosen:
        name = Path(entry['source']).name
        try:
            source = Path(entry['source'])
            if source.exists():
                stat = source.stat()
                if stat.st_size != entry['size'] or stat.st_mtime_ns != entry['mtime_ns']:
                    raise ValueError('卡上文件已变化，未删除')
                source.unlink()
            for proxy in entry['proxies']:
                Path(proxy).unlink(missing_ok=True)
            # copies all share one SHA-256, so keeping the first loses nothing.
            removed = 0
            for copy in (entry['copies'][1:] if keep_ssd_copy else entry['copies']):
                path = Path(copy)
                if path.is_file() and path.stat().st_size == entry['size']:
                    path.unlink(); removed += 1
            kept = f"，损坏原片保留在 {Path(entry['copies'][0]).parent.name}/{Path(entry['copies'][0]).name}" if keep_ssd_copy else ''
            messages.append(f'已从卡上移除 {name}' + kept + (f'，删除 {removed} 份' + ('重复' if keep_ssd_copy else '损坏') + '副本' if removed else '')
                            + (f"，LRF 保留在 {entry['lrf']}" if entry['lrf'] else ''))
            with _lock:
                job['corrupt'] = [e for e in job['corrupt'] if e['id'] != entry['id']]
        except Exception as error:
            messages.append(f'{name}：{error}')
    for message in messages:
        log(message)
    return messages


def eject(mounts):
    cards, _ = list_volumes(())
    names = {card['mount']: card['name'] for card in cards}
    messages = []
    for mount in mounts:
        if mount not in names:
            raise ValueError('只能推出检测到的 SD 卡')
        messages.append(_eject(mount, names[mount]))
    return messages
