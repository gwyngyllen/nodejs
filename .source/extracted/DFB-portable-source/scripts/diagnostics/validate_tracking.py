"""Replay a session to audit v2 lock/reacquisition states without touching acquisition."""
import os
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
os.chdir(ROOT)
import csv
import gzip
import json
from collections import Counter
from pathlib import Path
from peak_search import ChannelTracker

if __name__=='__main__':
    folder=Path('recordings/20261015_174701')
    rows=list(csv.DictReader((folder/'metrics.csv').open(encoding='utf-8-sig')))
    tracker=ChannelTracker();counts=Counter();transitions=[];previous=None
    for i,row in enumerate(rows):
        d=json.load(gzip.open(folder/f'spectrum_{i:05d}.json.gz','rt',encoding='utf-8'))
        r=tracker.update(d['wavelength_nm'],d['power'],d['unit'])
        counts[r['status']]+=1
        if r['status']!=previous:
            transitions.append(dict(timestamp=d['timestamp'],status=r['status'],
                                    shift_hypotheses_nm=r.get('shift_hypotheses_nm',[])))
            previous=r['status']
    result=dict(source=str(folder),frames=len(rows),states=dict(counts),transitions=transitions,
                final=r,metadata=tracker.metadata())
    Path('output/tracking_v2_validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(frames=len(rows),states=dict(counts),last_status=r['status'],
                         last_shifts=r.get('shift_hypotheses_nm',[])),ensure_ascii=False))
