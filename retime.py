"""时间校准: fix recordings whose camera clock (or time zone) was wrong.

All videos in one folder whose names carry a start time (`20220206_141803_ORG_004.mp4`,
`CAM_20261015160907_0001_D.MP4`) move by the same amount, so the gaps between clips stay exact.  The shift
comes either from one known moment ("this clip at 15:01 was really 11:53:39") or from a plain offset
(a time-zone mistake is a whole number of hours).

For each video: the time inside the name changes (optionally with `_APPROX` to say it was reconstructed),
the creation / modification times in the MP4 header (mvhd / tkhd / mdhd, fixed-size fields) are rewritten in
place — picture and sound bytes are never touched — and the file's mtime moves with it.  `<video>.dji.json`
compression records are renamed and their file names updated.  A date folder (`02.06_ORG`) follows the new
date when every clip lands on one day.  Every old value goes to `<root>/.qingsave/retime-log.jsonl`, so the
last batch can be undone exactly.
"""
import datetime as dt
import json
import os
import re
import struct
import uuid
from pathlib import Path

VIDEO_EXT = {'.mp4', '.mov', '.m4v', '.osv', '.insv', '.lrf', '.360'}
STAMP_RE = re.compile(r'(?<!\d)(20\d{6})([_-]?)(\d{6})(?!\d)')
APPROX_RE = re.compile(r'(?:^|_)APPROX(?=_|$)', re.I)
DATE_FOLDER_RE = re.compile(r'^(\d{1,2})\.(\d{1,2})(?!\d)')
CONTAINERS = {b'moov', b'trak', b'mdia'}
TIMED = {b'mvhd', b'tkhd', b'mdhd'}
MP4_EPOCH = dt.datetime(1904, 1, 1, tzinfo=dt.timezone.utc)
LOG = 'retime-log.jsonl'


def name_time(name):
    """(datetime, match) of the recording start written in the file name, or (None, None)."""
    for m in STAMP_RE.finditer(Path(name).stem):
        try:
            return dt.datetime.strptime(m.group(1) + m.group(3), '%Y%m%d%H%M%S'), m
        except ValueError:
            pass
    return None, None


def renamed(name, new_time, approx):
    when, m = name_time(name)
    stamp = new_time.strftime('%Y%m%d') + m.group(2) + new_time.strftime('%H%M%S')
    tail = name[m.end():]
    if approx and not APPROX_RE.search(Path(name).stem):
        tail = '_APPROX' + tail
    return name[:m.start()] + stamp + tail


def parse_position(text):
    """'15:01' / '1:02:03' / '901' -> seconds."""
    parts = [p for p in re.split(r'[:：]', str(text).strip()) if p != '']
    if not parts or len(parts) > 3:
        raise ValueError('播放位置请写成 分:秒，例如 15:01')
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + float(p)
    return seconds


def videos(folder):
    out = []
    for entry in sorted(os.scandir(folder), key=lambda e: e.name):
        if entry.is_file() and not entry.name.startswith('.') and Path(entry.name).suffix.lower() in VIDEO_EXT \
                and '.partial.' not in entry.name.lower() and name_time(entry.name)[0]:
            out.append(Path(entry.path))
    return out


def shift_of(folder, options):
    mode = options.get('mode', 'reference')
    if mode == 'offset':
        return dt.timedelta(seconds=round(float(options.get('offset_seconds') or 0)))
    ref = options.get('reference') or {}
    if not ref.get('file') or not ref.get('real'):
        return None
    start, _ = name_time(ref['file'])
    if not start:
        raise ValueError('参照视频的文件名里没有时间')
    real = dt.datetime.fromisoformat(str(ref['real']).replace('T', ' ').strip())
    camera = start + dt.timedelta(seconds=parse_position(ref.get('position') or '0'))
    return real.replace(microsecond=0) - camera.replace(microsecond=0)


def folder_target(folder, times):
    """New name for a date folder when every clip falls on one day, else None."""
    folder = Path(folder)
    m = DATE_FOLDER_RE.match(folder.name)
    days = {t.date() for t in times}
    if not m or len(days) != 1:
        return None
    day = days.pop()
    width = len(m.group(1))
    new = f'{day.month:0{width}d}.{day.day:02d}' + folder.name[m.end():]
    return None if new == folder.name else folder.with_name(new)


def plan(folder, options):
    folder = Path(folder)
    if not folder.is_dir():
        raise ValueError('找不到这个文件夹')
    files = videos(folder)
    listing = [{'name': p.name, 'time': name_time(p.name)[0].isoformat(sep=' ')} for p in files]
    shift = shift_of(folder, options)
    result = {'folder': str(folder), 'videos': listing, 'shift_seconds': None, 'items': [], 'folder_to': None,
              'warnings': []}
    if shift is None:
        return result
    result['shift_seconds'] = int(shift.total_seconds())
    approx = bool(options.get('approx', True))
    names = {p.name for p in folder.iterdir()}
    new_times = []
    for p in files:
        old = name_time(p.name)[0]
        new = old + shift
        new_times.append(new)
        target = renamed(p.name, new, approx)
        item = {'from': p.name, 'to': target, 'old': old.isoformat(sep=' '), 'new': new.isoformat(sep=' ')}
        if target != p.name and target in names:
            item['problem'] = '已有同名文件'
        result['items'].append(item)
    if options.get('rename_folder', True):
        target = folder_target(folder, new_times)
        if target is not None:
            if target.exists():
                result['warnings'].append(f'已有文件夹 {target.name}，文件夹名不改')
            else:
                result['folder_to'] = target.name
        elif DATE_FOLDER_RE.match(folder.name) and len({t.date() for t in new_times}) > 1:
            result['warnings'].append('校准后跨了不止一天，文件夹名不改')
    if any(i.get('problem') for i in result['items']):
        result['warnings'].append('有文件会和已有文件重名，不能应用')
    return result


# ---- MP4 header times -------------------------------------------------------------------------------

def _boxes(f, start, end):
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        size, kind = struct.unpack('>I4s', f.read(8))
        header = 8
        if size == 1:
            size = struct.unpack('>Q', f.read(8))[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header:
            raise ValueError('MP4 结构异常')
        yield kind, pos, header, size
        pos += size


def _time_fields(f, start, end):
    out = []
    for kind, pos, header, size in _boxes(f, start, end):
        if kind in CONTAINERS:
            out += _time_fields(f, pos + header, pos + size)
        elif kind in TIMED:
            f.seek(pos + header)
            width = 8 if f.read(1)[0] == 1 else 4
            body = pos + header + 4
            out += [(body, width), (body + width, width)]
    return out


def header_fields(path):
    """[(offset, width, value)] of every non-zero creation / modification time.  [] when not an MP4/MOV."""
    try:
        size = Path(path).stat().st_size
        with open(path, 'rb') as f:
            moov = [b for b in _boxes(f, 0, size) if b[0] == b'moov']
            if len(moov) != 1:
                return []
            _, pos, header, bsize = moov[0]
            fields = []
            for offset, width in _time_fields(f, pos + header, pos + bsize):
                f.seek(offset)
                value = int.from_bytes(f.read(width), 'big')
                if value:
                    fields.append((offset, width, value))
            return fields
    except (OSError, ValueError, struct.error, IndexError):
        return []


def write_fields(path, fields):
    with open(path, 'r+b') as f:
        for offset, width, value in fields:
            f.seek(offset)
            f.write(int(value).to_bytes(width, 'big'))
        f.flush()
        os.fsync(f.fileno())


def _mp4_seconds(local_time):
    """Seconds since 1904 (UTC) for a wall-clock time on this computer."""
    return int((local_time.astimezone(dt.timezone.utc) - MP4_EPOCH).total_seconds())


# ---- apply / undo -----------------------------------------------------------------------------------

def _log(root):
    return Path(root) / '.qingsave' / LOG


def _write_log(root, entry):
    path = _log(root)
    path.parent.mkdir(exist_ok=True)
    with path.open('a', encoding='utf8') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')


def _retarget_records(folder, mapping):
    """Rename `<video>.dji.json` and replace old video names inside every record in the folder."""
    changed = []
    for old, new in mapping.items():
        record = folder / f'{old}.dji.json'
        if record.exists() and not (folder / f'{new}.dji.json').exists():
            record.rename(folder / f'{new}.dji.json')
            changed.append({'rename': [record.name, f'{new}.dji.json']})
    for record in folder.glob('*.dji.json'):
        try:
            text = record.read_text(encoding='utf8')
        except OSError:
            continue
        updated = text
        for old, new in mapping.items():
            updated = updated.replace(old, new)
        if updated != text:
            record.write_text(updated, encoding='utf8')
            changed.append({'edit': record.name, 'pairs': [[o, n] for o, n in mapping.items() if o in text]})
    return changed


def apply(folder, options):
    folder = Path(folder)
    result = plan(folder, options)
    if result['shift_seconds'] is None:
        raise ValueError('还没有设好校准时间')
    if any(i.get('problem') for i in result['items']):
        raise ValueError('有文件会和已有文件重名，没有修改任何文件')
    if not result['shift_seconds']:
        raise ValueError('时间没有变化')
    root = folder.parent
    batch = uuid.uuid4().hex[:12]
    at = dt.datetime.now().isoformat(timespec='seconds')
    done, mapping = [], {}
    for item in result['items']:
        path = folder / item['from']
        new_time = dt.datetime.fromisoformat(item['new'])
        st = path.stat()
        fields = header_fields(path)
        new_fields = []
        delta = result['shift_seconds']
        if fields:
            # The header stores UTC; the camera's own time zone is unknown, so anchor on the movie header and
            # move every header time (and the mtime) by the same real amount.
            delta = _mp4_seconds(new_time) - fields[0][2]
            new_fields = [(o, w, v + delta) for o, w, v in fields]
        mtime_delta = delta * 10**9
        entry = {'batch': batch, 'at': at, 'folder': folder.name, 'from': item['from'], 'to': item['to'],
                 'mtime_ns_old': st.st_mtime_ns, 'mtime_ns_new': st.st_mtime_ns + mtime_delta,
                 'shift_seconds': result['shift_seconds'],
                 'fields': [{'offset': o, 'width': w, 'old': v, 'new': n}
                            for (o, w, v), (_, _, n) in zip(fields, new_fields)],
                 'options': {k: options.get(k) for k in ('mode', 'reference', 'offset_seconds', 'approx')}}
        _write_log(root, entry)  # written first, so even a crash halfway can be undone
        if new_fields:
            write_fields(path, new_fields)
        os.utime(path, ns=(st.st_atime_ns, entry['mtime_ns_new']))
        if item['to'] != item['from']:
            path.rename(folder / item['to'])
            mapping[item['from']] = item['to']
            sidecar = folder / ('._' + item['from'])  # macOS metadata file on exFAT
            if sidecar.exists() and not (folder / ('._' + item['to'])).exists():
                sidecar.rename(folder / ('._' + item['to']))
        done.append(item)
    records = _retarget_records(folder, mapping) if mapping else []
    if records:
        _write_log(root, {'batch': batch, 'at': at, 'folder': folder.name, 'records': records})
    final = folder
    if result['folder_to']:
        target = folder.with_name(result['folder_to'])
        if not target.exists():
            folder.rename(target)
            final = target
            _write_log(root, {'batch': batch, 'at': at, 'folder_from': folder.name, 'folder_to': target.name})
    return {'batch': batch, 'done': done, 'folder': str(final), 'shift_seconds': result['shift_seconds']}


def batches(root):
    """[(batch, entries)] oldest first, without batches already undone."""
    try:
        lines = _log(root).read_text(encoding='utf8').splitlines()
    except OSError:
        return []
    entries = [json.loads(line) for line in lines if line.strip()]
    gone = {e['batch'] for e in entries if e.get('undone')}
    order = []
    for e in entries:
        if e.get('batch') and e['batch'] not in gone and e['batch'] not in order:
            order.append(e['batch'])
    return [(b, [e for e in entries if e.get('batch') == b]) for b in order]


def last_batch(roots):
    """(root, batch, entries) of the newest not-yet-undone batch across `roots`."""
    best = None
    for root in roots:
        for batch, entries in batches(root):
            if best is None or entries[0]['at'] > best[2][0]['at']:
                best = (root, batch, entries)
    return best


def undo(root, batch):
    root = Path(root)
    entries = [e for b, es in batches(root) if b == batch for e in es]
    if not entries:
        return []
    folder_entry = next((e for e in entries if 'folder_from' in e), None)
    folder = root / (folder_entry['folder_to'] if folder_entry else entries[0]['folder'])
    restored = []
    files = [e for e in entries if 'from' in e]
    mapping = {e['to']: e['from'] for e in files if e['to'] != e['from']}
    for e in reversed(entries):
        if 'records' in e:
            for change in reversed(e['records']):
                if 'rename' in change:
                    a, b = change['rename']
                    if (folder / b).exists() and not (folder / a).exists():
                        (folder / b).rename(folder / a)
            for record in folder.glob('*.dji.json'):
                text = record.read_text(encoding='utf8')
                updated = text
                for new, old in mapping.items():
                    updated = updated.replace(new, old)
                if updated != text:
                    record.write_text(updated, encoding='utf8')
    for e in reversed(files):
        current = folder / e['to']
        if not current.exists():
            continue
        if e['fields']:
            write_fields(current, [(f['offset'], f['width'], f['old']) for f in e['fields']])
        st = current.stat()
        os.utime(current, ns=(st.st_atime_ns, e['mtime_ns_old']))
        if e['to'] != e['from'] and not (folder / e['from']).exists():
            current.rename(folder / e['from'])
            sidecar = folder / ('._' + e['to'])
            if sidecar.exists():
                sidecar.rename(folder / ('._' + e['from']))
        restored.append(e)
    if folder_entry and folder.exists() and not (root / folder_entry['folder_from']).exists():
        folder.rename(root / folder_entry['folder_from'])
    _write_log(root, {'undone': True, 'batch': batch, 'at': dt.datetime.now().isoformat(timespec='seconds')})
    return restored

