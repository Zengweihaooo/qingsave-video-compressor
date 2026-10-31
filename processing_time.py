"""Measured processing duration and estimates; never count cancelled jobs as pending."""
import json
import time
import statistics
from pathlib import Path
import applog

class Estimator:
    def __init__(self):
        self.phase = None
        self.baseline = None

    def measure(self, items, progress, paused=False, now=None):
        now = time.time() if now is None else now
        running = [i for i in items if i.get('status') == 'running']
        active = next(iter(running), None)
        pending = [i for i in items if i.get('status') == 'pending']
        attempt = max(0, now - active['started_at']) if active and active.get('started_at') else 0
        elapsed = sum(max(0, i.get('elapsed_seconds') or 0) for i in items) + sum(max(0,now-i['started_at']) for i in running if i.get('started_at'))
        phase_eta = None
        if active and not paused and '等待' not in progress.get('phase', ''):
            key = (active['id'], progress.get('phase'))
            byte_total = progress.get('bytes_total') or 0
            value = (progress.get('bytes_done') or 0) if byte_total else (progress.get('current_seconds') or 0)
            total = byte_total or progress.get('duration') or 0
            if key != self.phase or (self.baseline and value < self.baseline[1]):
                self.phase, self.baseline = key, (now, value)
            baseline = self.baseline
            if baseline and now-baseline[0] >= 2 and value > baseline[1] and total:
                phase_eta = max(0, (total-value) * (now-baseline[0]) / (value-baseline[1]))
        else:
            self.phase, self.baseline = None, None
        def predict(item):
            samples = [i for i in items if i.get('status') == 'completed' and i.get('source_size')
                       and i.get('attempt_seconds') and i.get('video') == item.get('video')
                       and (not item.get('engine') or i.get('engine') == item.get('engine'))]
            if not samples or not item.get('source_size'): return None
            return statistics.median(i['attempt_seconds']/i['source_size'] for i in samples) * item['source_size']
        queue_eta = 0
        unknown = paused or bool(active and '等待' in progress.get('phase', ''))
        current_eta = None
        for item in running + pending:
            estimate = predict(item)
            if estimate is None: unknown = True; continue
            if item in running:
                spent=max(0,now-item['started_at']) if item.get('started_at') else 0
                estimate = max((phase_eta or 0) if item is active else 0, estimate-spent, 0)
                if item is active: current_eta = estimate
            queue_eta += estimate
        return {'current_elapsed': attempt, 'processing_elapsed': elapsed, 'phase_eta': phase_eta,
                'current_eta': None if paused else current_eta,
                'queue_eta': None if unknown else queue_eta,
                'eta_basis': '根据已完成视频的大小、档位和编码器保守估算；并行可能更快。累计处理时长为各任务用时之和；不估算等待空间的时间'}

def restore(items):
    """Recover the current pre-feature job's completed duration from existing event logs."""
    latest = {}
    for path in sorted(applog.LOG_DIR.glob('events-*.jsonl'))[-3:]:
        try:
            for line in path.read_text(encoding='utf8').splitlines():
                try:
                    event=json.loads(line)
                    if event.get('kind')=='compress': latest[event.get('source')]=event
                except ValueError: pass
        except OSError: pass
    for item in items:
        event=latest.get(item.get('source'), {})
        if not item.get('elapsed_seconds') and event.get('status') == item.get('status'):
            item['elapsed_seconds']=event.get('seconds', 0)
            item['attempt_seconds']=event.get('seconds', 0)
        if not item.get('source_size'):
            try:item['source_size']=Path(item['source']).stat().st_size
            except OSError:item['source_size']=event.get('source_size', 0)
