"""Dedicated DJI Studio worker based on exodustw/dji-osv2mp4 (MIT).

Uses an owned Studio process only: never attach to a user's editing session.
The bundled image profile is English; interface compatibility needs a local smoke test.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import compressor as c


def studio_path():
    root = Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'DJI Studio'
    versions = list(root.glob('*/DJI Studio.exe'))
    return max(versions, key=lambda p: p.stat().st_mtime) if versions else root/'DJI Studio.exe'


def paths():
    vendor = c.BASE / 'vendor'
    java = list((vendor/'java').rglob('java.exe'))
    return (java[0] if java else vendor/'java'/'bin'/'java.exe',
            vendor/'sikulixide-2.0.5.jar', vendor/'exporter.sikuli', studio_path())


def status():
    java, jar, script, studio = paths()
    missing = [str(p) for p in (java, jar, script/'exporter.py', studio,
                               c.BASE/'vendor'/'jython-standalone-2.7.3.jar') if not p.exists()]
    return {'installed': not missing, 'missing': missing, 'profile': 'English',
            'verified_on_this_machine': False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--resolution', choices=['4K', '6K', '8K'], default='6K')
    parser.add_argument('--bitrate', choices=['low', 'recommended', 'high'], default='high')
    parser.add_argument('--fps', type=float, required=True)
    args = parser.parse_args()
    if os.name != 'nt': raise ValueError('DJI 界面适配器当前仅支持 Windows')
    if not status()['installed']: raise ValueError('DJI 自动化缺少依赖：'+str(status()['missing']))
    source, target = Path(args.input).resolve(strict=True), Path(args.output).resolve()
    if source.suffix.lower() != '.osv' or target.exists(): raise ValueError('输入必须为 OSV，输出不能已存在')
    if args.fps not in (25, 30): raise ValueError('当前 DJI 自动化配置仅支持 25/30 帧，其他帧率请使用 OpenOSV')
    listing = subprocess.check_output(['tasklist', '/FI', 'IMAGENAME eq DJI Studio.exe', '/FO', 'CSV'],
                                     creationflags=subprocess.CREATE_NO_WINDOW)
    if b'DJI Studio.exe' in listing:
        raise ValueError('请先关闭已打开的 DJI Studio；自动化需要独占新开的窗口，以便安全停止')
    java, jar, script, studio = paths()
    target.parent.mkdir(parents=True, exist_ok=True)
    application = subprocess.Popen([str(studio), str(source)], cwd=studio.parent)
    try:
        time.sleep(5)
        environment = dict(os.environ)
        environment['SIKULIX_HOME'] = str(c.BASE/'vendor'/'sikulix-home')
        result = subprocess.run([str(java), '-Dfile.encoding=UTF-8', '-cp', str(jar)+os.pathsep+str(c.BASE/'vendor'/'jython-standalone-2.7.3.jar'),
                                 'org.python.util.jython', str(script/'exporter.py'),
                                 str(target), args.resolution, args.bitrate, '2.5', str(int(args.fps))],
                                cwd=script.parent, timeout=240, env=environment)
        if result.returncode: raise RuntimeError('DJI Studio 界面操作失败；请检查英文界面和显示缩放')
        # GUI submission is not completion. Require final container, matching duration
        # and stable size before the pipeline's full decoding and spherical checks.
        source_info = c.probe(source)
        duration = float(source_info['format']['duration'])
        last_state = None
        stable = 0
        deadline = time.monotonic()+max(3600, duration*30)
        while time.monotonic() < deadline:
            if application.poll() is not None: raise RuntimeError('DJI Studio 在导出完成前退出')
            time.sleep(2)
            try:
                current = c.file_state(target)
                stable = stable+1 if current == last_state else 0
                last_state = current
                if stable < 3: continue
                info = c.probe(target)
                if abs(float(info['format']['duration'])-duration) <= max(.3, duration*.001):
                    print('DJI_EXPORT_FINISHED', flush=True)
                    return
            except (OSError, RuntimeError, ValueError, KeyError): continue
        raise TimeoutError('DJI 导出等待超时')
    finally:
        # Never touch any pre-existing Studio process; this PID was started above.
        if application.poll() is None:
            subprocess.run(['taskkill', '/PID', str(application.pid), '/T', '/F'],
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)


if __name__ == '__main__':
    try: main()
    except Exception as error:
        print(str(error), file=sys.stderr, flush=True)
        sys.exit(1)
