"""Date folders say what is inside: `10.31_ORG` holds only originals, `10.15_CMP` only compressed versions.

Mixed or unrecognised folders carry no tag (a tag that no longer fits is taken off again).  Only date folders
directly under a library root are touched, never while 轻存 is writing into them, and every rename is logged to
`<root>/.qingsave/folder-tags-log.jsonl` so it can be undone.
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
    if LRF_RE.search(path.stem):
        return 'proxy'
    if CMP_RE.search(path.stem):
        return 'cmp'
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


def plan(roots, locked=()):
    """Every date folder under `roots` whose name should change.  `locked`: paths 轻存 still needs as they are."""
    locked = [os.path.normcase(os.path.abspath(p)) for p in locked]
    changes, skipped = [], []
    for root in roots:
        root = Path(root)
        try:
            children = sorted(root.iterdir())
        except OSError:
            continue
        for folder in children:
            if not folder.is_dir() or folder.name.startswith('.') or not DATE_RE.match(folder.name):
                continue
            try:
                counts, tag, writing = inspect(folder)
            except OSError:
                continue
            base = strip_tag(folder.name)
            target = folder.with_name(f'{base}_{tag}' if tag else base)
            if target.name == folder.name:
                continue
            item = {'root': str(root), 'path': str(folder), 'from': folder.name, 'to': target.name, 'counts': counts}
            prefix = os.path.normcase(str(folder)) + os.sep
            if writing or any(p.startswith(prefix) for p in locked):
                skipped.append({**item, 'reason': '轻存还在用这个文件夹'})
            elif target.exists():
                skipped.append({**item, 'reason': f'已有同名文件夹 {target.name}'})
            else:
                changes.append(item)
    return changes, skipped


def apply(changes):
    done, failed = [], []
    for item in changes:
        folder = Path(item['path'])
        target = folder.with_name(item['to'])
        try:
            if target.exists():
                raise FileExistsError(f'已有同名文件夹 {target.name}')
            folder.rename(target)
        except OSError as error:
            failed.append({**item, 'reason': str(error)})
            continue
        log = Path(item['root']) / '.qingsave' / 'folder-tags-log.jsonl'
        try:
            log.parent.mkdir(exist_ok=True)
            with log.open('a', encoding='utf8') as f:
                f.write(json.dumps({'at': dt.datetime.now().isoformat(timespec='seconds'), 'from': item['from'], 'to': item['to']},
                                   ensure_ascii=False) + '\n')
        except OSError:
            pass
        done.append(item)
    return done, failed


def undo(root):
    """Put every folder renamed by this feature under `root` back to its untagged name, newest first."""
    log = Path(root) / '.qingsave' / 'folder-tags-log.jsonl'
    try:
        entries = [json.loads(line) for line in log.read_text(encoding='utf8').splitlines() if line.strip()]
    except (OSError, ValueError):
        return []
    restored = []
    for entry in reversed(entries):
        current, original = Path(root) / entry['to'], Path(root) / entry['from']
        if current.is_dir() and not original.exists():
            current.rename(original)
            restored.append(entry)
    log.unlink(missing_ok=True)
    return restored
