"""Bound concurrent encoding/checking and serialize full-file hashing."""
import threading
from contextlib import contextmanager
import applog

DEFAULTS={'jobs':3,'encoders':1,'checkers':1,'verification':'gpu'}
settings=dict(DEFAULTS)
condition=threading.Condition()
active={'encode':0,'check':0,'hash':0}

def configure(data=None, persist=True):
    data = data or {}
    value={**settings,**data}
    for key,maximum in (('jobs',4),('encoders',3),('checkers',2)):
        if isinstance(value[key],bool) or not isinstance(value[key],int) or not 1<=value[key]<=maximum:
            raise ValueError('并行数量超出允许范围')
    if value['encoders']>value['jobs'] or value['checkers']>value['jobs']:
        raise ValueError('编码或检查数量不能超过同时处理的视频数')
    if value['verification'] not in ('cpu','gpu'):raise ValueError('无效的检查方式')
    with condition:
        settings.update({key:value[key] for key in DEFAULTS});condition.notify_all()
    if persist:applog.save_json('pipeline-settings.json',settings)
    return dict(settings)

def initialize():
    configure(applog.load_json('pipeline-settings.json',DEFAULTS),persist=False)

@contextmanager
def slot(kind,cancel=None):
    with condition:
        while active[kind] >= (1 if kind=='hash' else settings['encoders' if kind=='encode' else 'checkers']):
            if cancel and cancel.is_set():raise RuntimeError('已取消等待处理资源')
            condition.wait(.2)
        if cancel and cancel.is_set():raise RuntimeError('已取消')
        active[kind]+=1
    try:yield
    finally:
        with condition:active[kind]-=1;condition.notify_all()

def command_kind(args):
    if '-c:v' in args and args[args.index('-c:v')+1]!='copy':return 'encode'
    if '-f' in args and args[args.index('-f')+1]=='null':return 'check'
    return None
