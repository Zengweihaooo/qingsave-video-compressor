"""Separate date folders: originals and LRF in *_ORG, compressed media in *_CMP.
Unknown files stay in place; journalled moves never overwrite a destination.
"""
import datetime as dt
import json
import os
import re
from pathlib import Path

TAGS = ('ORG', 'CMP')
TAG_RE = re.compile(r'[_ ](ORG|CMP)$', re.I)
DATE_RE = re.compile(r'^(\d{1,2})\.(\d{1,2})(?!\d)')
VIDEO_EXT = {'.mp4', '.mov', '.osv', '.insv', '.mkv', '.m4v', '.avi', '.mts', '.360'}
CMP_RE = re.compile(r'(?:^|[_. -])(cmp|compressed)(?:$|[_. -])', re.I)
ORG_RE = re.compile(r'(?:^|[_. -])(org|original)(?:$|[_. -])', re.I)
CAMERA_RE = re.compile(r'^(CAM|DJI)_\d', re.I)  # straight from the camera, never compressed by 轻存
LRF_RE = re.compile(r'(?:^|[_. -])lrf$', re.I)
BUSY_MARKERS = ('.partial.mp4', '.qingsave-return')


def strip_tag(name):
    return TAG_RE.sub('', name)


def kind(name):
    """'org' / 'cmp' / 'proxy' / None (not a video, or cannot tell from the name)."""
    path = Path(name)
    if name.startswith('.') or path.suffix.lower() not in VIDEO_EXT:
        return 'proxy' if path.suffix.lower() == '.lrf' else None
    if CMP_RE.search(path.stem):
        return 'cmp'
    if LRF_RE.search(path.stem):
        return 'proxy'
    if ORG_RE.search(path.stem) or CAMERA_RE.match(path.stem):
        return 'org'
    return 'other'


def inspect(folder):
    """Counts of what the folder holds and the tag it should carry."""
    counts = {'org': 0, 'cmp': 0, 'other': 0, 'proxy': 0}
    writing = False
    for entry in os.scandir(folder):
        if not entry.is_file():
            continue
        if entry.name.endswith(BUSY_MARKERS):
            writing = True
        k = kind(entry.name)
        if k:
            counts[k] += 1
    main = counts['org'] + counts['cmp'] + counts['other']
    tag = 'ORG' if main and counts['org'] == main else 'CMP' if main and counts['cmp'] == main else ''
    return counts, tag, writing


def destination(folder, tag):
    """A date's originals/proxies and compressed copies always use separate siblings."""
    folder = Path(folder)
    if folder.name in TAGS:
        return folder.with_name(tag)
    if DATE_RE.match(folder.name):
        return folder.with_name(f'{strip_tag(folder.name)}_{tag}')
    return folder / tag


def plan(roots, locked=()):
    """Move recognised media and their sidecars; leave unknown files untouched."""
    locked = [os.path.normcase(os.path.abspath(p)) for p in locked]
    changes, skipped = [], []
    for root in roots:
        root = Path(root)
        try:
            children = sorted(root.iterdir())
        except OSError:
            continue
        for folder in children:
            if not folder.is_dir() or folder.is_symlink() or not DATE_RE.match(folder.name):
                continue
            entries = sorted(folder.iterdir())
            prefix = os.path.normcase(str(folder.absolute())) + os.sep
            busy = any(p.name.endswith(BUSY_MARKERS) for p in entries)
            busy |= any(p.startswith(prefix) for p in locked)
            for path in entries:
                if not path.is_file() or path.is_symlink() or path.name.startswith('.'):
                    continue
                k = kind(path.name)
                if k not in ('org', 'cmp', 'proxy'):
                    continue
                target = destination(folder, 'CMP' if k == 'cmp' else 'ORG') / path.name
                if target == path:
                    continue
                pairs = [(path, target)]
                # Compression records belong to their video, including the source marker.
                pairs += [(side, target.with_name(target.name + side.name[len(path.name):]))
                          for side in entries if side.is_file() and not side.is_symlink()
                          and side.name.startswith(path.name + '.')]
                pairs += [(folder / ('._' + src.name), dst.with_name('._' + dst.name))
                          for src, dst in list(pairs) if (folder / ('._' + src.name)).is_file()]
                item = {'root': str(root), 'path': str(path), 'from': str(path.relative_to(root)),
                        'to': str(target.relative_to(root)), 'moves': [[str(x.relative_to(root)), str(y.relative_to(root))] for x, y in pairs]}
                target_prefix = os.path.normcase(str(target.parent.absolute())) + os.sep
                if busy or any(p.startswith(target_prefix) for p in locked):
                    skipped.append({**item, 'reason': '轻存还在用这个文件夹'})
                elif any(y.exists() for x, y in pairs):
                    skipped.append({**item, 'reason': '目标已有同名文件，保留两份不覆盖'})
                else:
                    changes.append(item)
    # Two old folders may contain the same filename: never let either overwrite the other.
    targets = {}
    safe = []
    for item in changes:
        dests = [str(Path(item['root']) / y) for x, y in item['moves']]
        if any(y in targets for y in dests):
            skipped.append({**item, 'reason': '多个文件要移到同一位置，保留原文件'})
        else:
            targets.update(dict.fromkeys(dests, True))
            safe.append(item)
    return safe, skipped


def update_records(root, moves):
    """Update paths in compression records, including records made on the other OS."""
    root = Path(root)
    problems = []
    for folder in root.iterdir():
        if not folder.is_dir() or not DATE_RE.match(folder.name):
            continue
        for manifest in folder.glob('*.dji.json'):
            if manifest.name.startswith('.'):
                continue
            try:
                record = json.loads(manifest.read_text(encoding='utf8'))
                changed = False
                for key in ('source', 'output'):
                    value = record.get(key)
                    if not isinstance(value, str):
                        continue
                    for before, after in moves:
                        for sep in ('/', chr(92)):
                            tail = before.replace('/', sep)
                            if value.endswith(sep + tail):
                                record[key] = value[:-len(tail)] + after.replace('/', sep)
                                changed = True
                                break
                        if record.get(key) != value:
                            break
                if changed:
                    tmp = manifest.with_name(manifest.name + '.tmp')
                    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf8')
                    os.replace(tmp, manifest)
            except (OSError, ValueError) as error:
                problems.append({'from': str(manifest), 'to': str(manifest), 'reason': f'压缩记录未更新：{error}'})
    return problems


def apply(changes):
    done, failed = [], []
    for item in changes:
        root = Path(item['root'])
        moved = []
        try:
            pairs = [(root / x, root / y) for x, y in item['moves']]
            if any(y.exists() for x, y in pairs):
                raise FileExistsError('目标已有同名文件，未覆盖')
            log = root / '.qingsave' / 'folder-tags-log.jsonl'
            log.parent.mkdir(exist_ok=True)
            # Journal before touching media; a partial operation can still be undone.
            with log.open('a', encoding='utf8') as f:
                f.write(json.dumps({'at': dt.datetime.now().isoformat(timespec='seconds'),
                                    'from': item['from'], 'to': item['to'], 'moves': item['moves']}, ensure_ascii=False) + '\n')
                f.flush()
                os.fsync(f.fileno())
            for source, target in pairs:
                # macOS on exFAT may move AppleDouble alongside the main file itself.
                if source.name.startswith('._') and not source.exists() and target.is_file():
                    continue
                target.parent.mkdir(exist_ok=True)
                if target.exists():
                    raise FileExistsError('目标已有同名文件，未覆盖')
                source.rename(target)
                moved.append((source, target))
        except OSError as error:
            for source, target in reversed(moved):
                if target.exists() and not source.exists():
                    target.rename(source)
            failed.append({**item, 'reason': str(error)})
            continue
        done.append(item)
    # Only remove genuinely empty source folders, never unknown files or subdirectories.
    for item in done:
        try:
            Path(item['path']).parent.rmdir()
        except OSError:
            pass
    for root in {item['root'] for item in done}:
        moves = [pair for item in done if item['root'] == root for pair in item['moves']]
        failed.extend(update_records(root, moves))
    return done, failed


def undo(root):
    """Undo logged media moves and legacy folder renames without replacing files."""
    root = Path(root)
    log = root / '.qingsave' / 'folder-tags-log.jsonl'
    try:
        entries = [json.loads(line) for line in log.read_text(encoding='utf8').splitlines() if line.strip()]
    except (OSError, ValueError):
        return []
    restored, remaining = [], []
    for entry in reversed(entries):
        pairs = entry.get('moves', [[entry['from'], entry['to']]])
        unresolved = []
        for before, after in reversed(pairs):
            current, original = root / after, root / before
            if current.exists():
                if original.exists():
                    unresolved.append([before, after])
                    continue
                try:
                    original.parent.mkdir(exist_ok=True)
                    current.rename(original)
                except OSError:
                    unresolved.append([before, after])
        if unresolved:
            remaining.append({**entry, 'moves': list(reversed(unresolved))})
        else:
            restored.append(entry)
        if 'moves' in entry:
            for before, after in pairs:
                try: (root / after).parent.rmdir()
                except OSError: pass
    update_records(root, [[y, x] for e in restored for x, y in e.get('moves', [])])
    log.write_text(''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in reversed(remaining)), encoding='utf8')
    return restored
