"""Repair regression with real generated media, entirely in a temporary directory."""
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import video_repair as r


class RepairTests(unittest.TestCase):
    def test_real_repair_and_safety(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'input' / 'clip.mp4'
            source.parent.mkdir()
            subprocess.run([r.c.executable('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i',
                'testsrc2=size=160x120:rate=10', '-f', 'lavfi', '-i', 'sine=frequency=440',
                '-t', '1', '-c:v', 'libx264', '-c:a', 'aac', str(source)], check=True)
            self.assertEqual(r.inspect(source)['status'], 'unchanged')
            # Reproduce a DJI mdat that is shorter than its actual payload.
            with source.open('r+b') as f:
                pos = 0
                while True:
                    item = r.box(f, pos, source.stat().st_size)
                    if item['kind'] == 'mdat': break
                    pos += item['size']
                original_size = item['size']
                f.seek(pos)
                f.write(struct.pack('>I', original_size - 32))
            before = source.read_bytes()
            # Extended 64-bit mdat, as used by files beyond 4 GiB, keeps payload offsets.
            extended = source.parent / 'extended.mp4'
            prior = before[pos-8:pos]
            self.assertEqual(prior, struct.pack('>I4s', 8, b'free'))
            ext = bytearray(before)
            ext[pos-8:pos+8] = struct.pack('>I4sQ', 1, b'mdat', original_size + 8 - 32)
            extended.write_bytes(ext)
            self.assertEqual(r.inspect(extended)['mdat']['width'], 16)
            ext_result = r.recover(extended, root / 'extended-output')
            self.assertEqual(ext_result['status'], 'verified_playable')
            self.assertEqual(extended.read_bytes(), ext)
            self.assertEqual(r.inspect(source)['status'], 'repairable')
            with self.assertRaises(ValueError): r.recover(source, source.parent / 'nested')
            output = root / 'recovered'
            result = r.recover(source, output)
            self.assertEqual(result['status'], 'verified_playable', result)
            self.assertEqual(result['decoded_frames'], 10)
            self.assertEqual(source.read_bytes(), before)
            repaired = Path(result['output']).read_bytes()
            self.assertEqual(repaired[:pos], before[:pos])
            self.assertEqual(repaired[pos+4:], before[pos+4:])
            self.assertEqual(result['output_sha256'], hashlib.sha256(repaired).hexdigest())
            receipt = json.loads(Path(result['output'] + '.repair.json').read_text())
            self.assertFalse(receipt['original_deleted'])
            # Retry creates a separate result, never overwrites.
            second = r.recover(source, output)
            self.assertNotEqual(second['output'], result['output'])
            cancelled = threading.Event(); cancelled.set()
            with self.assertRaises(InterruptedError): r.recover(source, output, cancelled)
            self.assertFalse(list(output.glob('*.part')))
            self.assertEqual(source.read_bytes(), before)
            # Corrupt a real video packet: index repair must not certify damaged pictures.
            packets = json.loads(subprocess.run([r.c.executable('ffprobe'), '-v', 'error',
                '-select_streams', 'v:0', '-show_packets', '-of', 'json', second['output']],
                capture_output=True, text=True, check=True).stdout)['packets']
            with source.open('r+b') as f:
                packet = packets[0]
                f.seek(int(packet['pos']))
                f.write(b'\0' * int(packet['size']))
            corrupt_result = r.recover(source, output)
            self.assertEqual(corrupt_result['status'], 'partial', corrupt_result)
            self.assertIn('_PARTIAL', corrupt_result['output'])
            self.assertFalse(corrupt_result['original_deleted'])
            source.write_bytes(before)
            midway = threading.Event()
            def stop_after_copy(phase, *args):
                if phase == '读回校验修复副本': midway.set()
            with self.assertRaises(InterruptedError):
                r.recover(source, output, midway, stop_after_copy)
            self.assertFalse(list(output.glob('*.part')))
            self.assertEqual(source.read_bytes(), before)
            empty = root / 'empty.mp4'
            empty.write_bytes(struct.pack('>I4s', 8, b'mdat'))
            self.assertEqual(r.inspect(empty)['status'], 'empty')
            bad = root / 'truncated.mp4'; bad.write_bytes(b'broken')
            self.assertEqual(r.inspect(bad)['status'], 'unsupported')

    def test_http_guards_and_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous_dir, previous_log = r.applog.DATA_DIR, r.applog.LOG_DIR
            r.applog.DATA_DIR = Path(tmp)
            r.applog.LOG_DIR = Path(tmp) / 'logs'
            try:
                import web_app as w
                server = w.ThreadingHTTPServer(('127.0.0.1', 0), w.Handler)
                t = threading.Thread(target=server.serve_forever, daemon=True); t.start()
                def request(path, auth=True):
                    req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}'+path,
                        data=b'{}', headers={'X-App-Token': w.TOKEN if auth else 'invalid'})
                    try:
                        with urllib.request.urlopen(req) as response: return response.status
                    except urllib.error.HTTPError as exc: return exc.code
                try:
                    self.assertEqual(request('/repair', False), 403)
                    with r._guard: r.job['running'] = True
                    self.assertTrue(w.working())
                    for path in ['/start', '/delete', '/quick-delete', '/sd/start', '/retime', '/update', '/folder-tags']:
                        self.assertEqual(request(path), 400, path)
                    self.assertEqual(request('/repair'), 200)
                finally:
                    with r._guard: r.job['running'] = False
                    server.shutdown(); server.server_close(); t.join()
            finally:
                r.applog.DATA_DIR, r.applog.LOG_DIR = previous_dir, previous_log


if __name__ == '__main__': unittest.main()
