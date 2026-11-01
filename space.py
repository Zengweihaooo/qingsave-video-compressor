"""How much room each drive had before this round, has now, and will have after the queue is done.

Estimates use the same target bitrate as the encoder (compressor.py), corrected by how far
off earlier estimates were (events log), so the numbers get closer the more is compressed.
"""
import glob
import json
import os
import shutil
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import applog
import compressor as c

KEEP = '保留原片质量'
_cache = {}  # (path, size, mtime_ns) -> media summary or None
_lock = threading.Lock()
_learned = {'at': 0, 'factor': 1.0, 'ratio': 0.3}


def mounts():
    """Every drive worth showing, as (mount, name)."""
    found = []
    if sys.platform == 'win32':
        import ctypes
        import string
        kernel = ctypes.windll.kernel32
        for letter in string.ascii_uppercase:
            root = f'{letter}:\\'
            if kernel.GetDriveTypeW(root) not in (2, 3):  # removable, fixed
                continue
            label = ctypes.create_unicode_buffer(261)
            if not kernel.GetVolumeInformationW(root, label, 261, None, None, None, None, 0):
                continue
            found.append((root, f'{label.value} ({letter}:)' if label.value else f'{letter}:'))
        return found
    if sys.platform == 'darwin':
        found.append(('/', 'Macintosh HD'))
        try:
            volumes = sorted(Path('/Volumes').iterdir())
        except OSError:
            volumes = []
        for mount in volumes:
            if mount.is_symlink() or mount.name.startswith('com.apple') or not os.path.ismount(mount):
                continue
            try:
                os.listdir(mount)  # Time Machine and other protected volumes refuse this
            except OSError:
                continue
            found.append((str(mount), mount.name))
        return found
    return [('/', '/')]


def mount_of(path, known):
    path = os.path.normcase(os.path.abspath(path))
    best = None
    for mount, _ in known:
        m = os.path.normcase(mount.rstrip('\\/') or '/')
        if (path == m or path.startswith(m.rstrip('\\/') + os.sep) or m == '/') and (best is None or len(m) > len(best[1])):
            best = (mount, m)
    return best[0] if best else None


def usage(mount):
    try:
        u = shutil.disk_usage(mount)
        return {'total': u.total, 'free': u.free}
    except OSError:
        return None


def media(path):
    """Duration / size / bitrate of a video, cached by size and mtime."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (os.path.normcase(str(path)), st.st_size, st.st_mtime_ns)
    with _lock:
        if key in _cache:
            return _cache[key]
    info = None
    tool = c.executable('ffprobe')
    if tool and Path(path).suffix.lower() != '.osv':
        try:
            kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
            out = subprocess.run([tool, '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)],
                                 capture_output=True, timeout=20, **kwargs)
            data = json.loads(out.stdout or b'{}')
            v = c.video(data)
            n, d = map(float, (v.get('avg_frame_rate') or '0/1').split('/'))
            audio = [s for s in data['streams'] if s.get('codec_type') == 'audio']
            info = {'duration': float(data['format'].get('duration') or 0), 'width': int(v['width']), 'height': int(v['height']),
                    'fps': n / d if d else 30, 'video_rate': int(v.get('bit_rate') or 0),
                    'audio_rate': sum(int(s.get('bit_rate') or 0) for s in audio), 'audio_tracks': len(audio)}
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            info = None
    info = {**(info or {}), 'size': st.st_size}
    with _lock:
        _cache[key] = info
    return info


def learn():
    """From the events log: how far off the bitrate estimate was, and the plain size ratio as a fallback."""
    if time.time() - _learned['at'] < 60:
        return _learned
    factors, ratios = [], []
    for name in sorted(glob.glob(str(applog.LOG_DIR / 'events-*.jsonl')))[-3:]:
        try:
            lines = Path(name).read_text(encoding='utf8').splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get('kind') != 'compress' or e.get('status') != 'completed' or e.get('video') == KEEP:
                continue
            if e.get('output_size') and e.get('source_size'):
                ratios.append(e['output_size'] / e['source_size'])
            if e.get('output_size') and e.get('raw_estimate'):
                factors.append(e['output_size'] / e['raw_estimate'])
    _learned.update(at=time.time(),
                    factor=min(1.2, max(0.3, statistics.median(factors[-40:]))) if factors else 1.0,
                    ratio=min(1.0, max(0.05, statistics.median(ratios[-40:]))) if ratios else 0.3)
    return _learned


def raw_estimate(info, video_level, audio_level):
    """Bytes the encoder aims for (same rule as compressor.compress), before learning."""
    if video_level == KEEP:
        return info['size']
    if not info.get('duration'):
        return None
    rate = max(350000, int(info['width'] * info['height'] * info['fps'] * c.LEVELS[video_level][0]))
    if info.get('video_rate'):
        rate = min(rate, int(info['video_rate'] * 0.9))
    audio = info.get('audio_rate', 0) if audio_level == KEEP else c.LEVELS[audio_level][1] * 1000 * info.get('audio_tracks', 0)
    return int((rate + audio) * info['duration'] / 8 * 1.01)


def estimate(path, video_level, audio_level, budget=None):
    """(estimated output size, uncorrected bitrate estimate), or (None, None) while it cannot be worked out yet."""
    if budget is not None and budget['until'] < time.time():
        try:
            st = os.stat(path)
        except OSError:
            return None, None
        with _lock:
            info = _cache.get((os.path.normcase(str(path)), st.st_size, st.st_mtime_ns))
        if info is None:
            return None, None
    else:
        info = media(path)
    if not info:
        return None, None
    learned = learn()
    raw = raw_estimate(info, video_level, audio_level)
    if raw is None:
        return int(info['size'] * learned['ratio']), None
    if video_level == KEEP:
        return raw, raw
    return min(info['size'], int(raw * learned['factor'])), raw


def total_saved():
    """Everything 轻存 has saved so far, from the events log."""
    saved, count = 0, 0
    for name in glob.glob(str(applog.LOG_DIR / 'events-*.jsonl')):
        try:
            lines = Path(name).read_text(encoding='utf8').splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get('kind') == 'compress' and e.get('status') == 'completed' and e.get('source_size') and e.get('output_size'):
                saved += e['source_size'] - e['output_size']; count += 1
    return {'bytes': saved, 'count': count}


def summary(tasks, selection=(), delete_mode='keep'):
    """Per drive: free before the round, now, after the queue, and after also adding `selection`.

    tasks: queue items (source, output, status, video, audio, config[3] delete mode, source_size, estimated_size).
    selection: [{'source', 'video', 'audio'}] not yet added, assumed to be saved next to the original.
    """
    known = mounts()
    budget = {'until': time.time() + 2.5}
    drives = {}

    def drive(path):
        mount = mount_of(path, known)
        if mount is None:
            return None
        if mount not in drives:
            u = usage(mount)
            if not u:
                return None
            drives[mount] = {'mount': mount, 'name': dict(known).get(mount, mount), **u, 'queue_change': 0, 'trash_after': 0,
                             'selection_change': 0, 'done_change': 0, 'unknown': 0}
        return drives[mount]

    queue = {'count': 0, 'source': 0, 'estimate': 0, 'unknown': 0}
    done = {'count': 0, 'source': 0, 'output': 0}
    for t in tasks:
        mode = (t.get('config') or [None] * 4)[3] or 'keep'
        src, out = drive(t['source']), drive(t['output'])
        size = t.get('source_size') or 0
        if t['status'] == 'completed':
            try:
                actual = os.stat(t['output']).st_size
            except OSError:
                actual = None
            if actual is not None:
                done['count'] += 1; done['source'] += size; done['output'] += actual
                if out: out['done_change'] -= actual
                if src and mode == 'permanent' and not os.path.exists(t['source']): src['done_change'] += size
            continue
        if t['status'] not in ('pending', 'running', 'interrupted') or t.get('kind') == 'duplicate':
            continue
        est = t.get('estimated_size')
        if not est:
            est, raw = estimate(t['source'], t['video'], t['audio'], budget)
            if est:
                t['estimated_size'], t['raw_estimate'] = est, raw
        queue['count'] += 1; queue['source'] += size
        if t.get('kind') == 'already_done':
            est = 0  # already compressed: only the original may go
        if est is None:
            queue['unknown'] += 1
            if out: out['unknown'] += 1
            continue
        queue['estimate'] += est
        if out: out['queue_change'] -= est
        if src and mode == 'permanent': src['queue_change'] += size
        if src and mode == 'trash': src['trash_after'] += size

    sel = {'count': 0, 'source': 0, 'estimate': 0, 'unknown': 0, 'items': {}}
    for s in selection:
        d = drive(s['source'])
        try:
            size = os.stat(s['source']).st_size
        except OSError:
            continue
        est, _ = estimate(s['source'], s.get('video', '中等压缩'), s.get('audio', '中等压缩'), budget)
        sel['count'] += 1; sel['source'] += size
        sel['items'][s['source']] = est
        if est is None:
            sel['unknown'] += 1; continue
        sel['estimate'] += est
        if d:
            d['selection_change'] += (size if delete_mode == 'permanent' else 0) - est
            if delete_mode == 'trash': d['trash_after'] += size

    if not drives:
        for mount, _ in known:
            drive(mount)
    for d in drives.values():
        d['before'] = d['free'] - d['done_change']  # what it was before this round's finished files
        d['after'] = d['free'] + d['queue_change']
        d['after_selection'] = d['after'] + d['selection_change']
        d['fits'] = d['after_selection'] > 0
    return {'drives': sorted(drives.values(), key=lambda d: (d['mount'] in ('/',) or d['mount'].upper().startswith('C:'), d['name'])),
            'queue': queue, 'selection': sel, 'done': done,
            'total_saved': total_saved(), 'factor': learn()['factor']}
