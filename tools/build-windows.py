"""Build a portable, no-install Windows package of 轻存.

Needs (downloaded once, checksums verified by hand):
  python-3.13.x-embed-amd64.zip   https://www.python.org/downloads/windows/
  ffmpeg-*-essentials_build.zip    https://www.gyan.dev/ffmpeg/builds/
  Send2Trash-*.whl                 pip download "Send2Trash>=1.8.3,<2" --no-deps --only-binary=:all: --platform win_amd64

Usage:
  python tools/build-windows.py <download-folder> <output.zip>

Layout of the package:
  轻存-Windows/启动轻存.cmd         double-click to start; keep the window open while working
  轻存-Windows/使用说明.txt
  轻存-Windows/app/                 this repository (code + pages), app/bin has ffmpeg.exe and ffprobe.exe
  轻存-Windows/runtime/python/      embeddable CPython with Send2Trash in Lib/site-packages
"""
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
APP_FILES = ['web_app.py', 'compressor.py', 'sd_import.py', 'library.py', 'osv_pipeline.py', 'dji_studio_export.py',
             'spherical.py', '界面.html', '素材日历.html', 'requirements.txt', 'README.md', 'AGENTS.md']
APP_DIRS = ['vendor', 'tools']

LAUNCHER = r"""@echo off
chcp 65001 >nul
title QingSave
cd /d "%~dp0app"
echo.
echo   QingSave is starting. Your browser will open automatically.
echo   Keep this window open while copying or compressing. Close it to quit.
echo.
"%~dp0runtime\python\python.exe" web_app.py
if errorlevel 1 (
  echo.
  echo   QingSave stopped with an error. See the message above.
  pause
)
"""

GUIDE = """轻存 · Windows 免安装版

使用
1. 把整个「轻存-Windows」文件夹放在任意位置（例如桌面），不需要安装，也不需要管理员权限。
2. 双击「启动轻存.cmd」。会出现一个黑色窗口，浏览器自动打开轻存页面。
   处理视频期间不要关这个黑色窗口；关掉它就退出轻存。
3. 第一次运行如果 Windows 提示「已保护你的电脑」，点「更多信息」→「仍要运行」。

功能
- SD 卡导入：插入读卡器后页面会自动列出卡上的视频，确认后逐个「复制 → 读回校验 → 删除卡上原片」，
  按拍摄日期放进 MM.DD 文件夹，优先存到卷标为 Data 的硬盘，空间不够再换其他硬盘（不会用系统盘 C:）。
- 压缩：加入队列后逐个「压缩 → 完整解码校验 → 按你选的方式删除原片」，一个做完再做下一个。
  NVIDIA 显卡会自动用硬件编码。
- 素材日历与整理：页面右上角进入，扫描整块硬盘，看每天的视频、冲突和重复文件。

自带组件：Python 3.13（python.org 官方便携版）、FFmpeg（gyan.dev essentials 版）、Send2Trash。
更新：用新版文件夹替换 app 文件夹即可，或者在装了 Git 的电脑上按 README 拉取最新代码。
"""


def build(downloads, output):
    downloads = Path(downloads)
    python_zip = next(downloads.glob('python-3.*-embed-amd64.zip'))
    ffmpeg_zip = next(downloads.glob('ffmpeg*.zip'))
    wheel = next(downloads.glob('[Ss]end2[Tt]rash-*.whl'))
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / '轻存-Windows'
        app, runtime = root / 'app', root / 'runtime' / 'python'
        app.mkdir(parents=True); runtime.mkdir(parents=True)
        for name in APP_FILES:
            shutil.copy2(REPO / name, app / name)
        for name in APP_DIRS:
            shutil.copytree(REPO / name, app / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'java', 'openosv', 'sikulix-home'))
        with zipfile.ZipFile(python_zip) as archive:
            archive.extractall(runtime)
        pth = next(runtime.glob('python3*._pth'))
        # Embedded Python ignores the script folder; list the app and site-packages explicitly.
        pth.write_text(f'{pth.stem}.zip\n.\n..\\..\\app\nLib\\site-packages\nimport site\n', encoding='utf8')
        site = runtime / 'Lib' / 'site-packages'
        site.mkdir(parents=True)
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(site)
        (app / 'bin').mkdir()
        with zipfile.ZipFile(ffmpeg_zip) as archive:
            for info in archive.infolist():
                name = Path(info.filename).name
                if name in ('ffmpeg.exe', 'ffprobe.exe') or (name == 'LICENSE' and info.filename.count('/') == 1):
                    target = app / 'bin' / ('FFmpeg-LICENSE.txt' if name == 'LICENSE' else name)
                    with archive.open(info) as source, open(target, 'wb') as sink:
                        shutil.copyfileobj(source, sink)
        assert (app / 'bin' / 'ffmpeg.exe').is_file() and (app / 'bin' / 'ffprobe.exe').is_file(), 'FFmpeg zip has no ffmpeg.exe / ffprobe.exe'
        (root / '启动轻存.cmd').write_bytes(LAUNCHER.replace('\n', '\r\n').encode('ascii'))
        (root / '使用说明.txt').write_bytes(('﻿' + GUIDE).replace('\n', '\r\n').encode('utf-8'))
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            output.unlink()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(root.rglob('*')):
                archive.write(path, path.relative_to(root.parent))
    return output


if __name__ == '__main__':
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    print(build(sys.argv[1], sys.argv[2]))
