"""Scan a whole footage drive: recording dates, original vs compressed, conflicts, ORG marking.

Everything is keyed by paths relative to the scanned root, so the same drive gives the
same result on Windows (H:\\) and macOS (/Volumes/...).  Reads are side-effect free except
for a probe cache in <root>/.qingsave/; renaming only happens through mark_originals(),
which logs every rename so undo_last_mark() can put names back.
"""
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import threading
import uuid
from pathlib import Path

import compressor as c

VIDEO_EXTENSIONS = {'.mp4', '.mov', '.mkv', '.osv'}
SIDECAR_EXTENSIONS = {'.lrf', '.thm', '.scr'}
SKIP_DIRS = {'$RECYCLE.BIN', 'System Volume Information', '.Trashes', '.Spotlight-V100', '.fseventsd',
             '.TemporaryItems', '.accelerate', '__pycache__', '.venv', '.qingsave', '.qingsave-osv-work'}
STATE_DIR = '.qingsave'
CACHE_NAME = 'library-cache.json'
RENAME_LOG = 'rename-log.jsonl'
EXPORT_NAME = '轻存素材日历.html'
SAMPLE = 1024 * 1024
OVERLAP_TOLERANCE = 2.0  # seconds; DJI chapter files touch end-to-start

CMP_RE = re.compile(r'(?:^|[_. -])(cmp|compressed)(?:$|[_. -])', re.I)
ORG_RE = re.compile(r'(?:^|[_. -])(org|original)(?:$|[_. -])', re.I)
LRF_RE = re.compile(r'(?:^|[_. -])lrf$', re.I)

# File-name timestamps, most specific first.
NAME_DATETIME = [
    re.compile(r'(?<!\d)(20\d{2})(\d{2})(\d{2})[_-]?(\d{2})(\d{2})(\d{2})(?!\d)'),                       # 20261015160907 / 20261015_160907
    re.compile(r'(?<!\d)(20\d{2})[-._](\d{2})[-._](\d{2})[ _T-]+(\d{2})[-._:](\d{2})[-._:](\d{2})(?!\d)'),  # 2026-01-13_23-24-57
]
NAME_DATE = [
    re.compile(r'(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)'),
    re.compile(r'(?<!\d)(20\d{2})[-._年](\d{1,2})[-._月](\d{1,2})日?(?!\d)'),
]
# Folder names.  Each returns (level, year, month, day, description).
FOLDER_PATTERNS = [
    ('day', re.compile(r'^(20\d{2})\s*[-._年 ]\s*(\d{1,2})\s*[-._月 ]\s*(\d{1,2})\s*日?(?!\d)\s*[-_ .]*(.*)$')),   # 2025年10月21日 / 2025-10-21 xx
    ('day', re.compile(r'^(20\d{2})(\d{2})(\d{2})(?!\d)\s*[-_ .]*(.*)$')),                                       # 20251021
    ('month', re.compile(r'^(20\d{2})\s*[-._年]\s*(\d{1,2})\s*月?(?!\d)\s*[-_ .]*(.*)$')),                        # 2025.10 / 2025年10月 / 2026.08Done
    ('md', re.compile(r'^(\d{1,2})\s*[.\-_月]\s*(\d{1,2})\s*日?(?![\d.])\s*[-_ .]*(.*)$')),                     # 12.5 / 10.12 第一次见 rebelo / 10月21日
    ('year', re.compile(r'^(20\d{2})\s*年?(?!\d)\s*[-_ .]*(.*)$')),                                              # 2025 / 2025年
    ('year2', re.compile(r'^(\d{2})\s*年(?!\d)\s*[-_ .]*(.*)$')),                                                # 25年
    ('dayonly', re.compile(r'^(\d{1,2})\s*日\s*[-_ .]*(.*)$')),                                                   # 21日 (inside a month folder)
]

job = {'running': False, 'cancel': threading.Event(), 'progress': {}, 'result': None, 'error': None, 'root': None}
_lock = threading.Lock()


def _valid(y, m, d):
    try:
        return dt.date(int(y), int(m), int(d))
    except ValueError:
        return None


def parse_folder(name):
    """Return {'level', 'year', 'month', 'day', 'desc'} or None for a folder name."""
    for level, pattern in FOLDER_PATTERNS:
        match = pattern.match(name.strip())
        if not match:
            continue
        g = match.groups()
        if level == 'day' and _valid(g[0], g[1], g[2]):
            return {'level': 'day', 'year': int(g[0]), 'month': int(g[1]), 'day': int(g[2]), 'desc': g[3].strip()}
        if level == 'month' and 1 <= int(g[1]) <= 12:
            return {'level': 'month', 'year': int(g[0]), 'month': int(g[1]), 'day': None, 'desc': g[2].strip()}
        if level == 'md' and 1 <= int(g[0]) <= 12 and 1 <= int(g[1]) <= 31:
            return {'level': 'md', 'year': None, 'month': int(g[0]), 'day': int(g[1]), 'desc': g[2].strip()}
        if level == 'year':
            return {'level': 'year', 'year': int(g[0]), 'month': None, 'day': None, 'desc': g[1].strip()}
        if level == 'year2':
            return {'level': 'year', 'year': 2000 + int(g[0]), 'month': None, 'day': None, 'desc': g[1].strip()}
        if level == 'dayonly' and 1 <= int(g[0]) <= 31:
            return {'level': 'dayonly', 'year': None, 'month': None, 'day': int(g[0]), 'desc': g[1].strip()}
    return None


def folder_context(parts):
    """Walk folder names root→leaf and resolve the date they describe, with inherited year/month."""
    year = month = None
    best, desc, labels = None, '', []
    for part in parts:
        parsed = parse_folder(part)
        if not parsed:
            continue
        if parsed['year']:
            year = parsed['year']
        if parsed['level'] == 'month':
            month = parsed['month']
        elif parsed['level'] in ('day', 'md'):
            month = parsed['month']
            best = (year, parsed['month'], parsed['day'])
            desc = parsed['desc'] or desc
        elif parsed['level'] == 'dayonly' and month:
            best = (year, month, parsed['day'])
            desc = parsed['desc'] or desc
        labels.append(part)
    return {'year': year, 'month': month, 'day': best, 'desc': desc}


def name_time(stem):
    for pattern in NAME_DATETIME:
        for match in pattern.finditer(stem):
            try:
                return dt.datetime(*map(int, match.groups())), True
            except ValueError:
                pass
    for pattern in NAME_DATE:
        for match in pattern.finditer(stem):
            day = _valid(*match.groups())
            if day:
                return dt.datetime(day.year, day.month, day.day), False
    return None, False


def classify_tag(stem):
    if LRF_RE.search(stem):
        return 'proxy'
    if CMP_RE.search(stem):
        return 'compressed'
    if ORG_RE.search(stem):
        return 'original'
    return None


def profile(info, suffix):
    """Bits per pixel per frame, mirroring web_app.origin_profile thresholds."""
    try:
        stream = c.video(info)
    except Exception:
        return {'kind': 'unknown', 'codec': '', 'width': 0, 'height': 0, 'fps': 0, 'bitrate': 0, 'bppf': 0}
    try:
        n, d = map(float, stream.get('avg_frame_rate', '0/1').split('/'))
        fps = n / d if d else 0
    except ValueError:
        fps = 0
    bitrate = int(stream.get('bit_rate') or info.get('format', {}).get('bit_rate') or 0)
    width, height = int(stream.get('width', 0)), int(stream.get('height', 0))
    bppf = bitrate / (width * height) / fps if bitrate and width and height and fps else 0
    codec = stream.get('codec_name', '').lower()
    low, high = (0.20, 0.23) if codec in ('h264', 'avc1') else (0.18, 0.20)
    kind = 'likely_compressed' if bppf and bppf <= low else ('camera_source' if bppf >= high else 'unknown')
    if suffix == '.osv':
        kind = 'camera_source'
    return {'kind': kind, 'codec': codec, 'width': width, 'height': height, 'fps': round(fps, 3), 'bitrate': bitrate, 'bppf': round(bppf, 4)}


def probe_file(path):
    try:
        info = c.probe(path)
    except Exception as error:
        broken = 'moov atom not found' in str(error)
        return {'ok': False, 'error': '视频索引缺失（录制中断）' if broken else 'FFprobe 无法读取', 'duration': 0}
    fmt = info.get('format', {})
    try:
        duration = float(fmt.get('duration') or 0)
    except ValueError:
        duration = 0
    created = (fmt.get('tags') or {}).get('creation_time', '')
    return {'ok': duration > 0, 'error': '' if duration > 0 else '读不出时长', 'duration': duration,
            'creation_time': created, **profile(info, Path(path).suffix.lower())}


def sample_hash(path, size):
    digest = hashlib.sha256(str(size).encode())
    with open(path, 'rb') as handle:
        for offset in sorted({0, max(0, size // 2 - SAMPLE // 2), max(0, size - SAMPLE)}):
            handle.seek(offset)
            digest.update(handle.read(SAMPLE))
    return digest.hexdigest()


def full_hash(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while block := handle.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def rel_of(root, path):
    return Path(path).relative_to(root).as_posix()


def load_cache(root):
    try:
        return json.loads((root / STATE_DIR / CACHE_NAME).read_text(encoding='utf8'))
    except (OSError, ValueError):
        return {}


def save_cache(root, cache):
    folder = root / STATE_DIR
    try:
        folder.mkdir(exist_ok=True)
        c.hidden_file(folder)
        temp = folder / (CACHE_NAME + '.tmp')
        temp.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf8')
        os.replace(temp, folder / CACHE_NAME)
    except OSError:
        pass  # read-only drive: scanning still works, just without a cache


def walk(root):
    videos, sidecars, manifests, folders = [], [], [], []
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith('.'))
        here = Path(current)
        count = 0
        for name in files:
            if name.startswith('.') or name.startswith('._'):
                continue
            path = here / name
            lower = name.lower()
            if lower.endswith('.dji.json'):
                manifests.append(path)
            elif '.partial.' in lower or lower.endswith('.qingsave-partial'):
                continue
            elif path.suffix.lower() in VIDEO_EXTENSIONS:
                videos.append(path); count += 1
            elif path.suffix.lower() in SIDECAR_EXTENSIONS:
                sidecars.append(path)
        if here != root:
            folders.append((here, count))
    return videos, sidecars, manifests, folders


def read_manifests(manifests):
    """Map compressed-output file names to their source names (paths in records are from the encoding machine)."""
    outputs, sources = {}, {}
    for manifest in manifests:
        try:
            record = json.loads(manifest.read_text(encoding='utf8'))
        except (OSError, ValueError):
            continue
        if not record.get('verified'):
            continue
        source = re.split(r'[\\/]', str(record.get('source', '')))[-1]
        output = re.split(r'[\\/]', str(record.get('output', '')))[-1]
        if source and output:
            outputs[output.lower()] = {'source': source, 'deleted': bool(record.get('deleted')), 'manifest': str(manifest)}
            sources.setdefault(source.lower(), []).append(output)
    return outputs, sources


def _progress(**values):
    with _lock:
        job['progress'].update(values)


def scan(root, cancel=None, workers=4):
    root = Path(root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError('扫描位置不是文件夹')
    cancel = cancel or threading.Event()
    _progress(phase='列出文件', done=0, total=0)
    videos, sidecars, manifest_paths, folders = walk(root)
    outputs, sources = read_manifests(manifest_paths)
    cache = load_cache(root)
    fresh = {}
    stats = {}
    for path in videos:
        st = path.stat()
        stats[path] = st
    todo = []
    for path in videos:
        st = stats[path]
        key = f'{rel_of(root, path)}|{st.st_size}|{st.st_mtime_ns}'
        if key in cache:
            fresh[key] = cache[key]
        else:
            todo.append((key, path))
    _progress(phase='读取视频信息', done=len(videos) - len(todo), total=len(videos))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(probe_file, path): key for key, path in todo}
        for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
            if cancel.is_set():
                pool.shutdown(cancel_futures=True)
                raise RuntimeError('扫描已取消')
            fresh[futures[future]] = future.result()
            with _lock:
                job['progress']['done'] += 1
            if index % 200 == 0:
                save_cache(root, fresh)
    save_cache(root, fresh)

    sidecar_stems = {}
    for path in sidecars:
        sidecar_stems.setdefault(path.stem.lower(), []).append(rel_of(root, path))

    files = []
    for path in videos:
        st = stats[path]
        rel = rel_of(root, path)
        meta = fresh[f'{rel}|{st.st_size}|{st.st_mtime_ns}']
        parts = list(Path(rel).parts[:-1])
        context = folder_context(parts)
        stem = path.stem
        flags = []
        when, precise = name_time(stem)
        basis = '文件名' if when else ''
        folder_day = None
        if context['day']:
            y, m, d = context['day']
            if not y:
                y = (when.year if when else dt.datetime.fromtimestamp(st.st_mtime).year)
                if not when:
                    flags.append('文件夹没写年份，按文件时间推断')
            folder_day = _valid(y, m, d)
        if when and folder_day and when.date() != folder_day:
            # Midnight recordings sit in the previous day's folder; only flag real mismatches.
            if abs((when.date() - folder_day).days) > 1:
                flags.append(f'文件名日期与文件夹 {folder_day:%m.%d} 不一致')
        if not when and folder_day:
            when, basis = dt.datetime(folder_day.year, folder_day.month, folder_day.day), '文件夹'
        created = meta.get('creation_time') or ''
        if created and (not when or not precise):
            try:
                meta_time = dt.datetime.fromisoformat(created.replace('Z', '+00:00')).replace(tzinfo=None)
                if not when or meta_time.date() == when.date():
                    when, precise, basis = meta_time, True, (basis + '+元数据' if basis else '元数据')
            except ValueError:
                pass
        if not when:
            when, precise, basis = dt.datetime.fromtimestamp(st.st_mtime), True, '文件修改时间'
            flags.append('日期只能按文件修改时间推断')
        tag = classify_tag(stem)
        in_manifest = outputs.get(path.name.lower())
        if tag == 'proxy':
            kind = 'proxy'
        elif tag == 'compressed' or in_manifest:
            kind = 'compressed'
        elif tag == 'original':
            kind = 'original'
        elif meta.get('kind') == 'likely_compressed':
            kind = 'compressed'; flags.append('码率偏低，判断为压缩过')
        elif meta.get('kind') == 'camera_source':
            kind = 'original'
        else:
            kind = 'unknown'
        if not meta.get('ok'):
            flags.append(meta.get('error') or '无法读取')
        files.append({
            'id': len(files), 'rel': rel, 'name': path.name, 'folder': '/'.join(parts), 'size': st.st_size,
            'start': when.isoformat(timespec='seconds'), 'precise': precise, 'basis': basis,
            'duration': round(meta.get('duration') or 0, 2), 'kind': kind, 'marked': tag in ('original', 'compressed'),
            'readable': bool(meta.get('ok')), 'codec': meta.get('codec', ''), 'width': meta.get('width', 0),
            'height': meta.get('height', 0), 'fps': meta.get('fps', 0), 'bitrate': meta.get('bitrate', 0),
            'desc': context['desc'], 'flags': flags,
            'source_of': in_manifest['source'] if in_manifest else '',
            'sidecars': sidecar_stems.get(stem.lower(), []),
        })
    _progress(phase='查找重复与冲突')
    issues = find_issues(root, files, sources, cancel)
    tree = folder_tree(root, folders)
    years = sorted({f['start'][:4] for f in files})
    result = {'root': str(root), 'scanned_at': dt.datetime.now().isoformat(timespec='seconds'),
              'files': files, 'issues': issues, 'tree': tree, 'years': years,
              'summary': {
                  'total': len(files), 'size': sum(f['size'] for f in files),
                  'original': sum(f['kind'] == 'original' for f in files),
                  'unmarked_original': len(issues['unmarked']),
                  'compressed': sum(f['kind'] == 'compressed' for f in files),
                  'unknown': sum(f['kind'] == 'unknown' for f in files),
                  'unreadable': sum(not f['readable'] for f in files),
                  'same_time': len(issues['same_time']), 'overlap': len(issues['overlap']),
                  'duplicates': len(issues['duplicates']), 'pairs': len(issues['pairs'])}}
    return result


def find_issues(root, files, manifest_sources, cancel):
    by_name = {f['name'].lower(): f for f in files}
    # (b) identical files: same size, then sampled hash.
    by_size = {}
    for f in files:
        by_size.setdefault(f['size'], []).append(f)
    duplicates = []
    for size, group in by_size.items():
        if len(group) < 2 or size == 0:
            continue
        hashed = {}
        for f in group:
            if cancel.is_set():
                raise RuntimeError('扫描已取消')
            try:
                hashed.setdefault(sample_hash(root / f['rel'], size), []).append(f['id'])
            except OSError:
                pass
        duplicates += [{'ids': ids, 'size': size, 'verified': False} for ids in hashed.values() if len(ids) > 1]
    duplicate_ids = {i for group in duplicates for i in group['ids'][1:]}

    # (c) one recording kept both as compressed and original.
    pairs, paired = [], set()
    for f in files:
        if f['kind'] == 'compressed' and f['source_of']:
            source = by_name.get(f['source_of'].lower())
            if source and source['id'] != f['id']:
                pairs.append({'original': source['id'], 'compressed': f['id'], 'by': '压缩记录'})
                paired.add((source['id'], f['id']))
    precise = {}
    for f in files:
        if f['precise'] and f['kind'] != 'proxy' and f['id'] not in duplicate_ids:
            precise.setdefault(f['start'], []).append(f)
    for start, group in precise.items():
        originals = [f for f in group if f['kind'] in ('original', 'unknown')]
        compressed = [f for f in group if f['kind'] == 'compressed']
        for o in originals:
            for cmp in compressed:
                if (o['id'], cmp['id']) in paired:
                    continue
                if o['duration'] and cmp['duration'] and abs(o['duration'] - cmp['duration']) > max(1.0, o['duration'] * 0.01):
                    continue
                pairs.append({'original': o['id'], 'compressed': cmp['id'], 'by': '同一拍摄时间'})
                paired.add((o['id'], cmp['id']))
    pair_members = {x for p in pairs for x in (p['original'], p['compressed'])}
    pair_of = {}
    for p in pairs:
        pair_of.setdefault(p['original'], set()).add(p['compressed'])
        pair_of.setdefault(p['compressed'], set()).add(p['original'])

    def related(a, b):
        return b in pair_of.get(a, ()) or any(a in g['ids'] and b in g['ids'] for g in duplicates)

    # (a) timeline conflicts: identical start second, or real overlap.
    same_time = []
    for start, group in precise.items():
        if len(group) < 2:
            continue
        ids = [f['id'] for f in group]
        if all(related(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]):
            continue
        same_time.append({'start': start, 'ids': ids})
    timed = sorted((f for f in files if f['precise'] and f['duration'] and f['kind'] != 'proxy' and f['id'] not in duplicate_ids),
                   key=lambda f: f['start'])
    overlap, active = [], []
    for f in timed:
        start = dt.datetime.fromisoformat(f['start'])
        end = start + dt.timedelta(seconds=f['duration'])
        active = [(e, g) for e, g in active if (e - start).total_seconds() > OVERLAP_TOLERANCE]
        for other_end, g in active:
            if g['start'] == f['start'] or related(f['id'], g['id']):
                continue
            seconds = min(other_end, end) - start
            if seconds.total_seconds() > OVERLAP_TOLERANCE:
                overlap.append({'ids': [g['id'], f['id']], 'seconds': round(seconds.total_seconds(), 1)})
        active.append((end, f))

    unmarked = [f['id'] for f in files if f['kind'] == 'original' and not f['marked']]
    unknown = [f['id'] for f in files if f['kind'] == 'unknown' and not f['marked']]
    return {'duplicates': duplicates, 'pairs': pairs, 'same_time': same_time, 'overlap': overlap,
            'unmarked': unmarked, 'unknown': unknown, 'pair_members': sorted(pair_members)}


def folder_tree(root, folders):
    tree = []
    for path, count in folders:
        rel = rel_of(root, path)
        parts = Path(rel).parts
        parsed = parse_folder(parts[-1])
        context = folder_context(list(parts))
        day = context['day']
        tree.append({'rel': rel, 'name': parts[-1], 'depth': len(parts) - 1, 'videos': count,
                     'level': parsed['level'] if parsed else '',
                     'date': (f"{day[0] or '????'}-{day[1]:02d}-{day[2]:02d}" if day else
                              f"{context['year']}-{context['month']:02d}" if context['year'] and context['month'] and parsed and parsed['level'] == 'month' else
                              str(context['year']) if parsed and parsed['level'] == 'year' else ''),
                     'desc': parsed['desc'] if parsed else ''})
    return tree


def verify_duplicate(root, rels):
    root = Path(root)
    hashes = {rel: full_hash(root / rel) for rel in rels}
    return {'identical': len(set(hashes.values())) == 1, 'hashes': hashes}


def marked_name(name):
    path = Path(name)
    return f'{path.stem}_ORG{path.suffix}'


def mark_originals(root, rels):
    """Append _ORG to each original (and its same-stem LRF/THM/SCR), logging for undo."""
    root = Path(root).resolve(strict=True)
    batch, done, skipped = uuid.uuid4().hex, [], []
    manifests = {}
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith('.')]
        for name in files:
            if name.lower().endswith('.dji.json'):
                manifests.setdefault(None, []).append(Path(current) / name)
    sources = {}
    for manifest in manifests.get(None, []):
        try:
            record = json.loads(manifest.read_text(encoding='utf8'))
            sources.setdefault(re.split(r'[\\/]', str(record.get('source', '')))[-1].lower(), []).append(manifest)
        except (OSError, ValueError):
            pass
    log_path = root / STATE_DIR / RENAME_LOG
    log_path.parent.mkdir(exist_ok=True)
    with log_path.open('a', encoding='utf8') as log:
        for rel in rels:
            path = (root / rel).resolve()
            if root not in path.parents or not path.is_file():
                skipped.append(f'{rel}：文件不存在'); continue
            if classify_tag(path.stem):
                skipped.append(f'{rel}：已经有标记'); continue
            moves = [(path, path.with_name(marked_name(path.name)))]
            for sidecar in path.parent.iterdir():
                if sidecar != path and sidecar.stem.lower() == path.stem.lower() and sidecar.suffix.lower() in SIDECAR_EXTENSIONS:
                    moves.append((sidecar, sidecar.with_name(marked_name(sidecar.name))))
            if any(target.exists() for _, target in moves):
                skipped.append(f'{rel}：目标文件名已存在'); continue
            for old, new in moves:
                os.rename(old, new)
                log.write(json.dumps({'batch': batch, 'old': rel_of(root, old), 'new': rel_of(root, new),
                                      'at': dt.datetime.now().isoformat(timespec='seconds')}, ensure_ascii=False) + '\n')
            log.flush()
            for manifest in sources.get(path.name.lower(), []):
                # Keep compressor records pointing at the renamed source so 匹配删除 still finds it.
                try:
                    record = json.loads(manifest.read_text(encoding='utf8'))
                    old_source = str(record['source'])
                    record['source'] = old_source[: len(old_source) - len(path.name)] + moves[0][1].name
                    record.setdefault('renamed_sources', []).append(old_source)
                    c.save_record(manifest, record)
                except (OSError, ValueError, KeyError):
                    skipped.append(f'{rel}：已改名，但未能更新压缩记录 {manifest.name}')
            done.append(rel_of(root, moves[0][1]))
    return {'batch': batch, 'renamed': done, 'skipped': skipped}


def undo_last_mark(root):
    root = Path(root).resolve(strict=True)
    log_path = root / STATE_DIR / RENAME_LOG
    try:
        entries = [json.loads(line) for line in log_path.read_text(encoding='utf8').splitlines() if line.strip()]
    except OSError:
        raise ValueError('没有可撤销的改名记录')
    entries = [e for e in entries if not e.get('undone')]
    if not entries:
        raise ValueError('没有可撤销的改名记录')
    batch = entries[-1]['batch']
    restored, problems = [], []
    for entry in reversed([e for e in entries if e['batch'] == batch]):
        new, old = root / entry['new'], root / entry['old']
        if new.is_file() and not old.exists():
            os.rename(new, old); restored.append(entry['old'])
        else:
            problems.append(entry['new'])
    with log_path.open('a', encoding='utf8') as log:
        log.write(json.dumps({'batch': batch, 'undone': True, 'at': dt.datetime.now().isoformat(timespec='seconds')}) + '\n')
    # Manifests were pointed at the new names; point them back.
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith('.')]
        for name in files:
            if not name.lower().endswith('.dji.json'):
                continue
            manifest = Path(current) / name
            try:
                record = json.loads(manifest.read_text(encoding='utf8'))
            except (OSError, ValueError):
                continue
            if record.get('renamed_sources') and Path(re.split(r'[\\/]', record['source'])[-1]).stem.endswith('_ORG'):
                record['source'] = record['renamed_sources'].pop()
                c.save_record(manifest, record)
    return {'restored': restored, 'problems': problems}


def export_page(result, template):
    """Write a self-contained calendar page to the drive root, using relative links."""
    root = Path(result['root'])
    data = json.dumps(result, ensure_ascii=False).replace('</', '<\\/')
    html = template.replace('__TOKEN__', '').replace('/*__MODE__*/', "const MODE='static';").replace(
        '<script id="embedded" type="application/json"></script>',
        f'<script id="embedded" type="application/json">{data}</script>')
    target = root / EXPORT_NAME
    target.write_text(html, encoding='utf8')
    return str(target)


def start_scan(root):
    with _lock:
        if job['running']:
            raise ValueError('已有扫描在进行')
        job.update(running=True, cancel=threading.Event(), progress={'phase': '准备', 'done': 0, 'total': 0},
                   result=None, error=None, root=str(root))
    cancel = job['cancel']

    def run():
        try:
            result = scan(root, cancel)
            with _lock:
                job['result'] = result
        except Exception as error:
            with _lock:
                job['error'] = str(error)
        finally:
            with _lock:
                job['running'] = False
    threading.Thread(target=run, daemon=True).start()


def state(include_result=False):
    with _lock:
        snapshot = {'running': job['running'], 'progress': dict(job['progress']), 'error': job['error'],
                    'root': job['root'], 'has_result': job['result'] is not None}
        if include_result:
            snapshot['result'] = job['result']
    return snapshot
