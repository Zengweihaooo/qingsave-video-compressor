"""Verified desktop staging and persistent, space-aware return transfers."""
import json
import os
import shutil
import threading
import uuid
from pathlib import Path
import applog
import compressor as c

IO_LOCK = threading.Lock()
LOCK = threading.RLock()
STOP = threading.Event()
ENTRIES = []
RESERVE = 1024 ** 3
RESERVATIONS = {}

def desktop_root():
    if (c.BASE / '.portable-data').exists():
        return applog.DATA_DIR / 'temporary-video'
    desktop = Path.home() / 'Desktop'
    if os.name == 'nt':
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders') as key:
                desktop = Path(os.path.expandvars(winreg.QueryValueEx(key, 'Desktop')[0]))
        except OSError:
            pass
    return desktop / 'QingSave-Temporary'

def free(path):
    path = Path(path)
    while not path.exists() and path != path.parent:
        path = path.parent
    return shutil.disk_usage(path).free

def save():
    with LOCK:
        applog.save_json('temporary-storage.json', ENTRIES)

def snapshot():
    with LOCK:
        return [dict(e) for e in ENTRIES if e.get('status') != 'returned']

def volume_key(path):
    path=Path(path).resolve()
    while not path.exists() and path!=path.parent:path=path.parent
    return path.drive.lower() or str(path.stat().st_dev)

def reserve_space(path,required,token):
    with LOCK:
        volume=volume_key(path)
        reserved=sum(amount for key,(drive,amount) in RESERVATIONS.items() if drive==volume and key!=token)
        if free(path)-reserved<required:return False
        RESERVATIONS[token]=(volume,required)
        return True

def release_reservation(token):
    with LOCK:RESERVATIONS.pop(token,None)

def prepare(item, owner):
    """Reserve conservatively; wait rather than filling either disk."""
    source, target = Path(item['source']), Path(item.get('return_path') or item['output']).resolve()
    required = int(source.stat().st_size * 1.15) + RESERVE
    while not owner.cancel.is_set() and not STOP.is_set():
        if not item.get('return_path') and reserve_space(target.parent, required, item['id']):
            target.parent.mkdir(parents=True, exist_ok=True)
            return target, item['config']
        if not item.get('temporary_storage', True):
            raise ValueError('目标盘空间不足；可以开启桌面临时存储')
        root = desktop_root()
        if reserve_space(root, required, item['id']):
            staged = root / item['id'] / target.parent.name / target.name
            staged.parent.mkdir(parents=True, exist_ok=True)
            entry = {'id': item['id'], 'staged': str(staged), 'target': str(target),
                     'source': str(source), 'delete_mode': item['config'][3],
                     'status': 'encoding', 'message': '正在桌面暂存压缩'}
            with LOCK:
                old = next((e for e in ENTRIES if e['id'] == item['id']), None)
                if old is None: ENTRIES.append(entry)
                else: old.update(entry)
                save()
            item['return_path'] = str(target)
            owner.events.put(f'目标盘空间不足，暂存到桌面：{staged}；空间足够后自动移回 {target}')
            config = list(item['config']); config[3] = False
            return staged, tuple(config)
        c.update_progress(owner, phase='等待磁盘空间（桌面及目标盘空间不足）')
        owner.cancel.wait(10)
    raise RuntimeError('已停止等待磁盘空间，原片保留')

def mark_ready(task_id):
    with LOCK:
        entry = next((e for e in ENTRIES if e['id'] == task_id), None)
        if entry:
            entry.update(status='waiting', message='已完整验证，暂存在桌面，等待目标盘空间')
            save()

def _return_one(entry):
    """Copy, read back SHA-256, commit without overwrite, then clean staging."""
    if entry.get('status') == 'encoding': return False
    staged, target = Path(entry['staged']), Path(entry['target'])
    manifest = staged.with_suffix('.mp4.dji.json')
    if not manifest.is_file(): return False
    record = json.loads(manifest.read_text(encoding='utf8'))
    if not record.get('verified') or record.get('source') != entry['source']: return False
    if not staged.is_file(): return False
    expected = record['output_sha256']
    if free(target.parent) < staged.stat().st_size + RESERVE:
        entry.update(status='waiting', message='已完整验证，暂存在桌面，等待目标盘空间')
        return False
    final_manifest = target.with_suffix('.mp4.dji.json')
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name('.' + uuid.uuid4().hex + '.qingsave-return')
    entry.update(status='moving', message='正在移回目标盘并读回校验')
    save()
    try:
        if target.exists():
            prior = json.loads(final_manifest.read_text(encoding='utf8')) if final_manifest.exists() else {}
            if prior.get('output_sha256') != expected or c.digest(target, STOP) != expected:
                raise FileExistsError('目标已存在，未覆盖；桌面副本保留')
        else:
            with staged.open('rb') as inp, partial.open('xb') as out:
                while block := inp.read(4 * 1024 * 1024):
                    if STOP.is_set(): raise RuntimeError('退出时暂停移回，桌面副本保留')
                    out.write(block)
                out.flush(); os.fsync(out.fileno())
            if c.digest(partial, STOP) != expected:
                raise ValueError('移回校验不一致，桌面副本保留')
            # Windows rename refuses an existing destination; POSIX link also refuses overwrite.
            if os.name == 'nt': os.rename(partial, target)
            else: os.link(partial, target); partial.unlink()
        record.update(output=str(target), output_state=c.file_state(target))
        c.save_record(final_manifest, record)
        marker = Path(entry['source']).with_suffix(Path(entry['source']).suffix + '.dji.json')
        if Path(entry['source']).is_file(): c.save_record(marker, record)
        # Honor the original deletion choice only after the verified return succeeded.
        if entry.get('delete_mode') and not record.get('deleted') and Path(entry['source']).is_file():
            if c.digest(Path(entry['source']), STOP) != record['source_sha256']:
                raise ValueError('原片内容发生变化，原片保留')
            c.recycle(record, final_manifest, STOP, 'permanent' if entry['delete_mode'] == 'permanent' else 'trash', fast=True)
        staged.unlink()
        manifest.unlink(missing_ok=True)
        if marker.exists(): c.save_record(marker, record)
        root = desktop_root().resolve()
        parent = staged.parent.resolve()
        if root in parent.parents:
            while parent == root or root in parent.parents:
                try: parent.rmdir()
                except OSError: break
                if parent == root: break
                parent = parent.parent
        entry.update(status='returned', message='已移回并校验')
        applog.write('临时存储', f'已移回并校验：{target}')
        return True
    finally:
        partial.unlink(missing_ok=True)

def return_one(entry):
    if entry.get('status')=='encoding':return False
    staged=Path(entry['staged']);target=Path(entry['target'])
    if not staged.is_file():return False
    token='return-'+entry['id']
    if not reserve_space(target.parent,staged.stat().st_size+RESERVE,token):return False
    try:return _return_one(entry)
    finally:release_reservation(token)

def flush(on_return=None):
    if not IO_LOCK.acquire(blocking=False): return
    try:
        for entry in list(ENTRIES):
            if STOP.is_set(): break
            if entry.get('status') == 'returned': continue
            try:
                if return_one(entry) and on_return: on_return(entry)
            except Exception as exc:
                entry.update(status='waiting', message=str(exc))
                applog.write('临时存储', f"{entry['staged']}：{exc}")
            save()
    finally:
        IO_LOCK.release()

def monitor(on_return=None):
    while not STOP.is_set():
        flush(on_return)
        STOP.wait(30)

def initialize():
    with LOCK:
        ENTRIES[:] = applog.load_json('temporary-storage.json', []) or []
        for entry in ENTRIES:
            if entry.get('status') in ('encoding', 'moving'):
                entry.update(status='waiting', message='恢复暂存记录，等待校验成品或目标盘空间')
    STOP.clear()
