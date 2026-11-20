"""End-to-end self test.  Builds a fake footage drive and a fake SD card in a temp folder,
then checks every library rule and the SD import flow.  Needs FFmpeg on PATH.

    python tools/selftest.py   (Windows 免安装版：runtime\\python\\python.exe app\\tools\\selftest.py)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import library as L  # noqa: E402
import sd_import as sd  # noqa: E402
import web_app as w  # noqa: E402

seed = 0


def make(root, rel, sec=4, compressed=False):
    global seed
    seed += 1
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if compressed:
        args = ['-f', 'lavfi', '-i', f'testsrc2=size=320x240:rate=25,hue=h={seed * 25}', '-t', str(sec),
                '-c:v', 'libx265', '-b:v', '60k', '-tag:v', 'hvc1', '-x265-params', 'log-level=error']
    else:
        args = ['-f', 'lavfi', '-i', f'testsrc2=size=320x240:rate=25,noise=alls=40:allf=t:all_seed={seed}',
                '-t', str(sec), '-c:v', 'libx264', '-b:v', '3M']
    subprocess.run([L.c.executable('ffmpeg'), '-v', 'error', '-y', *args, '-f', 'mp4', str(path)], check=True)
    return path


def set_mtime(path, *when):
    stamp = time.mktime((*when, 0, 0, -1))
    os.utime(path, (stamp, stamp))


def build_drive(root):
    make(root, '2024/2024.08/08.09 海边/CAM_20240809101500_0001_D.MP4')
    make(root, '2024/2024.08/08.09 海边/CAM_20240809143000_0002_D.MP4', 6)
    (root / '2024/2024.08/08.09 海边/CAM_20240809101500_0001_D.LRF').write_text('lrf')
    make(root, '2025年/2025年10月/2025年10月21日 骑车/CAM_20251021090000_0001_D.MP4')
    make(root, '2025年/2025年10月/2025年10月21日 骑车/20251021_090000_CMP_001.mp4', compressed=True)
    make(root, '2025/12.5 第一次见 rebelo/DJI_0001.MP4')
    make(root, '2025/12.5 第一次见 rebelo/DJI_0002.MP4')
    new_year = make(root, '2026-01-13 新年/CAM_20260113235900_0003_D.MP4', 5)
    (root / '2026-01-13 新年/备份').mkdir()
    shutil.copy2(new_year, root / '2026-01-13 新年/备份' / new_year.name)
    school = make(root, '2026/2026.03/03.14-学校/CAM_20260314120000_0001_D.MP4', 5)
    shutil.copy2(school, school.with_name('CAM_20260314120000_0001_D (1).MP4'))
    make(root, '2026/2026.03/03.14-学校/另一台/GH_20260314120002_A.MP4', 5)
    make(root, '2026/2026.03/03.14-学校/另一台/GH_20260314150000_B.MP4', 4)
    make(root, '2026/2026.03/03.14-学校/VID_20260314_150000.mp4')
    make(root, '2026/2026.03/03.14-学校/20260314_160000_CMP_001.mp4', compressed=True)
    make(root, '2026/2026.03/03.14-学校/CAM_20260314160000_0005_D.MP4')
    (root / '2026/2026.03/03.14-学校/20260314_160000_CMP_001.mp4.dji.json').write_text(json.dumps({
        'source': 'H:\\2026\\2026.03\\03.14-学校\\CAM_20260314160000_0005_D.MP4',
        'output': 'H:\\2026\\2026.03\\03.14-学校\\20260314_160000_CMP_001.mp4', 'verified': True}, ensure_ascii=False), encoding='utf8')
    unnamed = make(root, '杂项/未命名视频.mp4')
    set_mtime(unnamed, 2024, 5, 1, 12, 0, 0)
    (root / '杂项/CAM_20260201100000_0009_D.MP4').write_bytes(new_year.read_bytes()[:200000].replace(b'moov', b'xxxx'))
    for twin_of in (school, new_year, root / '2024/2024.08/08.09 海边/CAM_20240809101500_0001_D.MP4'):
        twin_of.with_name('._' + twin_of.name).write_bytes(b'\x00\x05\x16\x07' + b'\x00' * 4092)  # macOS AppleDouble
    (root / '2026/2026.03/03.14-学校/._20260314_160000_CMP_001.mp4.dji.json').write_bytes(b'\x00\x05\x16\x07')


def check(condition, label):
    print(('  ✓ ' if condition else '  ✗ ') + label)
    if not condition:
        check.failed += 1


check.failed = 0


def test_library(base):
    root = base / 'drive'
    build_drive(root)
    print('素材库扫描')
    r = L.scan(root)
    F, I = r['files'], r['issues']
    name = {f['id']: f['rel'].split('/')[-1] for f in F}
    check(r['summary']['total'] == 17, '共 17 个视频（Mac 的 ._ 文件不算）')
    compress_scan = w.scan_library(root)
    check(compress_scan['candidate_count'] + compress_scan['explicit_compressed_count'] + compress_scan['likely_compressed_count']
          + compress_scan['completed_count'] <= 17 and not any('/._' in f or '\\._' in f for d in compress_scan['days'] for f in d['files']),
          '压缩页的按日期扫描也不把 ._ 文件当视频')
    check(len(I['unmarked']) == 14, '14 个未标记原片')
    check([sorted(name[i] for i in g['ids']) for g in I['same_time']] == [['GH_20260314150000_B.MP4', 'VID_20260314_150000.mp4']], '(a) 同一时间点')
    check(len(I['overlap']) == 1 and I['overlap'][0]['seconds'] == 3.0, '(a) 时间重叠 3 秒')
    check(sorted(sorted(name[i] for i in g['ids']) for g in I['duplicates']) == [
        ['CAM_20260113235900_0003_D.MP4', 'CAM_20260113235900_0003_D.MP4'],
        ['CAM_20260314120000_0001_D (1).MP4', 'CAM_20260314120000_0001_D.MP4']], '(b) 完全一样的文件')
    check(sorted(p['by'] for p in I['pairs']) == ['压缩记录', '同一拍摄时间'], '(c) 原片与压缩版并存')
    rebelo = next(f for f in F if f['name'] == 'DJI_0001.MP4')
    check(rebelo['start'].startswith('2025-12-05') and rebelo['desc'] == '第一次见 rebelo', '「12.5 第一次见 rebelo」文件夹日期与描述')
    check(next(f for f in F if f['name'] == 'CAM_20260201100000_0009_D.MP4')['readable'] is False, '损坏视频标为读不出')
    levels = {t['name']: t['level'] for t in r['tree']}
    check(levels.get('2025年10月21日 骑车') == 'day' and levels.get('2025年10月') == 'month' and levels.get('2025年') == 'year', '年 / 月 / 日父子结构')

    print('标记 _ORG 与撤销')
    marked = L.mark_originals(root, [F[i]['rel'] for i in I['unmarked']])
    check(len(marked['renamed']) == 14 and not marked['skipped'], '14 个原片已加 _ORG')
    check((root / '2024/2024.08/08.09 海边/CAM_20240809101500_0001_D_ORG.LRF').exists(), '同名 LRF 一起改名')
    check((root / '2024/2024.08/08.09 海边/._CAM_20240809101500_0001_D_ORG.MP4').exists()
          and not (root / '2024/2024.08/08.09 海边/._CAM_20240809101500_0001_D.MP4').exists(), 'Mac 的 ._ 文件跟着改名')
    manifest = json.loads((root / '2026/2026.03/03.14-学校/20260314_160000_CMP_001.mp4.dji.json').read_text(encoding='utf8'))
    check(manifest['source'].endswith('CAM_20260314160000_0005_D_ORG.MP4'), '压缩记录指向新文件名')
    check(len(L.scan(root)['issues']['unmarked']) == 0, '重新扫描后没有未标记原片')
    undo = L.undo_last_mark(root)
    check(len(undo['restored']) == 15 and not undo['problems'] and (root / '2024/2024.08/08.09 海边/._CAM_20240809101500_0001_D.MP4').exists(),
          '撤销恢复全部文件名（含 ._ 文件）')
    check(len(L.scan(root)['issues']['unmarked']) == 14, '撤销后恢复为 14 个未标记')

    page = L.export_page(L.scan(root), (L.c.BASE / '素材日历.html').read_text(encoding='utf8'))
    html = Path(page).read_text(encoding='utf8')
    check("const MODE='static'" in html and '"files"' in html, '硬盘根目录网页已生成并内嵌数据')


def test_sd(base):
    print('SD 卡导入')
    card_root, ssd = base / 'card', base / 'ssd'
    (card_root / 'DCIM/A').mkdir(parents=True)
    (ssd / '10.21 骑车').mkdir(parents=True)
    good = make(card_root, 'DCIM/A/CAM_20261021143025_0001_D.MP4', 3)
    (card_root / 'MISC').mkdir()
    (card_root / 'MISC/CAM_20261021143025_0001_D.LRF').write_text('x')
    (card_root / 'DCIM/A/._CAM_20261021143025_0001_D.MP4').write_bytes(b'\x00\x05\x16\x07' + b'\x00' * 4092)
    broken = card_root / 'DCIM/A/CAM_20261021150000_0002_D.MP4'
    broken.write_bytes(good.read_bytes()[:100000].replace(b'moov', b'xxxx'))
    make(card_root, 'DCIM/A/CAM_20261021150000_0002_D.LRF', 3)
    (ssd / '10.21 骑车/20261021_080000_ORG_004.mp4').write_bytes(b'')
    sd.LOG_PATH = base / 'sd-log.jsonl'
    targets = [{'mount': str(ssd), 'name': 'ssd', 'free': 10**12}]

    def run(uuid):
        card = {'mount': str(card_root), 'name': 'card', 'uuid': uuid}
        sd.list_volumes = lambda roots: ([card], targets)
        plan = sd.build_plan((), w.find_storage_dir)
        sd.start(plan['plan_id'])
        while sd.job['running']:
            time.sleep(0.1)
        return plan

    plan = run('UUID-A')
    check([i['name'] for i in plan['items']] == ['20261021_143025_ORG_001.mp4', '20261021_150000_ORG_002.mp4'], '沿用已有文件夹，编号沿用相机原编号 0001→ORG_001（卡上 ._ 文件不导入）')
    check(not good.exists() and not (card_root / 'MISC/CAM_20261021143025_0001_D.LRF').exists(), '好视频移走、代理文件删除')
    check(broken.exists() and len(sd.job['corrupt']) == 1, '坏视频留在卡上等待确认')
    check((ssd / '10.21 骑车_ORG/20261021_150000_ORG_002_LRF.mp4').exists(), '坏视频的 LRF 已存到固态硬盘')
    plan = run('UUID-B')  # same card, different volume id
    check(plan['items'][0]['status'] == 'copied' and len(list((ssd / '10.21 骑车_ORG').glob('*150000_ORG_*.mp4'))) == 2, '换了卷 ID 也不会重复复制')
    sd.remove_corrupt([e['id'] for e in sd.job['corrupt']], keep_ssd_copy=True)
    check(not broken.exists() and (ssd / '10.21 骑车_ORG/20261021_150000_ORG_002.mp4').exists(), '确认后清卡，保留一份损坏原片')
    imported = ssd / '10.21 骑车_ORG/20261021_143025_ORG_001.mp4'
    names = w.names({'files': [str(imported)], 'start': 1, 'width': 3, 'suffix': 'CMP', 'archive': False})
    check([n['name'] for n in names] == ['20261021_143025_CMP_001.mp4'], '压缩时编号跟着走：ORG_001 → CMP_001')


def test_sd_resume(base):
    print('SD 卡断点续拷')
    card_root, ssd = base / 'card2', base / 'ssd2'
    (card_root / 'DCIM').mkdir(parents=True); ssd.mkdir()
    big = card_root / 'DCIM/CAM_20261022100000_0001_D.MP4'
    good = make(base, 'tmp/clip.mp4', 3)
    big.write_bytes(good.read_bytes() + os.urandom(150 * 1024 * 1024))  # playable head, ~3 chunks long
    original = big.read_bytes()
    sd.LOG_PATH = base / 'sd-log2.jsonl'
    card = {'mount': str(card_root), 'name': 'card2', 'uuid': 'U2'}
    sd.list_volumes = lambda roots: ([card], [{'mount': str(ssd), 'name': 'ssd2', 'free': 10**12}])
    real_copy = sd._copy_resumable

    def stop_after_two_chunks(item, part, meta, cancel, step):
        seen = []
        def counting(n):
            step(n); seen.append(n)
            if len(seen) == 2: cancel.set()
        return real_copy(item, part, meta, cancel, counting)
    sd._copy_resumable = stop_after_two_chunks
    plan = sd.build_plan((), w.find_storage_dir)
    sd.start(plan['plan_id'])
    while sd.job['running']:
        time.sleep(0.1)
    sd._copy_resumable = real_copy
    part = next((ssd / '.qingsave/partials').glob('*.part'))
    check(big.exists() and part.stat().st_size == 2 * sd.CHUNK, '中断后卡上原片还在，已复制部分保留')
    plan = sd.build_plan((), w.find_storage_dir)
    check(plan['items'][0]['resume_bytes'] == 2 * sd.CHUNK, '重新检测时知道可以接着复制')
    sd.start(plan['plan_id'])
    while sd.job['running']:
        time.sleep(0.1)
    dest = ssd / '10.22_ORG' / '20261022_100000_ORG_001.mp4'
    check(dest.is_file() and dest.read_bytes() == original, '接着复制完成，内容与原片完全一致')
    check(not big.exists() and not list((ssd / '.qingsave/partials').glob('*')), '校验后清卡并清理临时文件')
    check(any('接着上次复制' in line for line in sd.job['logs']), '日志里记录了续拷')


def run_queue(files, out, delete_mode):
    data = {'files': [str(f) for f in files], 'start': 1, 'width': 3, 'suffix': 'CMP', 'archive': False, 'same': False,
            'output': str(out), 'video': '中等压缩', 'audio': '中等压缩', 'engine': 'CPU（较慢）', 'pano': False,
            'delete_mode': delete_mode, 'osv': {}, 'verification_mode': 'cpu', 'temporary_storage': False}
    ids = w.enqueue(data)
    for _ in range(600):
        with w.task_condition:
            mine = [t for t in w.tasks if t['id'] in ids]
            if all(t['status'] not in ('pending', 'running') for t in mine):
                return {t['source']: t for t in mine}
        time.sleep(0.2)
    raise RuntimeError('queue did not finish')


def test_name_clash(base):
    print('同名文件：先比哈希再处理')
    root, output_root = base / 'clash', base / 'clash-out'
    out = output_root / 'CMP'
    original = make(root, 'a/CAM_20261021120000_0001_D.MP4', 2)
    backup = root / 'b/CAM_20261021120000_0001_D.MP4'
    backup.parent.mkdir(parents=True); shutil.copy2(original, backup)
    other = make(root, 'c/CAM_20261021120000_0001_D.MP4', 2)  # same name, different picture
    result = run_queue([original, backup, other], out, 'permanent')
    check(not original.exists() and (out / '20261021_120000_CMP_001.mp4').is_file(), '第一份正常压缩、校验后删原片')
    check(not backup.exists() and '完全一样' in result[str(backup.resolve())]['message'], '内容一样的备份：比对哈希后删除')
    check(not other.exists() and (out / '20261021_120000_CMP_001-2.mp4').is_file(), '同名但内容不同：改名 -2 后正常压缩')
    again = make(root, 'd/CAM_20261021130000_0002_D.MP4', 2)
    keep = shutil.copy2(again, root / 'd/copy.bin')
    run_queue([again], out, 'keep')
    check(again.exists() and (out / '20261021_130000_CMP_002.mp4').is_file(), '先压缩一次并保留原片')
    result = run_queue([again], out, 'permanent')
    check(not again.exists() and '完整性核对通过' in result[str(again.resolve())]['message'], '已压缩过：核对哈希和完整性后删除原片')
    Path(keep).rename(again)  # put the same original back, now with a corrupted compressed copy
    (out / '20261021_130000_CMP_002.mp4').write_bytes((out / '20261021_130000_CMP_002.mp4').read_bytes()[:5000])
    result = run_queue([again], out, 'permanent')
    check(again.exists() and result[str(again.resolve())]['status'] == 'failed', '已有压缩版损坏：不删原片、报错')



def test_folder_tags(base):
    print('ORG / CMP 分开保存')
    import folder_tags as ft
    root = base / 'tags'
    fixtures = ['10.31/20261031_214333_ORG_001.mp4', '10.31/20261031_214333_ORG_001.LRF',
                '10.31/20261031_214333_ORG_001_LRF.mp4', '10.31/20261031_214333_CMP_001.mp4',
                '10.31/20261031_214333_CMP_001_LRF.mp4', '10.31/IMG_0001.MOV',
                '10.12 学校/CAM_20261012101010_0001_D.MP4',
                '10.16/a_ORG_001.mp4', '10.16/.0123.partial.mp4', '10.17/a_ORG_001.mp4',
                '10.18/a_ORG_001.mp4', '10.18_ORG/a_ORG_001.mp4']
    for rel in fixtures:
        p = root / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(rel.encode())
    manifest = root / '10.31/20261031_214333_CMP_001.mp4.dji.json'
    manifest.write_text(json.dumps({'source': str(root / fixtures[0]), 'output': str(root / fixtures[3])}, ensure_ascii=False, indent=2))
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    changes, skipped = ft.plan([root], locked=[str(root / '10.17/a_ORG_001.mp4')])
    check(len(skipped) == 3, '正在写入、未完成队列、目标重名均跳过')
    # Emulate macOS/exFAT moving AppleDouble automatically with its owner.
    companion = root / '10.31' / ('._' + Path(fixtures[3]).name)
    companion.write_bytes(b'metadata')
    before[str(companion.relative_to(root))] = companion.read_bytes()
    changes, skipped = ft.plan([root], locked=[str(root / '10.17/a_ORG_001.mp4')])
    real_rename = Path.rename
    def native_rename(path, target):
        result = real_rename(path, target)
        side = path.with_name('._' + path.name)
        if not path.name.startswith('._') and side.exists():
            real_rename(side, Path(target).with_name('._' + Path(target).name))
        return result
    Path.rename = native_rename
    try:
        done, failed = ft.apply(changes)
    finally:
        Path.rename = real_rename
    check(done and not failed, '混放文件分开，原片和 LRF 一起移动')
    check((root / '10.31_ORG/20261031_214333_ORG_001.LRF').is_file()
          and (root / '10.31_CMP/20261031_214333_CMP_001_LRF.mp4').is_file(), '原始 LRF 在 ORG，压缩过的代理在 CMP')
    check((root / '10.31/IMG_0001.MOV').is_file(), '未知视频不擅自移动')
    record = json.loads((root / '10.31_CMP' / manifest.name).read_text())
    check('_ORG/' in record['source'] and '_CMP/' in record['output'], '压缩记录随视频移动并更新路径')
    check(not ft.plan([root])[0] or all('10.17' in c['from'] for c in ft.plan([root])[0]), '重复整理不会再移动已分好的文件')
    check(ft.destination(root / '10.31_ORG', 'CMP') == root / '10.31_CMP', '原目录压缩直接放同日期 CMP')
    check(w.find_storage_dir(10, 31, [root], tag='ORG') == root / '10.31_ORG', 'SD 导入不会选到 CMP')
    preview = w.names({'files': [str(root / '10.31_ORG' / Path(fixtures[0]).name)], 'start': 1, 'width': 3, 'suffix': 'CMP', 'same': True})
    check(preview[0]['directory'] == str(root / '10.31_CMP'), '预览与实际输出都指向 CMP，原编号保持')
    restored = ft.undo(root)
    after = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file() and '.qingsave' not in p.parts}
    check(restored and before == after, '撤销恢复全部内容与记录路径，不覆盖任何文件')


def test_retime(base):
    print('时间校准')
    import datetime as dt
    import retime
    root = base / 'retime'
    a = make(root, '02.06_ORG/20220206_133128_ORG_001.mp4', 2)
    b = make(root, '02.06_ORG/20220206_134659_ORG_002.mp4', 2)
    for clip, utc in ((a, '2022-02-06T05:31:29Z'), (b, '2022-02-06T05:46:59Z')):  # camera clock: UTC+8, wrong date
        tmp = clip.with_suffix('.tmp.mp4')
        subprocess.run([L.c.executable('ffmpeg'), '-v', 'error', '-y', '-i', str(clip), '-c', 'copy', '-metadata',
                        f'creation_time={utc}', str(tmp)], check=True)
        tmp.replace(clip)
    (root / '02.06_ORG/20220206_134659_ORG_002.mp4.dji.json').write_text('{"source": "/x/20220206_134659_ORG_002.mp4"}')
    before = {p.name: p.read_bytes() for p in (root / '02.06_ORG').iterdir()}
    folder = root / '02.06_ORG'
    opt = {'mode': 'reference', 'reference': {'file': b.name, 'position': '6:01', 'real': '2026-10-30T11:53:39'}}
    plan = retime.plan(folder, opt)
    check(plan['shift_seconds'] == int((dt.datetime(2026, 10, 30, 11, 53, 39) - dt.datetime(2022, 2, 6, 13, 53)).total_seconds()),
          '按「第几分钟＝真实时间」算出平移')
    check([i['to'] for i in plan['items']] == ['20261030_113207_APPROX_ORG_001.mp4', '20261030_114738_APPROX_ORG_002.mp4']
          and plan['folder_to'] == '10.30_ORG', '文件名加 APPROX，日期文件夹跟着改')
    result = retime.apply(folder, opt)
    new = root / '10.30_ORG'
    info = L.c.probe(new / '20261030_113207_APPROX_ORG_001.mp4')
    created = dt.datetime.fromisoformat(info['format']['tags']['creation_time'].replace('Z', '+00:00')).astimezone().replace(tzinfo=None)
    check(abs((created - dt.datetime(2026, 10, 30, 11, 32, 7)).total_seconds()) <= 1, '视频里的拍摄时间同步改好')
    check('20261030_114738_APPROX_ORG_002.mp4' in (new / '20261030_114738_APPROX_ORG_002.mp4.dji.json').read_text(), '压缩记录跟着改名')
    check(len(result['done']) == 2, '两段都已校准')
    tz = retime.plan(new, {'mode': 'offset', 'offset_seconds': -3600, 'approx': False})
    check(tz['items'][0]['to'] == '20261030_103207_APPROX_ORG_001.mp4', '时区平移：已有 APPROX 不重复加')
    last = retime.last_batch([root])
    retime.undo(last[0], last[1])
    after = {p.name: p.read_bytes() for p in folder.iterdir()} if folder.is_dir() else {}
    check(after == before, '撤销后逐字节恢复原样（文件名、文件夹、记录）')
    check(retime.last_batch([root]) is None, '撤销过的不再出现在「上一次」')


def test_sd_keep_lrf(base):
    print('SD 导入：带 LRF / 保留卡')
    import folder_tags as ft
    card, ssd = base / 'lrf-card', base / 'lrf-ssd'
    video = make(card, 'DCIM/DJI_001/DJI_20261109075919_0001_D.MP4', 2)
    lrf = make(card, 'DCIM/DJI_001/DJI_20261109075919_0001_D.LRF', 1)
    video2 = make(card, 'DCIM/DJI_001/DJI_20261109082317_0002_D.MP4', 2)
    lrf2 = make(card, 'DCIM/DJI_001/DJI_20261109082317_0002_D.LRF', 1)
    ssd.mkdir()
    saved = sd.list_volumes
    volumes = ([{'mount': str(card), 'name': 'CARD', 'uuid': 'u'}], [{'mount': str(ssd), 'name': 'SSD', 'free': 10**12, 'total': 10**12}])
    sd.list_volumes = lambda roots: volumes
    finder = lambda m, d, roots: None
    def run(**options):
        plan = sd.build_plan([ssd], finder, keep_lrf=options.get('keep_lrf', False))
        sd.start(plan['plan_id'], **options)
        while sd.job['running']: time.sleep(0.05)
        return plan
    try:
        plan = run(keep_lrf=True, keep_card=True)
        check(plan['size'] == sum(p.stat().st_size for p in (video, lrf, video2, lrf2)), '空间按视频 + LRF 计算')
        folder = ssd / '11.09_ORG'
        main, side = folder / '20261109_075919_ORG_001.mp4', folder / '20261109_075919_ORG_001.LRF'
        check(main.read_bytes() == video.read_bytes() and side.read_bytes() == lrf.read_bytes(), 'LRF 与视频同名放在一起，内容一致')
        check(all(p.exists() for p in (video, lrf, video2, lrf2)), '只复制时卡上文件全部保留')
        check(ft.kind(side.name) == 'proxy', 'LRF 不影响文件夹后缀判断')
        folder.rename(ssd / '11.09')
        again = sd.build_plan([ssd], finder)
        check({i['status'] for i in again['items']} == {'copied'}, '文件夹加了后缀，仍认得已复制（不会重复拷）')
        run()
        check(not video.exists() and not lrf.exists() and not video2.exists(), '之后普通导入：核对一致才清卡')
        check(len(list((ssd / '11.09').glob('*.mp4'))) == 2 and (ssd / '11.09' / side.name).is_file(), '没有多出重复文件，LRF 还在')
    finally:
        sd.list_volumes = saved


def test_space(base):
    print('空间预估')
    import space
    root = base / 'space'
    clip = make(root, 'a/CAM_20261022120000_0001_D.MP4', 3)
    size = clip.stat().st_size
    est, raw = space.estimate(clip, '中等压缩', '中等压缩')
    check(raw and 0 < est <= size, '按目标码率估算压缩后大小')
    mount = space.mount_of(str(clip), space.mounts())
    task = {'source': str(clip), 'output': str(root / 'a/x_CMP_001.mp4'), 'status': 'pending', 'video': '中等压缩',
            'audio': '中等压缩', 'config': ['中等压缩', '中等压缩', '自动选择', 'permanent', False], 'source_size': size}
    drive = next(d for d in space.summary([task])['drives'] if d['mount'] == mount)
    check(drive['queue_change'] == size - est, '彻底删除：完成后多出 原片 − 成品')
    task['config'][3] = 'trash'
    drive = next(d for d in space.summary([task])['drives'] if d['mount'] == mount)
    check(drive['queue_change'] == -est and drive['trash_after'] == size, '回收站：先占用成品空间，清空回收站后才腾出原片')
    result = space.summary([], [{'source': str(clip), 'video': '中等压缩', 'audio': '中等压缩'}], 'keep')
    check(result['selection']['estimate'] == est and result['selection']['items'][str(clip)] == est, '所选视频的预估')


if __name__ == '__main__':
    if not L.c.executable('ffmpeg') or not L.c.executable('ffprobe'):
        sys.exit('需要 FFmpeg：放进 bin/ 或加入 PATH')
    with tempfile.TemporaryDirectory(prefix='qingsave-selftest-') as folder:
        base = Path(folder)
        import applog  # keep test logs out of the real log folder
        applog.DATA_DIR, applog.LOG_DIR = base / 'appdata', base / 'appdata' / 'logs'
        test_library(base)
        test_sd(base)
        test_sd_resume(base)
        test_name_clash(base)
        test_space(base)
        test_folder_tags(base)
        test_retime(base)
        test_sd_keep_lrf(base)
    print('\n全部通过' if not check.failed else f'\n{check.failed} 项失败')
    sys.exit(1 if check.failed else 0)
