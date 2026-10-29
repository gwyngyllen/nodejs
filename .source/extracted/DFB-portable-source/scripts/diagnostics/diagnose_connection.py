"""Read-only LAN diagnostics. Run with python -m pdb diagnose_connection.py."""
import concurrent.futures
import os
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
os.chdir(ROOT)
import json
import socket
from datetime import datetime
from main import OSA
HOST = '192.168.1.100'
def probe(port):
    try:
        with socket.create_connection((HOST, port), timeout=3):
            return port, 'OPEN'
    except OSError as exc:
        return port, f'{type(exc).__name__}: {exc}'
def main():
    results = {'time':str(datetime.now()), 'host':HOST}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results['ports'] = dict(pool.map(probe, [10001,20001,111,80]))
    print(json.dumps(results,ensure_ascii=False,indent=2))
    if results['ports'][10001] == 'OPEN':
        osa = None
        try:
            osa = OSA(HOST,10001,'anonymous','')
            results['identity'] = osa.identity
            x,y,u = osa.spectrum('TRA')
            results['spectrum'] = {'points':len(x), 'range_nm':[min(x),max(x)], 'peak_nm':x[y.index(max(y))], 'peak_level':max(y), 'unit':u}
        except Exception as exc:
            results['error'] = repr(exc)
        finally:
            if osa: osa.close()
    output=ROOT/'tmp'/'diagnostics'/'connection_diagnostics.json'
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('w',encoding='utf-8') as f:
        json.dump(results,f,ensure_ascii=False,indent=2)
    print('Saved',output)
if __name__ == '__main__': main()
