"""Freeze all usable afternoon spectra and audit them with the current v2 tracker."""
import os
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
os.chdir(ROOT)
import csv
import gzip
import json
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path
from peak_search import ChannelTracker, search_peaks

OUT=Path('output/pdf/afternoon_summary_20261015')
BAD_ID={'lost_lock','ambiguous_identity','reacquiring'}


def stamp(s):return datetime.fromisoformat(s)


def analyze():
    OUT.mkdir(parents=True,exist_ok=True)
    manifest=[]
    for folder in sorted(Path('recordings').glob('20261015_*')):
        path=folder/'metrics.csv'
        if not path.exists():continue
        rows=list(csv.DictReader(path.open(encoding='utf-8-sig')))
        if not rows:continue
        manifest.append((folder,rows))
    (OUT/'input_manifest.json').write_text(json.dumps([dict(folder=str(f),frames=len(rows),
        start=rows[0]['timestamp'],end=rows[-1]['timestamp']) for f,rows in manifest],ensure_ascii=False,indent=2),encoding='utf-8')
    sessions=[];all_frames=[];events=[];channel_stats=[];gaps=[]
    channel_fields=['session','frame','timestamp','channel','status','reason','peak_nm','peak_dbm','sampled_nm','sampled_dbm','prominence_db','fit_valid']
    with (OUT/'all_channel_peaks.csv').open('w',newline='',encoding='utf-8-sig') as output:
        writer=csv.DictWriter(output,fieldnames=channel_fields,extrasaction='ignore');writer.writeheader()
        for folder,rows in manifest:
            tracker=ChannelTracker()
            config_path=folder/'channel_config.json'
            if config_path.exists():
                config=json.loads(config_path.read_text(encoding='utf-8'))
                anchors=config.get('initial_channel_wavelengths_nm',[])
                if len(anchors)==10:tracker.anchors=anchors;tracker.previous=anchors[:]
            times=[stamp(r['timestamp']) for r in rows]
            diffs=[(b-a).total_seconds() for a,b in zip(times,times[1:])]
            median=statistics.median(diffs) if diffs else 0
            gap_limit=max(3,median*5)
            coverage=sum(d for d in diffs if d<=gap_limit)
            states=Counter();records=[];grid=None;invalid=[];duplicates=0;previous_fingerprint=None
            for i,(row,when) in enumerate(zip(rows,times)):
                try:
                    with gzip.open(folder/f'spectrum_{i:05d}.json.gz','rt',encoding='utf-8') as f:d=json.load(f)
                    if d['timestamp']!=row['timestamp'] or d['unit']!='dBm' or d['trace']!='TRA':raise ValueError('timestamp/unit/trace mismatch')
                    if grid is None:grid=d['wavelength_nm']
                    if grid!=d['wavelength_nm']:raise ValueError('wavelength grid changed')
                    if i and diffs[i-1]>gap_limit:
                        gaps.append(dict(session=folder.name,start=times[i-1].isoformat(sep=' '),end=when.isoformat(sep=' '),seconds=diffs[i-1]))
                        tracker.pending_count=0;tracker.pending_shift=None
                    result=tracker.update(d['wavelength_nm'],d['power'],d['unit'])
                    if row['fingerprint']==previous_fingerprint:duplicates+=1
                    previous_fingerprint=row['fingerprint']
                    record=dict(session=folder.name,index=i,timestamp=d['timestamp'],state=result['status'],
                        candidates=result['candidate_count'],shifts=result.get('shift_hypotheses_nm',[]),channels=result['channels'])
                    records.append(record);all_frames.append(record);states[result['status']]+=1
                    for p in result['channels']:writer.writerow(dict(session=folder.name,frame=i,timestamp=d['timestamp'],**p))
                except (OSError,ValueError,KeyError,EOFError) as exc:
                    invalid.append(dict(index=i,error=str(exc)))
                if i%2000==0:print(folder.name,i,flush=True)
            # Event = consecutive loss of identity; split at state recovery or acquisition gaps.
            event=None
            for j,r in enumerate(records):
                previous=records[j-1] if j else None
                delta=(stamp(r['timestamp'])-stamp(previous['timestamp'])).total_seconds() if previous else 0
                if event and (r['state'] not in BAD_ID or delta>gap_limit):
                    event['recovery']=r['timestamp'] if r['state'] not in BAD_ID and delta<=gap_limit else None
                    event['right_censored']=event['recovery'] is None
                    events.append(event);event=None
                if r['state'] in BAD_ID:
                    if event is None:event=dict(session=folder.name,start=r['timestamp'],end=r['timestamp'],frames=0,
                        observed_seconds=0.,states={},first_index=r['index'],last_index=r['index'],
                        hypotheses=[],min_candidates=r['candidates'],max_candidates=r['candidates'])
                    if event['frames'] and delta<=gap_limit:event['observed_seconds']+=delta
                    event['frames']+=1;event['end']=r['timestamp'];event['last_index']=r['index']
                    event['states'][r['state']]=event['states'].get(r['state'],0)+1
                    event['min_candidates']=min(event['min_candidates'],r['candidates']);event['max_candidates']=max(event['max_candidates'],r['candidates'])
                    if r['shifts'] and len(event['hypotheses'])<5:event['hypotheses'].append(r['shifts'])
            if event:event.update(recovery=None,right_censored=True);events.append(event)
            duration_by_state=Counter()
            for a,b in zip(records,records[1:]):
                dt=(stamp(b['timestamp'])-stamp(a['timestamp'])).total_seconds()
                if dt<=gap_limit:duration_by_state[a['state']]+=dt
            for ch in range(10):
                key=f'CH{ch+1:02d}';good=[]
                for r in records:
                    p=next((p for p in r['channels'] if p['channel']==key),None)
                    if p and p['status']=='ok':good.append((r['timestamp'],p))
                first=[p for ts,p in good if (stamp(ts)-times[0]).total_seconds()<=30]
                last=[p for ts,p in good if (times[-1]-stamp(ts)).total_seconds()<=30]
                def mean(values,field):return statistics.mean(p[field] for p in values) if values else None
                a,b,c,e=mean(first,'peak_nm'),mean(last,'peak_nm'),mean(first,'peak_dbm'),mean(last,'peak_dbm')
                channel_stats.append(dict(session=folder.name,channel=key,valid_frames=len(good),unassigned_frames=len(records)-len(good),
                    initial_nm=a,latest_nm=b,wavelength_change_pm=(b-a)*1000 if a is not None and b is not None else None,
                    initial_dbm=c,latest_dbm=e,power_change_db=e-c if c is not None and e is not None else None,
                    first_window_valid=len(first),last_window_valid=len(last)))
            sessions.append(dict(session=folder.name,start=rows[0]['timestamp'],end=rows[-1]['timestamp'],
                committed_frames=len(rows),valid_frames=len(records),invalid_frames=invalid,median_interval_s=median,
                max_interval_s=max(diffs) if diffs else 0,covered_seconds=coverage,
                span_seconds=(times[-1]-times[0]).total_seconds(),states=dict(states),
                state_observed_seconds=dict(duration_by_state),identical_adjacent_snapshots=duplicates,
                initial_channel_wavelengths_nm=tracker.anchors,grid_points=len(grid),grid_nm=[grid[0],grid[-1]]))
    all_frames.sort(key=lambda r:r['timestamp'])
    for a,b in zip(sessions,sessions[1:]):
        dt=(stamp(b['start'])-stamp(a['end'])).total_seconds()
        if dt>3:gaps.append(dict(session='between_sessions',start=a['end'],end=b['start'],seconds=dt))
    summary=dict(date='2026-10-15',cutoff=all_frames[-1]['timestamp'],start=all_frames[0]['timestamp'],
        sessions=sessions,total_frames=len(all_frames),covered_seconds=sum(s['covered_seconds'] for s in sessions),
        events=events,gaps=gaps,channel_stats=channel_stats,algorithm=ChannelTracker().metadata(),
        screenshot='main_ui.png',screenshot_caption='2026-10-15 18:26:43，正常跟踪状态；非历史失锁时刻截图')
    (OUT/'afternoon_analysis.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    with gzip.open(OUT/'frame_analysis.json.gz','wt',encoding='utf-8') as f:json.dump(all_frames,f,ensure_ascii=False)
    for filename,values in [('session_summary.csv',sessions),('channel_changes_by_session.csv',channel_stats),('lock_events.csv',events),('acquisition_gaps.csv',gaps)]:
        if values:
            with (OUT/filename).open('w',newline='',encoding='utf-8-sig') as f:
                w=csv.DictWriter(f,fieldnames=list(values[0]));w.writeheader();w.writerows(values)
    print(json.dumps(dict(total_frames=len(all_frames),cutoff=summary['cutoff'],covered_minutes=summary['covered_seconds']/60,
        sessions=[{k:s[k] for k in ('session','start','end','valid_frames','states')} for s in sessions],
        events=events),ensure_ascii=False,indent=2))


if __name__=='__main__':analyze()
