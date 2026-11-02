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
    check((ssd / '10.21 骑车/20261021_150000_ORG_002_LRF.mp4').exists(), '坏视频的 LRF 已存到固态硬盘')
    plan = run('UUID-B')  # same card, different volume id
    check(plan['items'][0]['status'] == 'copied' and len(list((ssd / '10.21 骑车').glob('*150000_ORG_*.mp4'))) == 2, '换了卷 ID 也不会重复复制')
    sd.remove_corrupt([e['id'] for e in sd.job['corrupt']], keep_ssd_copy=True)
    check(not broken.exists() and (ssd / '10.21 骑车/20261021_150000_ORG_002.mp4').exists(), '确认后清卡，保留一份损坏原片')
    imported = ssd / '10.21 骑车/20261021_143025_ORG_001.mp4'
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
    dest = ssd / '10.22' / '20261022_100000_ORG_001.mp4'
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
    root, out = base / 'clash', base / 'clash-out'
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
    print('文件夹后缀')
    import folder_tags as ft
    import library
    root = base / 'tags'
    for rel in ('10.31/20261031_214333_ORG_001.mp4', '10.31/20261031_214333_ORG_001_LRF.mp4', '10.15/20261015_160907_CMP_001.mp4',
                '10.21/20261021_201840_ORG_006.mp4', '10.21/20261021_201840_CMP_006.mp4', '10.12 学校/CAM_20261012101010_0001_D.MP4',
                '10.13/IMG_0001.MOV', '10.14_ORG/20261014_101010_CMP_001.mp4', '旅行/20261015_160907_ORG_009.mp4',
                '10.16/20261016_101010_ORG_001.mp4', '10.16/.0123.partial.mp4', '10.17/20261017_101010_ORG_001.mp4'):
        p = root / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(b'x')
    changes, skipped = ft.plan([root], locked=[str(root / '10.17/20261017_101010_ORG_001.mp4')])
    got = {c['from']: c['to'] for c in changes}
    check(got.get('10.31') == '10.31_ORG', '全是原片（代理不算）→ _ORG')
    check(got.get('10.15') == '10.15_CMP', '全是压缩版 → _CMP')
    check(got.get('10.12 学校') == '10.12 学校_ORG', '带描述的日期文件夹：相机原名算原片，后缀加在最后')
    check(got.get('10.14_ORG') == '10.14_CMP', '内容变了：后缀跟着改')
    check('10.21' not in got and '10.13' not in got and '旅行' not in got, '混放 / 认不出 / 非日期文件夹不改')
    check({s['from'] for s in skipped} >= {'10.16', '10.17'}, '正在写入或队列还要用的文件夹跳过')
    done, failed = ft.apply(changes)
    check((root / '10.31_ORG').is_dir() and (root / '10.15_CMP').is_dir() and not failed, '改名')
    check(library.parse_folder('10.31_ORG')['desc'] == '' and library.parse_folder('10.12 学校_ORG')['desc'] == '学校', '素材日历不把后缀当描述')
    (root / '10.31_ORG/20261031_220000_CMP_002.mp4').write_bytes(b'x')
    ft.apply(ft.plan([root])[0])
    check((root / '10.31').is_dir(), '变成混放后去掉后缀')
    restored = ft.undo(root)
    check(restored and (root / '10.15').is_dir() and (root / '10.12 学校').is_dir() and (root / '10.31').is_dir(), '撤销改回原名')


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
    print('\n全部通过' if not check.failed else f'\n{check.failed} 项失败')
    sys.exit(1 if check.failed else 0)
