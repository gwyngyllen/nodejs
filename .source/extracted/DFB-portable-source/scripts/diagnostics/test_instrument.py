"""Connect to the actual instrument and save a summary, without raw spectra."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import socket
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--host',default='10.200.5.10')
    parser.add_argument('--port',type=int,default=10001)
    parser.add_argument('--channels',type=int,default=16)
    args=parser.parse_args()
    result=dict(time=datetime.now().isoformat(),host=args.host,port=args.port,
                success=False,stage='tcp_connect')
    started=time.monotonic()
    device=None
    try:
        # Use the production handshake on a single connection, without probing
        # a second socket that could occupy the instrument control session.
        from main import OSA
        from peak_search import ChannelTracker, SearchConfig
        device=OSA(args.host,args.port,'anonymous','')
        result.update(stage='spectrum_read',identity=device.identity)
        tracker=ChannelTracker(SearchConfig(channels=args.channels))
        result['frames']=[]
        for _ in range(3):
            x,y,unit=device.spectrum('TRA')
            tracked=tracker.update(x,y,unit)
            result['frames'].append(dict(points=len(x),unit=unit,
                wavelength_range_nm=[min(x),max(x)],power_range=[min(y),max(y)],
                candidate_count=tracked['candidate_count'],status=tracked['status'],
                channels=tracked['channels']))
        result.update(stage='complete',success=True)
    except Exception as exc:
        result.update(error_type=type(exc).__name__,error=str(exc))
        cause=exc
        while cause.__cause__ is not None:cause=cause.__cause__
        result['root_error']=str(cause)
        result['winerror']=getattr(cause,'winerror',None)
    finally:
        if device is not None:device.close()
    result['elapsed_seconds']=round(time.monotonic()-started,3)
    folder=ROOT/'tmp/diagnostics'
    folder.mkdir(parents=True,exist_ok=True)
    path=folder/f'connection_{args.host}.json'
    path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True,indent=2),flush=True)
    print(f'Report: {path}',flush=True)
    return 0 if result['success'] else 1


if __name__=='__main__':sys.exit(main())
