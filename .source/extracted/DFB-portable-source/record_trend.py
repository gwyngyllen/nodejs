"""One-hour OSA recording with incremental, recoverable trend reports."""
import argparse
import csv
import gzip
import hashlib
import html
import json
import math
import statistics
import time
from datetime import datetime
from pathlib import Path
from main import OSA


def metrics(x, y, unit, baseline):
    peak = max(range(len(y)), key=y.__getitem__)
    same_grid = x == baseline[0] and unit == baseline[2]
    rms = math.sqrt(statistics.mean((a-b)**2 for a,b in zip(y, baseline[1]))) if same_grid else None
    fingerprint = hashlib.sha256(json.dumps([x,y,unit],separators=(',',':')).encode()).hexdigest()
    return {'peak_nm':x[peak], 'peak_power':y[peak], 'shape_rms':rms, 'fingerprint':fingerprint, 'grid_matches':same_grid}


def slope(rows, field):
    valid = [(r['elapsed_s']/3600, r[field]) for r in rows if r[field] is not None]
    if len(valid)<2: return 0.0
    mx,my = statistics.mean(a for a,b in valid),statistics.mean(b for a,b in valid)
    den = sum((a-mx)**2 for a,b in valid)
    return sum((a-mx)*(b-my) for a,b in valid)/den if den else 0.0


def plot(rows, field, label):
    points = [(r['elapsed_s']/60, r[field]) for r in rows if r[field] is not None]
    if not points: return '<p>No comparable wavelength grids.</p>'
    xmin,xmax = 0,max(1,points[-1][0])
    ymin,ymax = min(y for x,y in points),max(y for x,y in points)
    pad = max((ymax-ymin)*.1,1e-6)
    ymin,ymax = ymin-pad,ymax+pad
    coords = ' '.join(f'{65+x/xmax*800:.2f},{220-(y-ymin)/(ymax-ymin)*175:.2f}' for x,y in points)
    return f'<h3>{html.escape(label)}</h3><svg viewBox="0 0 900 260" style="width:100%;max-width:1000px;background:#f8fafc"><path d="M65 30V220H865" fill="none" stroke="#64748b"/><polyline points="{coords}" fill="none" stroke="#0284c7" stroke-width="1.5"/><text x="5" y="42">{ymax:.6g}</text><text x="5" y="220">{ymin:.6g}</text><text x="65" y="246">0 min</text><text x="800" y="246">{xmax:.2f} min</text></svg>'


def report(folder, rows, unit, status, errors):
    if not rows: return
    duration = rows[-1]['elapsed_s']
    first,last = rows[0],rows[-1]
    duplicates = sum(a['fingerprint']==b['fingerprint'] for a,b in zip(rows,rows[1:]))
    summary = {'status':status, 'samples':len(rows), 'observed_seconds':duration, 'unit':unit, 'errors':errors,
               'peak_wavelength_change_nm':last['peak_nm']-first['peak_nm'], 'peak_wavelength_slope_nm_per_hour':slope(rows,'peak_nm'),
               'peak_power_change':last['peak_power']-first['peak_power'], 'peak_power_slope_per_hour':slope(rows,'peak_power'),
               'peak_wavelength_range_nm':[min(r['peak_nm'] for r in rows),max(r['peak_nm'] for r in rows)],
               'peak_power_range':[min(r['peak_power'] for r in rows),max(r['peak_power'] for r in rows)], 'identical_adjacent_snapshots':duplicates}
    (folder/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    body = '<html><meta charset="utf-8"><title>OSA 光谱趋势</title><body style="font-family:Arial;max-width:1100px;margin:30px auto"><h1>一小时光谱趋势</h1>'
    body += f'<p>状态：{html.escape(status)}；{len(rows)} 个快照；实际覆盖 {duration/60:.2f} 分钟；读取失败 {errors} 次。</p>'
    body += f'<p>峰值波长首末变化 {summary["peak_wavelength_change_nm"]:+.6f} nm；线性拟合斜率 {summary["peak_wavelength_slope_nm_per_hour"]:+.6f} nm/h。峰值功率首末变化 {summary["peak_power_change"]:+.6g} {html.escape(unit)}。</p>'
    body += '<p>采样点峰值未做亚采样插值。曲线 RMS 为与首个快照在同一波长网格上的逐点差；网格或单位改变则不比较。连续扫描快照可能包含尚未完成的扫描。拟合斜率描述趋势，不证明变化原因。密度单位沿用仪器设置。</p>'
    body += f'<p>相邻完全相同快照 {duplicates} 对：重复快照可能来自扫描间隔或未开启 REPEAT，不能据此断言光源稳定。</p>'
    for field,label in [('peak_nm','峰值波长 (nm)'),('peak_power','峰值功率 ('+unit+')'),('shape_rms','相对初始光谱 RMS ('+unit+')')]:
        body += plot(rows,field,label)
    (folder/'report.html').write_text(body+'</body></html>',encoding='utf-8')
    temporary = folder/'status.tmp'
    temporary.write_text(json.dumps(summary,ensure_ascii=False),encoding='utf-8')
    try:
        temporary.replace(folder/'status.json')
    except PermissionError:
        # Windows may temporarily lock a status file while another reader opens it.
        # A status refresh must never terminate acquisition of the spectra.
        try:
            (folder/'status.json').write_text(json.dumps(summary,ensure_ascii=False),encoding='utf-8')
        except OSError:
            pass


def run(args):
    folder = Path('recordings')/datetime.now().strftime('%Y%m%d_%H%M%S')
    folder.mkdir(parents=True,exist_ok=False)
    (Path('recordings')/'latest.txt').write_text(str(folder.resolve()),encoding='utf-8')
    rows,baseline,osa = [],None,None
    errors = 0
    unit = ''
    start = None
    (folder/'status.json').write_text(json.dumps({'status':'等待仪器连接','folder':str(folder.resolve())},ensure_ascii=False),encoding='utf-8')
    print('Recording folder:',folder,flush=True)
    try:
        with open(folder/'metrics.csv','w',newline='',encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f,fieldnames=['timestamp','elapsed_s','read_duration_s','unit','peak_nm','peak_power','shape_rms','fingerprint','grid_matches'])
            writer.writeheader()
            wait_deadline = time.monotonic()+args.wait
            while True:
                if start is not None and time.monotonic()-start >= args.seconds: break
                try:
                    if osa is None:
                        osa = OSA(args.host,args.port,'anonymous','')
                        (folder/'instrument.txt').write_text(osa.identity,encoding='utf-8')
                    tick = time.monotonic()
                    x,y,current_unit = osa.spectrum(args.trace)
                    now = time.monotonic()
                    if start is None:
                        start = now
                        baseline = (x,y,current_unit)
                        unit = current_unit
                    row = {'timestamp':datetime.now().isoformat(timespec='milliseconds'),'elapsed_s':now-start,'read_duration_s':now-tick,'unit':current_unit,**metrics(x,y,current_unit,baseline)}
                    with gzip.open(folder/f'spectrum_{len(rows):05d}.json.gz','wt',encoding='utf-8') as raw:
                        json.dump({'timestamp':row['timestamp'],'trace':args.trace,'unit':current_unit,'wavelength_nm':x,'power':y},raw)
                    rows.append(row)
                    writer.writerow(row)
                    f.flush()
                    report(folder,rows,unit,'记录中',errors)
                    print(f'{row["elapsed_s"]:.1f}s peak {row["peak_nm"]:.5f} nm {row["peak_power"]:.4f} {current_unit}',flush=True)
                    time.sleep(min(args.interval,max(0,args.seconds-(time.monotonic()-start))))
                except (OSError,RuntimeError,ValueError) as exc:
                    errors += 1
                    with open(folder/'errors.log','a',encoding='utf-8') as err:
                        err.write(f'{datetime.now().isoformat()} {exc}\n')
                    if osa: osa.close()
                    osa = None
                    if start is None and time.monotonic() > wait_deadline: raise
                    if rows: report(folder,rows,unit,'连接中断，正在重试',errors)
                    print('Retry:',exc,flush=True)
                    time.sleep(5)
        report(folder,rows,unit,'一小时记录结束' if args.seconds==3600 else '记录结束',errors)
    except BaseException:
        if rows: report(folder,rows,unit,'提前终止（不完整）',errors)
        else: (folder/'status.json').write_text(json.dumps({'status':'连接失败，未采集数据','errors':errors},ensure_ascii=False),encoding='utf-8')
        raise
    finally:
        if osa: osa.close()

if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--host',default='10.200.5.10')
    parser.add_argument('--port',type=int,default=10001)
    parser.add_argument('--trace',default='TRA',choices=['TR'+v for v in 'ABCDEFG'])
    parser.add_argument('--interval',type=float,default=5)
    parser.add_argument('--seconds',type=float,default=3600)
    parser.add_argument('--wait',type=float,default=300)
    args = parser.parse_args()
    if args.interval<=0 or args.seconds<=0: parser.error('duration and interval must be positive')
    run(args)
