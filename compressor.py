"""DJI video compressor. Python 3.10+, FFmpeg and send2trash."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from spherical import inject
try:  # the legacy desktop window only; the web app works without Tk (portable Windows Python has none)
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except ImportError:
    tk = None

BASE = Path(__file__).resolve().parent
LEVELS = {"压缩较猛": (0.045, 64), "中等压缩": (0.075, 128), "压缩较轻": (0.115, 192), "保留原片质量": (0, 0)}
ENGINES = {"自动选择": None, "NVIDIA RTX 3080": "hevc_nvenc", "Intel 核显": "hevc_qsv", "Apple M1 / M1 Pro": "hevc_videotoolbox", "CPU（较慢）": "libx265"}
PROCESS_LOCK = threading.Lock()
ACTIVE_PROCESSES = {}

def executable(name):
    suffix = '.exe' if os.name == 'nt' else ''
    local = BASE / 'bin' / (name + suffix)
    return str(local) if local.exists() else shutil.which(name)

def run(args, cancel=None):
    kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
    if cancel:
        with PROCESS_LOCK:
            ACTIVE_PROCESSES[id(cancel)] = p
    try:
        while True:
            try:
                out, err = p.communicate(timeout=0.15)
                break
            except subprocess.TimeoutExpired:
                if cancel and cancel.is_set():
                    p.terminate()
                    try:
                        out, err = p.communicate(timeout=2)
                    except subprocess.TimeoutExpired:
                        p.kill(); out, err = p.communicate()
                    raise RuntimeError('已停止，临时文件已清理，原片保留')
    finally:
        if cancel:
            with PROCESS_LOCK:
                if ACTIVE_PROCESSES.get(id(cancel)) is p:
                    ACTIVE_PROCESSES.pop(id(cancel), None)
    if cancel and cancel.is_set():
        raise RuntimeError('已停止，临时文件已清理，原片保留')
    if p.returncode:
        raise RuntimeError(err.decode('utf-8', errors='replace')[-2400:])
    return out.decode('utf-8', errors='replace')

def _parse_clock(value):
    try:
        hours, minutes, seconds = value.split(':')
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (ValueError, AttributeError):
        return 0.0

def run_progress(args, cancel, on_progress):
    """Run FFmpeg and report encoded media time without filling pipe buffers."""
    command = args[:1] + ['-progress', 'pipe:1', '-nostats', '-stats_period', '0.5'] + args[1:]
    kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
    errors = []
    def read_output():
        for raw in iter(p.stdout.readline, b''):
            line = raw.decode('utf-8', errors='replace').strip()
            if line.startswith('out_time='):
                on_progress(_parse_clock(line.split('=', 1)[1]))
    def read_errors():
        for raw in iter(p.stderr.readline, b''):
            errors.append(raw.decode('utf-8', errors='replace'))
            if len(errors) > 200:
                del errors[:100]
    readers = [threading.Thread(target=read_output, daemon=True), threading.Thread(target=read_errors, daemon=True)]
    for reader in readers: reader.start()
    with PROCESS_LOCK:
        ACTIVE_PROCESSES[id(cancel)] = p
    try:
        while p.poll() is None:
            if cancel.is_set():
                p.terminate()
                try: p.wait(timeout=2)
                except subprocess.TimeoutExpired: p.kill(); p.wait()
                raise RuntimeError('已停止，临时文件已清理，原片保留')
            time.sleep(0.1)
    finally:
        with PROCESS_LOCK:
            if ACTIVE_PROCESSES.get(id(cancel)) is p:
                ACTIVE_PROCESSES.pop(id(cancel), None)
        for reader in readers: reader.join(timeout=1)
    if cancel.is_set():
        raise RuntimeError('已停止，临时文件已清理，原片保留')
    if p.returncode:
        raise RuntimeError(''.join(errors)[-2400:])

def update_progress(owner, **values):
    if not hasattr(owner, 'progress'):
        return
    if 'phase' in values:
        values = {'bytes_done': 0, 'bytes_total': 0, **values}
    progress_lock = getattr(owner, 'progress_lock', None)
    if progress_lock:
        with progress_lock: owner.progress.update(values)
    else:
        owner.progress.update(values)

def cancel_work(cancel):
    cancel.set()
    with PROCESS_LOCK:
        process = ACTIVE_PROCESSES.get(id(cancel))
    if process and process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass

def probe(path, cancel=None):
    return json.loads(run([executable('ffprobe'), '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)], cancel))

def video(info):
    streams = [s for s in info['streams'] if s['codec_type'] == 'video' and not s.get('disposition', {}).get('attached_pic')]
    if not streams:
        raise ValueError('没有找到可处理的主视频流')
    return max(streams, key=lambda s: (
        int(s.get('width', 0)) * int(s.get('height', 0)),
        bool(s.get('disposition', {}).get('default')),
        int(s.get('bit_rate', 0) or 0),
    ))

def file_state(path):
    stat = Path(path).stat()
    return {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}

def stamp(path):
    # Do not silently invent recording time from filesystem modification time.
    for m in re.finditer(r'(?<!\d)(20\d{6})[_-]?(\d{6})(?!\d)', Path(path).stem):
        try:
            return dt.datetime.strptime(''.join(m.groups()), '%Y%m%d%H%M%S').strftime('%Y%m%d_%H%M%S')
        except ValueError:
            pass
    raise ValueError('文件名未找到有效年月日时分秒；请保留 DJI 时间戳或先修正文件名')

def output_name(path, suffix, number, width):
    if re.search(r'[<>:"/\\|?*\x00-\x1f{}]', suffix) or suffix.endswith(('.', ' ')):
        raise ValueError('后缀含有不可用于文件名的字符')
    return f'{stamp(path)}{("_" + suffix) if suffix else ""}_{number:0{width}d}.mp4'

def digest(path, cancel=None, on_progress=None):
    h = hashlib.sha256()
    total = Path(path).stat().st_size
    done = 0
    last_report = time.monotonic()
    if on_progress: on_progress(0, total)
    with open(path, 'rb') as f:
        while block := f.read(4 * 1024 * 1024):
            if cancel and cancel.is_set():
                raise RuntimeError('已取消')
            h.update(block)
            done += len(block)
            now = time.monotonic()
            if on_progress and now - last_report >= 0.5:
                on_progress(done, total)
                last_report = now
    if cancel and cancel.is_set(): raise RuntimeError('已取消')
    if on_progress: on_progress(done, total)
    return h.hexdigest()

def digest_progress(owner, path, phase):
    update_progress(owner, phase=phase, current_seconds=0)
    return digest(path, owner.cancel, lambda done, total: update_progress(
        owner, bytes_done=done, bytes_total=total))


# Quality targets for engines with a real constant-quality VBR mode.  The bitrate
# from LEVELS stays as the peak cap (1.5x / 3x buffer), so busy scenes cannot balloon.
QUALITY = {"压缩较猛": {'libx265': 28, 'hevc_nvenc': 30},
           "中等压缩": {'libx265': 25, 'hevc_nvenc': 27},
           "压缩较轻": {'libx265': 22, 'hevc_nvenc': 24}}

ENGINE_INFO = {'hevc_nvenc': ('NVIDIA 显卡', 'GPU'), 'hevc_qsv': ('Intel 核显', 'GPU'),
               'hevc_videotoolbox': ('Apple 媒体引擎', 'GPU'), 'libx265': ('CPU（x265）', 'CPU'),
               'copy': ('直接复制视频流', '不编码')}

def short_error(error):
    lines = [line.strip() for line in str(error).splitlines() if line.strip()]
    return (lines[-1] if lines else str(error))[:180]

def decode_args(engine, tuned=True):
    # Decode on the NVIDIA card too, so the CPU is not the bottleneck; FFmpeg falls back to
    # software decoding by itself when a stream is not supported.
    return ['-hwaccel', 'cuda'] if engine == 'hevc_nvenc' and tuned else []

def encoding_args(engine, bitrate, tenbit=False, level=None, tuned=True):
    quality = QUALITY.get(level, {}).get(engine)
    cap = ['-maxrate', str(int(bitrate * 1.5)), '-bufsize', str(bitrate * 3)]
    if engine == 'hevc_nvenc':
        if quality and tuned:
            args = ['-c:v', engine, '-rc', 'vbr', '-cq', str(quality), '-b:v', '0'] + cap + [
                '-preset', 'p5', '-multipass', 'qres', '-rc-lookahead', '32',
                '-spatial-aq', '1', '-temporal-aq', '1', '-aq-strength', '8']
        else:
            args = ['-c:v', engine, '-b:v', str(bitrate)] + cap + ['-rc', 'vbr', '-preset', 'p5', '-multipass', 'qres']
    elif engine == 'libx265' and quality:
        args = ['-c:v', engine, '-crf', str(quality)] + cap + ['-preset', 'medium']
    else:
        args = ['-c:v', engine, '-b:v', str(bitrate)] + cap
        if engine == 'hevc_qsv':
            args += ['-preset', 'medium']
        elif engine == 'hevc_videotoolbox':
            args += ['-allow_sw', '0']
        else:
            args += ['-preset', 'medium']
    pix = ('yuv420p10le' if engine == 'libx265' else 'p010le') if tenbit else 'yuv420p'
    return args + ['-pix_fmt', pix, '-tag:v', 'hvc1']

def candidates(choice):
    if ENGINES[choice]:
        return [ENGINES[choice]]
    return (['hevc_videotoolbox', 'libx265'] if platform.system() == 'Darwin'
            else ['hevc_nvenc', 'hevc_qsv', 'libx265'])

def validate(src_info, target, cancel, on_progress=None):
    result = probe(target, cancel)
    a, b = video(src_info), video(result)
    if (a['width'], a['height']) != (b['width'], b['height']):
        raise ValueError('验证失败：分辨率不一致')
    def fps(stream):
        n, d = map(float, stream.get('avg_frame_rate', '0/1').split('/'))
        return n/d if d else 0
    if fps(a) and abs(fps(a)-fps(b)) > max(0.05, fps(a)*0.002):
        raise ValueError('验证失败：平均帧率不一致')
    da, db = float(src_info['format']['duration']), float(result['format']['duration'])
    if abs(da - db) > max(0.3, da * 0.001):
        raise ValueError('验证失败：时长不一致')
    if sum(s['codec_type'] == 'audio' for s in src_info['streams']) != sum(s['codec_type'] == 'audio' for s in result['streams']):
        raise ValueError('验证失败：音轨数量不一致')
    command = [executable('ffmpeg'), '-v', 'error', '-xerror', '-i', str(target), '-map', '0:v:0', '-map', '0:a?', '-f', 'null', '-']
    if on_progress: run_progress(command, cancel, on_progress)
    else: run(command, cancel)

def hidden_file(path, hidden=True):
    if os.name != 'nt': return
    import ctypes
    attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
    if attrs != 0xFFFFFFFF:
        ctypes.windll.kernel32.SetFileAttributesW(str(path), (attrs | 0x2) if hidden else (attrs & ~0x2))

def save_record(path, record):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    if path.exists(): hidden_file(path, False)
    os.replace(temp, path)
    hidden_file(path)

def recycle(record, manifest, cancel, mode='trash', fast=False, owner=None):
    if mode not in ('trash', 'permanent'): raise ValueError('无效的删除方式')
    src, dst = Path(record['source']), Path(record['output'])
    def checked_digest(path):
        return digest_progress(owner, path, '删除前核对文件内容') if owner else digest(path, cancel)
    if record.get('deleted'):
        return '此前已移入回收站'
    if not record.get('verified') or not src.is_file() or not dst.is_file() or src.resolve() == dst.resolve():
        raise ValueError('匹配失败：原片或成品不存在，或缺少验证记录')
    if fast:
        if file_state(src) != record.get('source_state') or file_state(dst) != record.get('output_state'):
            raise ValueError('文件状态已发生变化，请使用“压缩后匹配删除”进行完整校验')
    elif checked_digest(src) != record['source_sha256'] or checked_digest(dst) != record['output_sha256']:
        raise ValueError('文件已发生变化，拒绝删除')
    if record.get('panorama') and not any('spherical' in str(s).lower() for s in video(probe(dst, cancel)).get('side_data_list', [])):
        raise ValueError('全景标记缺失，拒绝删除')
    if not fast:
        if owner: update_progress(owner, phase='删除前完整播放检查', current_seconds=0)
        if record.get('osv_export'):
            from osv_pipeline import validate_panorama, SIZES
            validate_panorama(probe(src, cancel), dst, cancel, SIZES[record['osv_export']['resolution']])
        else:
            validate(probe(src, cancel), dst, cancel,
                     (lambda seconds: update_progress(owner, current_seconds=seconds)) if owner else None)
    if not record.get('video_copy') and dst.stat().st_size >= src.stat().st_size:
        raise ValueError('成品未变小，保留原片')
    if cancel.is_set():
        raise RuntimeError('已取消')
    if not fast and (checked_digest(src) != record['source_sha256'] or checked_digest(dst) != record['output_sha256']):
        raise ValueError('验证期间文件发生变化，拒绝删除')
    if mode == 'permanent':
        src.unlink()
    else:
        from send2trash import send2trash
        send2trash(str(src.resolve()))
    record['deleted'] = True
    record['deletion_mode'] = mode
    save_record(manifest, record)
    return '原片已彻底删除' if mode == 'permanent' else '原片已移入回收站'

class App(tk.Tk if tk else object):
    def __init__(self):
        super().__init__()
        self.title('DJI 视频一键压缩')
        self.geometry('1040x730')
        self.files = []
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.out = tk.StringVar(value=str(Path.home() / 'Videos' / 'DJI_Compressed'))
        self.vlevel = tk.StringVar(value='中等压缩')
        self.alevel = tk.StringVar(value='中等压缩')
        self.engine = tk.StringVar(value='自动选择')
        self.suffix = tk.StringVar(value='CMP')
        self.start = tk.StringVar(value='1')
        self.width = tk.StringVar(value='3')
        self.delete = tk.BooleanVar(value=False)
        self.pano = tk.BooleanVar(value=False)
        bar = ttk.Frame(self, padding=12); bar.pack(fill='x')
        for title, action in [('添加视频（可多选）', self.add), ('添加文件夹', self.folder), ('移除选中项', self.remove)]:
            ttk.Button(bar, text=title, command=action).pack(side='left', padx=4)
        ttk.Label(bar, text='列表内全部视频参与压缩；按路径排序编号').pack(side='left', padx=16)
        self.tree = ttk.Treeview(self, columns=('src', 'dst'), show='headings', height=10, selectmode='extended')
        self.tree.heading('src', text='原文件'); self.tree.heading('dst', text='成品命名预览')
        self.tree.column('src', width=520); self.tree.column('dst', width=440)
        self.tree.pack(fill='both', expand=True, padx=16)
        opts = ttk.Frame(self, padding=12); opts.pack(fill='x')
        for i, (label, var, values) in enumerate([('视频', self.vlevel, list(LEVELS)), ('音频', self.alevel, list(LEVELS)), ('编码器', self.engine, list(ENGINES))]):
            ttk.Label(opts, text=label).grid(row=0, column=i*2, padx=4)
            ttk.Combobox(opts, textvariable=var, values=values, state='readonly', width=20).grid(row=0, column=i*2+1)
        names = ttk.Frame(self, padding=8); names.pack(fill='x')
        for label, var, width in [('时间戳后的文字', self.suffix, 24), ('起始序号', self.start, 7), ('序号位数', self.width, 5)]:
            ttk.Label(names, text=label).pack(side='left', padx=8)
            ttk.Entry(names, textvariable=var, width=width).pack(side='left')
            var.trace_add('write', lambda *_: self.preview())
        ttk.Label(names, text='固定前缀：年月日_时分秒').pack(side='left', padx=16)
        dest = ttk.Frame(self, padding=8); dest.pack(fill='x')
        ttk.Label(dest, text='输出文件夹').pack(side='left', padx=8)
        ttk.Entry(dest, textvariable=self.out).pack(side='left', fill='x', expand=True)
        ttk.Button(dest, text='选择', command=self.output_folder).pack(side='left')
        ttk.Checkbutton(self, text='开始前勾选：成功、变小且完整验证后，将原片移入回收站', variable=self.delete).pack(anchor='w', padx=20)
        ttk.Checkbutton(self, text='本批次是已拼接的 360° 全景 MP4（完整 2:1 等距柱状投影，保留可转动视角）', variable=self.pano).pack(anchor='w', padx=20)
        ttk.Label(self, text='OSV 请先用 DJI Studio 导出全景 MP4；HDR / D-Log 请先转换为普通 SDR。').pack(anchor='w', padx=20, pady=6)
        actions = ttk.Frame(self, padding=8); actions.pack(fill='x')
        ttk.Button(actions, text='开始压缩', command=self.start_job).pack(side='left', padx=6)
        ttk.Button(actions, text='停止（保留原片）', command=self.cancel.set).pack(side='left', padx=6)
        ttk.Button(actions, text='压缩后匹配删除原片', command=self.match_delete).pack(side='left', padx=6)
        self.status = tk.StringVar(value='就绪。首次运行请按说明安装 FFmpeg 和 send2trash。')
        ttk.Label(self, textvariable=self.status).pack(anchor='w', padx=16)
        self.log = tk.Text(self, height=8, wrap='word', state='disabled'); self.log.pack(fill='x', padx=16, pady=10)
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.after(150, self.drain)

    def add(self):
        if not self.busy:
            self.files = sorted(set(self.files + list(filedialog.askopenfilenames(filetypes=[('视频', '*.mp4 *.MP4 *.mov *.MOV *.mkv *.MKV')]))))
            self.preview()

    def folder(self):
        if self.busy: return
        p = filedialog.askdirectory()
        if p:
            self.files = sorted(set(self.files + [str(f.resolve()) for f in Path(p).iterdir() if f.suffix.lower() in ('.mp4', '.mov', '.mkv')]))
            self.preview()

    def remove(self):
        if self.busy: return
        selected = {self.tree.item(i)['values'][0] for i in self.tree.selection()}
        self.files = [p for p in self.files if p not in selected]; self.preview()

    def output_folder(self):
        p = filedialog.askdirectory()
        if p: self.out.set(p)

    def settings(self):
        start, width = int(self.start.get()), int(self.width.get())
        if start < 0 or not 1 <= width <= 10: raise ValueError('序号须非负，位数须为 1–10')
        return start, width

    def preview(self):
        self.tree.delete(*self.tree.get_children())
        for i, p in enumerate(self.files):
            try:
                start, width = self.settings()
                name = output_name(p, self.suffix.get(), start+i, width)
            except ValueError as e: name = str(e)
            self.tree.insert('', 'end', values=(p, name))

    def drain(self):
        while not self.events.empty():
            msg = self.events.get()
            if msg is None:
                self.busy = False; self.status.set('任务结束；请查看下方结果。'); continue
            self.status.set(msg[:110])
            self.log.configure(state='normal'); self.log.insert('end', msg + '\n'); self.log.see('end'); self.log.configure(state='disabled')
        self.after(150, self.drain)

    def launch(self, fn):
        self.busy = True; self.cancel.clear()
        def work():
            try: fn()
            except Exception as e: self.events.put(str(e))
            finally: self.events.put(None)
        threading.Thread(target=work, daemon=True).start()

    def dependencies(self):
        if not executable('ffmpeg') or not executable('ffprobe'):
            raise ValueError('未找到 FFmpeg / ffprobe，请阅读使用说明安装，或放入程序旁 bin 文件夹。')

    def start_job(self):
        if self.busy: return
        try:
            self.dependencies()
            if not self.files: raise ValueError('请先添加视频')
            start, width = self.settings()
            dest = Path(self.out.get()).expanduser().resolve()
            jobs = [(Path(p).resolve(), dest / output_name(p, self.suffix.get(), start+i, width)) for i, p in enumerate(self.files)]
            if any(s == t or t.exists() for s, t in jobs): raise ValueError('输出文件已存在或与原片同名，请修改序号或输出目录')
            delete = self.delete.get()
            if delete:
                import send2trash
            config = (self.vlevel.get(), self.alevel.get(), self.engine.get(), delete, self.pano.get())
            if delete and not messagebox.askyesno('确认移入回收站', '本批次成功、变小并通过验证的原片将移入回收站。继续？'): return
            dest.mkdir(parents=True, exist_ok=True)
            self.launch(lambda: self.compress(jobs, config))
        except Exception as e: messagebox.showerror('无法开始', str(e))

    def compress(self, jobs, config):
        vl, al, choice, delete, pano = config
        copy_video = vl == '保留原片质量'
        total = len(jobs)
        update_progress(self, total=total, processed=0, remaining=total, index=0, file='', phase='准备', current_seconds=0, duration=0)
        for index, (src, dst) in enumerate(jobs, 1):
            if self.cancel.is_set(): break
            temp = dst.with_name('.' + uuid.uuid4().hex + '.partial.mp4')
            try:
                if src.suffix.lower() == '.osv':
                    raise ValueError('OSV 必须先经过全景拼接流程，不能直接压缩单个鱼眼轨道')
                update_progress(self, index=index, file=getattr(self, 'display_source_name', src.name), phase='分析原片', current_seconds=0, duration=0, remaining=total-index+1)
                self.events.put(f'[{index}/{len(jobs)}] 分析 {src.name}')
                info = probe(src, self.cancel); v = video(info)
                duration = float(info.get('format', {}).get('duration', 0) or 0)
                update_progress(self, duration=duration)
                usable_videos = [s for s in info['streams'] if s['codec_type'] == 'video' and not s.get('disposition', {}).get('attached_pic')]
                if len(usable_videos) > 1:
                    self.events.put(f'检测到 {len(usable_videos)} 个视频流；使用主画面 {v["width"]}×{v["height"]}，忽略辅助流')
                source_map = f'0:{v["index"]}'
                if v.get('color_transfer') in ('smpte2084', 'arib-std-b67'):
                    raise ValueError('检测到 HDR，请先导出 SDR 视频')
                if pano and v['width'] != v['height'] * 2:
                    raise ValueError('全景模式仅支持完整 2:1 等距柱状投影视频')
                if not pano and any('spherical' in str(x).lower() for x in v.get('side_data_list', [])):
                    raise ValueError('检测到全景信息，请勾选全景模式')
                n, d = map(float, v.get('avg_frame_rate', '30/1').split('/')); fps = n/d if d else 30
                rate = max(350000, int(v['width'] * v['height'] * fps * LEVELS[vl][0]))
                if v.get('bit_rate'): rate = min(rate, int(int(v['bit_rate']) * 0.9))
                tenbit = '10' in v.get('pix_fmt', '') or '12' in v.get('pix_fmt', '')
                original_hash = digest_progress(self, src, '读取原片并校验内容')
                used, used_tuned, skipped = None, True, []
                for engine in ([] if copy_video else candidates(choice)):
                    quality = QUALITY.get(vl, {}).get(engine)
                    # Older NVIDIA drivers may reject lookahead/AQ; retry with plain VBR before moving on.
                    for tuned in ((True, False) if engine == 'hevc_nvenc' and quality else (True,)):
                        try:
                            mode = (f'画质值 {quality}，峰值上限 {rate*1.5/1e6:.2f} Mbps' if quality and tuned
                                    else f'目标视频码率 {rate/1e6:.2f} Mbps')
                            self.events.put(f'试编码：{engine}，{mode}')
                            run([executable('ffmpeg'), '-v', 'error', '-nostdin', *decode_args(engine, tuned), '-i', str(src), '-t', '1', '-map', source_map, '-an'] + encoding_args(engine, rate, tenbit, vl, tuned) + ['-f', 'null', '-'], self.cancel)
                            used = engine; used_tuned = tuned; break
                        except RuntimeError as e:
                            if self.cancel.is_set(): raise
                            self.events.put(f'{engine} 不可用：{e}')
                            skipped.append(f'{ENGINE_INFO.get(engine, (engine,))[0]}{"（完整参数）" if tuned and engine == "hevc_nvenc" and quality else ""}：{short_error(e)}')
                    if used: break
                if copy_video: used = 'copy'
                if not used: raise ValueError('没有可用的编码器')
                color = []
                for key, flag in [('color_primaries', '-color_primaries'), ('color_transfer', '-color_trc'), ('color_space', '-colorspace'), ('color_range', '-color_range')]:
                    if v.get(key) and v[key] != 'unknown': color += [flag, v[key]]
                name, kind = ENGINE_INFO.get(used, (used, ''))
                self.events.put(f'正在压缩：{src.name}（{name}，{kind}）；可点击停止')
                update_progress(self, phase='正在压缩', current_seconds=0, engine=used, engine_name=name, engine_kind=kind,
                                engine_skipped=skipped, hw_decode=bool(decode_args(used, used_tuned)))
                video_args = ['-c:v', 'copy'] if copy_video else encoding_args(used, rate, tenbit, vl, used_tuned) + color + ['-fps_mode', 'passthrough']
                audio_args = ['-c:a', 'copy'] if al == '保留原片质量' else ['-c:a', 'aac', '-b:a', str(LEVELS[al][1])+'k']
                run_progress([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-noautorotate', *decode_args(used, used_tuned), '-i', str(src), '-map', source_map, '-map', '0:a?', '-map_metadata', '0', '-map_chapters', '0'] + video_args + audio_args + ([] if pano else ['-movflags', '+faststart']) + [str(temp)], self.cancel, lambda seconds: update_progress(self, current_seconds=min(seconds, duration) if duration else seconds))
                if pano:
                    inject(temp)
                    if not any('spherical' in str(x).lower() for x in video(probe(temp, self.cancel)).get('side_data_list', [])):
                        raise ValueError('全景标记验证失败')
                self.events.put('正在完整解码验证成品…')
                update_progress(self, phase='完整播放检查', current_seconds=0)
                validate(info, temp, self.cancel, lambda seconds: update_progress(self, current_seconds=min(seconds, duration) if duration else seconds))
                if digest_progress(self, src, '确认原片未发生变化') != original_hash: raise ValueError('压缩期间原片发生变化，未保存成品')
                # Exclusive creation via hard link prevents overwriting a concurrent file.
                try:
                    os.link(temp, dst)
                except FileExistsError:
                    raise
                except OSError:
                    out = open(dst, 'xb')
                    try:
                        with out, open(temp, 'rb') as inp:
                            shutil.copyfileobj(inp, out, 4 * 1024 * 1024)
                    except Exception:
                        dst.unlink()
                        raise
                temp.unlink()
                record = {'version': 2, 'source': str(src), 'output': str(dst), 'source_sha256': original_hash, 'output_sha256': digest_progress(self, dst, '校验并记录成品'), 'verified': True, 'deleted': False, 'engine': used, 'panorama': pano, 'video_copy': copy_video, 'source_state': file_state(src), 'output_state': file_state(dst)}
                manifest = dst.with_suffix('.mp4.dji.json'); save_record(manifest, record)
                source_marker = src.with_suffix(src.suffix + '.dji.json')
                if source_marker != manifest:
                    try: save_record(source_marker, record)
                    except OSError as marker_error: self.events.put(f'提示：无法在原片目录写入隐藏完成标记：{marker_error}')
                if hasattr(self, 'manifests'):
                    self.manifests.append(str(manifest))
                saving = 1 - dst.stat().st_size / src.stat().st_size
                self.events.put(f'完成：{dst.name}；体积变化 {saving:+.1%}（正数为节省）')
                if delete:
                    self.events.put(recycle(record, manifest, self.cancel, 'permanent' if delete == 'permanent' else 'trash', owner=self))
            except Exception as e: self.events.put(f'{src.name}：{e}')
            finally:
                if temp.exists(): temp.unlink()
                if self.cancel.is_set():
                    update_progress(self, phase='已停止', processed=index-1, remaining=total-index+1)
                else:
                    update_progress(self, phase='已处理', processed=index, remaining=total-index, current_seconds=getattr(self, 'progress', {}).get('duration', 0))
        if not self.cancel.is_set():
            update_progress(self, phase='全部完成', processed=total, remaining=0, index=total)

    def match_delete(self):
        if self.busy: return
        try:
            self.dependencies()
            import send2trash
        except Exception as e:
            messagebox.showerror('缺少依赖', str(e)); return
        files = filedialog.askopenfilenames(title='选择成品旁的匹配记录（可多选）', initialdir=self.out.get(), filetypes=[('压缩匹配记录', '*.dji.json')])
        if not files: return
        if not messagebox.askyesno('匹配删除', f'将核对所选 {len(files)} 条记录的原片与成品，完整验证后把原片移入回收站。继续？'): return
        def task():
            for f in files:
                if self.cancel.is_set(): break
                try: self.events.put(recycle(json.loads(Path(f).read_text(encoding='utf-8')), Path(f), self.cancel))
                except Exception as e: self.events.put(f'{Path(f).name}：{e}')
        self.launch(task)

    def close(self):
        if self.busy:
            self.cancel.set(); messagebox.showinfo('正在停止', '正在停止并清理临时文件，请任务结束后关闭。')
        else: self.destroy()

if __name__ == '__main__':
    App().mainloop()
