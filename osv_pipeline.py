"""OSV export adapters. A stitched file is never considered the original OSV."""
import copy
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import types
from contextlib import contextmanager
import compressor as c

BACKENDS = ('openosv', 'dji')
SIZES = {'4K': (3840, 1920), '6K': (6000, 3000), '8K': (7680, 3840)}


def settings(data):
    result = {'backend': data.get('backend', 'openosv'),
              'resolution': data.get('resolution', '6K'),
              'bitrate': data.get('bitrate', 'high'),
              'stabilization': data.get('stabilization', 'horizon'),
              'temp_root': str(data.get('temp_root', '')).strip()}
    if result['backend'] not in BACKENDS or result['resolution'] not in SIZES:
        raise ValueError('无效的 OSV 导出引擎或分辨率')
    if result['bitrate'] not in ('low', 'recommended', 'high'):
        raise ValueError('无效的 DJI 导出码率')
    if result['stabilization'] not in ('off', 'horizon', 'smooth-horizon'):
        raise ValueError('无效的防抖模式')
    return result


def tool():
    if os.environ.get('QINGSAVE_OSVTOOL'):
        return Path(os.environ['QINGSAVE_OSVTOOL']).expanduser().resolve()
    return c.BASE / 'vendor' / 'openosv' / ('osvtool.exe' if os.name == 'nt' else 'osvtool')


def fps(info):
    n, d = map(float, c.video(info)['avg_frame_rate'].split('/'))
    return n / d


def validate_panorama(source_info, output, cancel, expected_size=None, on_progress=None):
    """Check output projection metadata, streams and timeline, then decode every frame."""
    info = c.probe(output, cancel)
    streams = [s for s in info['streams'] if s.get('codec_type') == 'video'
               and not s.get('disposition', {}).get('attached_pic')]
    if len(streams) != 1:
        raise ValueError('全景成品必须只有一个主视频轨道')
    v = streams[0]
    if v['width'] != 2 * v['height']:
        raise ValueError('全景成品不是完整 2:1 画面')
    if expected_size and tuple(expected_size) != (v['width'], v['height']):
        raise ValueError('全景导出分辨率不符合设置')
    spherical = [s for s in v.get('side_data_list', []) if 'spherical' in str(s).lower()]
    if not spherical or not any('equirectangular' in str(s).lower() for s in spherical):
        raise ValueError('全景成品缺少等距柱状投影标记')
    # OSV stores two square fisheyes. Their dimensions must not be compared to the sphere.
    expected = copy.deepcopy(source_info)
    primary = c.video(expected)
    primary.update(width=v['width'], height=v['height'])
    expected['streams'] = [primary] + [s for s in expected['streams'] if s.get('codec_type') != 'video']
    c.validate(expected, output, cancel, on_progress)
    return info


def run_export(command, owner, duration, timeout=24*3600, cwd=None):
    """Drain logs continuously and terminate the owned process tree when cancelled."""
    kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
    process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
    lines = queue.Queue()
    def reader():
        for raw in iter(process.stdout.readline, b''):
            lines.put(raw.decode('utf8', errors='replace').rstrip())
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    recent = []
    start = time.monotonic()
    try:
        while process.poll() is None or not lines.empty() or thread.is_alive():
            if owner.cancel.is_set():
                raise RuntimeError('已取消全景导出，原片保留')
            if time.monotonic()-start > timeout:
                raise TimeoutError('全景导出超时，原片保留')
            try: line = lines.get(timeout=0.1)
            except queue.Empty: continue
            recent.append(line)
            recent = recent[-40:]
            progress = re.search(r'(\d+)/(\d+) frames', line)
            if progress and int(progress[2]):
                c.update_progress(owner, current_seconds=duration * int(progress[1]) / int(progress[2]))
            else:
                owner.events.put('[全景导出] ' + line[:1000])
        if process.returncode:
            raise RuntimeError('全景导出失败：' + '\n'.join(recent[-8:]))
    finally:
        if process.poll() is None:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                import signal
                os.killpg(process.pid, signal.SIGTERM)
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
        thread.join(timeout=2)
        process.stdout.close()


def ascii_ffmpeg():
    """Upstream Windows renderer uses an ANSI command line for its ffmpeg child."""
    original = Path(c.executable('ffmpeg'))
    try:
        str(original).encode('ascii')
        return str(original)
    except UnicodeEncodeError:
        cache = Path(tempfile.gettempdir()) / 'qingsave-osv-tools'
        cache.mkdir(exist_ok=True)
        str(cache).encode('ascii')  # Refuse unsafe lossy paths rather than silently mislaunch.
        dest = cache / original.name
        if not dest.exists() or dest.stat().st_size != original.stat().st_size:
            shutil.copy2(original, dest)
        return str(dest)


def openosv_export(source, target, owner, opts, info):
    if not tool().is_file(): raise ValueError('未安装 OpenOSV 命令行运行库')
    codec = next(iter(c.candidates(getattr(owner, 'encoder_choice', '自动选择'))))
    width, height = SIZES[opts['resolution']]
    command = [str(tool()), 'render', str(source), '--all', '--mode', 'equirect',
               '--size', f'{width}x{height}', '--color', '709', '--stab', opts['stabilization'],
               '--codec', codec, '--crf', '16', '--ffmpeg', ascii_ffmpeg(), '--out', str(target)]
    run_export(command, owner, float(info['format']['duration']))


@contextmanager
def export_workspace(source, output, opts, owner):
    """Keep intermediates on the chosen output drive and stage Unicode input by hardlink."""
    root = Path(opts['temp_root']).expanduser().resolve(strict=True) if opts['temp_root'] else Path(output.anchor)/'.qingsave-osv-work'
    if not root.is_dir() and opts['temp_root']:
        raise ValueError('临时位置不是文件夹')
    if os.name == 'nt' and not str(root).isascii():
        raise ValueError('OpenOSV 的临时目录目前需使用英文路径，例如 H:\\OSV_Temp；成品目录可使用中文')
    root.mkdir(parents=True, exist_ok=True)
    if not opts['temp_root']: c.hidden_file(root)
    required = source.stat().st_size * 2 + 256*1024*1024
    if shutil.disk_usage(root).free < required:
        raise ValueError(f'临时盘空间不足：本片预留约 {required/1024**3:.1f} GiB，请选择其他临时盘')
    with tempfile.TemporaryDirectory(prefix='job-', dir=root) as directory:
        directory = Path(directory)
        staged_source = source
        if os.name == 'nt' and not str(source).isascii():
            staged_source = directory/'source.OSV'
            try:
                os.link(source, staged_source)
            except OSError:
                if shutil.disk_usage(root).free < required + source.stat().st_size:
                    raise ValueError('中文路径需要临时副本，但临时盘空间不足；请选择与原片相同的临时盘')
                owner.events.put('正在为中文路径建立临时副本…')
                with source.open('rb') as inp, staged_source.open('xb') as out:
                    while block := inp.read(4*1024*1024):
                        if owner.cancel.is_set(): raise RuntimeError('已取消临时素材准备')
                        out.write(block)
        yield staged_source, directory/'panorama.mp4'


def dji_export(source, target, owner, opts, info):
    # Local adapter is separate from the rendering engine, allowing UI-specific fixes.
    adapter = c.BASE / 'dji_studio_export.py'
    if not adapter.is_file(): raise ValueError('DJI Studio 自动化适配器尚未安装')
    import sys
    command = [sys.executable, str(adapter), '--input', str(source), '--output', str(target),
               '--resolution', opts['resolution'], '--bitrate', opts['bitrate'],
               '--fps', str(fps(info))]
    run_export(command, owner, float(info['format']['duration']))


def publish(candidate, output, record, owner):
    """Publish only fully checked output; remove our own partial copy if interrupted."""
    if owner.cancel.is_set(): raise RuntimeError('已取消，原片保留')
    created = False
    manifest = output.with_suffix('.mp4.dji.json')
    if manifest.exists(): raise FileExistsError('目标匹配记录已经存在')
    try:
        try:
            os.link(candidate, output)
            created = True
        except FileExistsError:
            raise
        except OSError:
            with output.open('xb') as out:
                created = True
                with candidate.open('rb') as inp:
                    while block := inp.read(4*1024*1024):
                        if owner.cancel.is_set(): raise RuntimeError('已取消成品保存')
                        out.write(block)
        if c.digest(output, owner.cancel) != record['output_sha256']:
            raise ValueError('成品保存后哈希不一致')
        record.update(output=str(output), output_state=c.file_state(output))
        c.save_record(manifest, record)
    except Exception:
        if created: output.unlink(missing_ok=True)
        raise
    return manifest


def process(owner, source, output, config, options):
    """Export -> verify -> existing compressor -> final OSV-to-MP4 manifest."""
    opts = settings(options)
    if source.suffix.lower() != '.osv': raise ValueError('仅支持 OSV 原片')
    if output.exists(): raise FileExistsError('目标文件已存在')
    info = c.probe(source, owner.cancel)
    source_state = c.file_state(source)
    source_hash = c.digest(source, owner.cancel)
    duration = float(info['format']['duration'])
    owner.encoder_choice = config[2]
    c.update_progress(owner, file=source.name, duration=duration, current_seconds=0,
                      phase='OpenOSV 拼接导出' if opts['backend'] == 'openosv' else 'DJI Studio 拼接导出')
    with export_workspace(source, output, opts, owner) as (export_source, intermediate):
        exporter = openosv_export if opts['backend'] == 'openosv' else dji_export
        exporter(export_source, intermediate, owner, opts, info)
        c.update_progress(owner, phase='校验全景导出', current_seconds=0)
        validate_panorama(info, intermediate, owner.cancel, SIZES[opts['resolution']],
                          lambda seconds: c.update_progress(owner, current_seconds=seconds))
        # Keep temporary manifests private until provenance is rewritten to the OSV.
        child = types.SimpleNamespace(events=owner.events, cancel=owner.cancel, manifests=[],
                                      progress=owner.progress, progress_lock=owner.progress_lock,
                                      display_source_name=source.name)
        candidate = intermediate.with_name('compressed.mp4')
        c.App.compress(child, [(intermediate, candidate)], (config[0], config[1], config[2], False, True))
        candidate_manifest = candidate.with_suffix('.mp4.dji.json')
        if owner.cancel.is_set(): raise RuntimeError('已取消，OSV 原片保留')
        if not child.manifests: raise RuntimeError('全景导出完成，但后续压缩未成功；OSV 原片保留')
        record = json.loads(candidate_manifest.read_text(encoding='utf8'))
        # Withhold final verification until the original is stable and final output checks pass.
        record.update(source=str(source), source_sha256=source_hash, source_state=source_state,
                      verified=False, osv_export=opts, version=3, video_copy=False)
        c.save_record(candidate_manifest, record)
        if c.file_state(source) != source_state or c.digest(source, owner.cancel) != source_hash:
            raise ValueError('处理期间 OSV 原片发生变化，拒绝标记完成')
        c.update_progress(owner, phase='核验最终全景成品', current_seconds=0, duration=duration)
        validate_panorama(info, candidate, owner.cancel, SIZES[opts['resolution']],
                          lambda seconds: c.update_progress(owner, current_seconds=seconds))
        record.update(verified=True, output_sha256=c.digest(candidate, owner.cancel))
        manifest = publish(candidate, output, record, owner)
        if getattr(owner, 'write_source_marker', True):
            try: c.save_record(source.with_suffix(source.suffix+'.dji.json'), record)
            except OSError as error: owner.events.put('原片旁标记写入失败：'+str(error))
        owner.manifests.append(str(manifest))
        owner.events.put(f'OSV 原片到成品：节省 {1-output.stat().st_size/source_state["size"]:.1%}')
        if config[3]:
            owner.events.put(c.recycle(record, manifest, owner.cancel,
                                      'permanent' if config[3] == 'permanent' else 'trash'))
        c.update_progress(owner, phase='全景成品已验证', current_seconds=duration)
