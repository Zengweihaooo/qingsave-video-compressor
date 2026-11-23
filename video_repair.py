"""Read-only MP4 diagnosis and copy-only recovery. Never deletes originals.

A valid trailing moov with a short mdat length is repairable without re-encoding.
Decoded candidates with errors remain explicitly partial, never verified sources.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import threading
import time
import uuid

import compressor as c
import applog

TAIL_LIMIT = 64 * 1024 * 1024
job = {'running': False, 'phase': '', 'current': '', 'results': [], 'error': ''}
_previous = applog.load_json('repair-last.json', {})
if isinstance(_previous, dict) and isinstance(_previous.get('results'), list):
    job.update(_previous)
    if job.get('running'):
        job.update(running=False, phase='上次修复中断', error='原片保留；未完成的副本不能当作已验证结果')
_guard = threading.Lock()
_cancel = threading.Event()


def state():
    with _guard:
        return json.loads(json.dumps(job))


def check_cancel(cancel):
    if cancel and cancel.is_set():
        raise InterruptedError('已停止；原文件保留')


def box(f, pos, end):
    f.seek(pos)
    h = f.read(8)
    if len(h) != 8:
        return None
    size, kind = struct.unpack('>I4s', h)
    width = 8
    if size == 1:
        h = f.read(8)
        if len(h) != 8: return None
        size = struct.unpack('>Q', h)[0]
        width = 16
    elif size == 0:
        size = end - pos
    if size < width or pos + size > end:
        return None
    return {'offset': pos, 'size': size, 'kind': kind.decode('latin1'), 'width': width}


def valid_moov(f, pos, end):
    outer = box(f, pos, end)
    if not outer or outer['kind'] != 'moov': return None
    cursor = pos + outer['width']
    kinds = []
    while cursor < pos + outer['size']:
        child = box(f, cursor, pos + outer['size'])
        if not child: return None
        kinds.append(child['kind'])
        cursor += child['size']
    return outer if 'mvhd' in kinds and 'trak' in kinds else None


def inspect(path):
    path = Path(path).resolve(strict=True)
    if not path.is_file() or path.suffix.lower() not in ('.mp4', '.mov', '.m4v'):
        raise ValueError('请选择 MP4 / MOV 文件')
    size = path.stat().st_size
    result = {'source': str(path), 'size': size, 'mtime_ns': path.stat().st_mtime_ns,
              'status': 'unsupported', 'message': '没有找到可直接修复的完整索引；可保留原片，另用同相机参考片尝试 untrunc'}
    with path.open('rb') as f:
        pos = 0
        mdat = None
        for _ in range(10000):
            item = box(f, pos, size)
            if not item: break
            if item['kind'] == 'mdat':
                mdat = item
                break
            pos += item['size']
        if not mdat: return result
        if mdat['size'] == mdat['width']:
            return {**result, 'status': 'empty', 'message': '媒体数据区为空；索引或缩略图不能恢复成原视频'}
        f.seek(max(0, size - TAIL_LIMIT))
        start = f.tell()
        tail = f.read()
        idx = len(tail)
        moov = None
        while True:
            idx = tail.rfind(b'moov', 0, idx)
            if idx < 4: break
            candidate = start + idx - 4
            if candidate >= mdat['offset'] + mdat['size']:
                moov = valid_moov(f, candidate, size)
                if moov: break
        if not moov: return result
        expected = moov['offset'] - mdat['offset']
        if expected == mdat['size']:
            return {**result, 'status': 'unchanged', 'message': '媒体区长度与索引位置一致，无需这项修复'}
        if mdat['width'] == 8 and expected > 0xffffffff:
            return result
        return {**result, 'status': 'repairable', 'message': '找到完整索引，可在副本中纠正媒体区长度；仍需完整检查画面和声音',
                'patch_offset': mdat['offset'] + (8 if mdat['width'] == 16 else 0),
                'patch_hex': struct.pack('>Q' if mdat['width'] == 16 else '>I', expected).hex(),
                'mdat': mdat, 'moov': moov}


def digest(path, cancel=None):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        while True:
            check_cancel(cancel)
            chunk = f.read(4 * 1024 * 1024)
            if not chunk: return h.hexdigest()
            h.update(chunk)


def write_json(path, value):
    with Path(path).open('x', encoding='utf8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())


def recover(source, output_dir, cancel=None, progress=None):
    """Structural repair + complete decode. Files are exclusive, source is never written."""
    plan = inspect(source)
    if plan['status'] != 'repairable': return plan
    source = Path(plan['source'])
    output_dir = Path(output_dir).resolve()
    if output_dir == source.parent or source.parent in output_dir.parents:
        raise ValueError('修复结果请放在原片文件夹以外的独立文件夹')
    output_dir.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(output_dir).free < plan['size'] + 64 * 1024 * 1024:
        raise ValueError('修复结果所在硬盘空间不足')
    uid = uuid.uuid4().hex[:12]
    stem = source.stem + '_RECOVERED_' + uid
    partial = output_dir / ('.' + stem + '.part')
    log_path = output_dir / (stem + '.decode.log')
    def phase(value):
        if progress: progress(value)
    source_hash = hashlib.sha256()
    expected_hash = hashlib.sha256()
    patch = bytes.fromhex(plan['patch_hex'])
    offset = plan['patch_offset']
    try:
        phase('复制并纠正文件头')
        with source.open('rb') as inp, partial.open('xb') as out:
            cursor = 0
            while True:
                check_cancel(cancel)
                b = inp.read(4 * 1024 * 1024)
                if not b: break
                source_hash.update(b)
                left, right = max(cursor, offset), min(cursor + len(b), offset + len(patch))
                if left < right:
                    b = b[:left-cursor] + patch[left-offset:right-offset] + b[right-cursor:]
                expected_hash.update(b)
                out.write(b)
                cursor += len(b)
            out.flush()
            os.fsync(out.fileno())
        check_cancel(cancel)
        phase('读回校验修复副本')
        if digest(partial, cancel) != expected_hash.hexdigest():
            raise ValueError('副本读回校验不一致')
        info = c.probe(partial, cancel)
        stream = c.video(info)
        duration = float(stream.get('duration') or info.get('format', {}).get('duration') or 0)
        if duration <= 0: raise ValueError('修复副本仍没有有效时长')
        phase('完整检查画面和声音')
        with log_path.open('xb') as errors:
            proc = subprocess.Popen([c.executable('ffmpeg'), '-nostdin', '-v', 'error', '-threads', '4',
                '-err_detect', 'explode', '-i', str(partial), '-map', f"0:{stream['index']}", '-map', '0:a?',
                '-progress', 'pipe:1', '-nostats', '-f', 'null', '-'],
                stdout=subprocess.PIPE, stderr=errors)
            try:
                # Drain progress in another thread so cancellation never waits on a pipe read.
                counters = {}
                def drain():
                    for line in proc.stdout:
                        key, _, value = line.decode(errors='replace').strip().partition('=')
                        counters[key] = value
                reader = threading.Thread(target=drain, daemon=True)
                reader.start()
                while proc.poll() is None:
                    check_cancel(cancel)
                    if progress: progress('完整检查画面和声音', counters.copy())
                    time.sleep(.2)
                reader.join()
                proc.stdout.close()
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try: proc.wait(timeout=5)
                    except subprocess.TimeoutExpired: proc.kill(); proc.wait()
        check_cancel(cancel)
        frames = int(counters.get('frame') or 0)
        expected_frames = int(stream.get('nb_frames') or 0)
        verified = proc.returncode == 0 and log_path.stat().st_size == 0 and frames > 0 and (not expected_frames or frames == expected_frames)
        status = 'verified_playable' if verified else 'partial'
        result_path = output_dir / (stem + ('' if verified else '_PARTIAL') + '.mp4')
        # A random exclusive reservation prevents replacing an existing recovery.
        with result_path.open('xb'): pass
        try: os.replace(partial, result_path)
        except BaseException: result_path.unlink(missing_ok=True); raise
        unchanged = c.file_state(source) == {'size': plan['size'], 'mtime_ns': plan['mtime_ns']}
        if not unchanged: status = 'source_changed'
        result = {**plan, 'status': status, 'output': str(result_path), 'decode_log': str(log_path),
            'source_sha256': source_hash.hexdigest(), 'output_sha256': expected_hash.hexdigest(),
            'source_unchanged': unchanged, 'duration': duration, 'width': stream.get('width'), 'height': stream.get('height'),
            'decoded_frames': frames, 'indexed_frames': expected_frames, 'decode_exit': proc.returncode,
            'decode_error_bytes': log_path.stat().st_size,
            'at': dt.datetime.now().astimezone().isoformat(), 'original_deleted': False,
            'message': '副本完整解码通过；不能据此证明录制本身没有缺失，原片保留' if verified else '已恢复索引，但画面或声音有错误；只作部分恢复，原片保留'}
        write_json(str(result_path) + '.repair.json', result)
        return result
    finally:
        partial.unlink(missing_ok=True)


def start(sources, output_dir):
    if not isinstance(sources, list) or not sources or len(sources) > 100:
        raise ValueError('请选择 1–100 个视频')
    paths = list(dict.fromkeys(str(Path(p).resolve(strict=True)) for p in sources))
    if not output_dir: raise ValueError('请选择独立的修复结果文件夹')
    # Fail synchronously on invalid paths; worker owns the long decoding phase.
    for path in paths: inspect(path)
    with _guard:
        if job['running']: raise ValueError('已有修复任务正在运行')
        _cancel.clear()
        job.update(running=True, phase='准备', current='', results=[], error='', progress={})
    try: applog.save_json('repair-last.json', state())
    except Exception:
        with _guard: job['running'] = False
        raise
    def run():
        report = Path(output_dir).resolve() / ('repair-batch-' + uuid.uuid4().hex + '.json')
        try:
            report.parent.mkdir(parents=True, exist_ok=True)
            for path in paths:
                check_cancel(_cancel)
                with _guard: job.update(current=path)
                def update(phase, counters=None):
                    with _guard: job.update(phase=phase, progress=counters or {})
                try: result = recover(path, output_dir, _cancel, update)
                except InterruptedError: raise
                except Exception as exc: result = {'source': path, 'status': 'failed', 'message': str(exc)}
                with _guard: job['results'].append(result)
        except Exception as exc:
            with _guard: job['error'] = str(exc)
        finally:
            with _guard: job.update(running=False, phase='已结束', report=str(report))
            try:
                write_json(report, state())
                applog.save_json('repair-last.json', state())
            except Exception as exc:
                with _guard: job['error'] += '；结果记录保存失败：' + str(exc)
    threading.Thread(target=run, daemon=True).start()
    return state()


def cancel():
    _cancel.set()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='轻存：只在独立副本中修复 MP4 索引，原片保留')
    parser.add_argument('sources', nargs='+')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    start(args.sources, args.output)
    try:
        while state()['running']: time.sleep(1)
    except KeyboardInterrupt:
        cancel()
        while state()['running']: time.sleep(.2)
    print(json.dumps(state(), ensure_ascii=False, indent=2))
    raise SystemExit(1 if state()['error'] or any(r['status'] not in ('verified_playable', 'unchanged') for r in state()['results']) else 0)
