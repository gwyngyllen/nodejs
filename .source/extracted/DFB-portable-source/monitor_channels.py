"""Configurable channel peak tracking. One socket; save each acquired spectrum."""
import csv
import json
import queue
from datetime import datetime
import tkinter as tk
import threading
from dataclasses import replace
from pathlib import Path
from tkinter import ttk, messagebox
from monitor_live import RecordingMonitor
from peak_search import ChannelTracker, SearchConfig


class ChannelMonitor(RecordingMonitor):
    CHANNEL_COLORS=('#60a5fa','#fb923c','#4ade80','#f87171','#c084fc',
                    '#facc15','#f472b6','#2dd4bf','#a3e635','#e2e8f0')

    def __init__(self,root):
        self.tracker=ChannelTracker()
        self.channel_events=queue.Queue(maxsize=1)
        self.display_results={}
        self.config_initialized=False
        self.reidentify_requested=threading.Event()
        self.config_lock=threading.Lock()
        self.pending_channels=None
        self.pending_distance=None
        self.channel_epoch=1
        # Preserve identities across an application restart, including v1
        # sessions which only persisted anchors and the per-channel CSV.
        latest=Path('recordings/latest.txt')
        if latest.exists():
            prior=Path(latest.read_text(encoding='utf-8').strip())
            config=prior/'channel_config.json'
            if config.exists():
                saved=json.loads(config.read_text(encoding='utf-8'))
                settings=dict(saved.get('config',{}))
                migrated=settings.get('min_distance_nm',0.3)<0.3
                if migrated:settings['min_distance_nm']=0.3
                self.tracker=ChannelTracker(SearchConfig(**settings))
                anchors=[] if migrated else saved.get('initial_channel_wavelengths_nm',[])
                self.channel_epoch=saved.get('channel_epoch',1)+(1 if migrated else 0)
                if len(anchors)==self.tracker.config.channels:
                    self.tracker.anchors=anchors
                    self.tracker.previous=anchors[:]
                    if (prior/'channel_peaks.csv').exists():
                        with (prior/'channel_peaks.csv').open(encoding='utf-8-sig') as f:
                            for row in csv.DictReader(f):
                                if int(row.get('channel_epoch') or 1)!=saved.get('channel_epoch',1):
                                    continue
                                if row['status']=='ok' and row.get('peak_nm'):
                                    self.tracker.previous[int(row['channel'][2:])-1]=float(row['peak_nm'])
        # Pack before the base connection controls so search settings stay at top.
        self.channel_controls=ttk.Frame(root,padding=(10,8))
        self.channel_controls.pack(fill='x')
        super().__init__(root)
        root.title('AQ6370E 多通道动态寻峰监控')
        root.geometry('1250x900')
        self.requested_channels=self.tracker.config.channels
        self.channel_count=tk.StringVar(value=str(self.requested_channels))
        self.peak_distance=tk.StringVar(value=str(self.tracker.config.min_distance_nm))
        controls=self.channel_controls
        ttk.Label(controls,text='监控通道数量（1–100）').pack(side='left')
        ttk.Spinbox(controls,from_=1,to=100,textvariable=self.channel_count,width=6).pack(side='left',padx=5)
        ttk.Label(controls,text='最小通道间距（≥0.3 nm）').pack(side='left',padx=5)
        ttk.Spinbox(controls,from_=0.30,to=100,increment=0.01,textvariable=self.peak_distance,width=7).pack(side='left')
        ttk.Button(controls,text='重新搜索通道',command=self.request_reidentify).pack(side='left',padx=5)
        self.channel_status=tk.StringVar(value=f'等待首帧识别 {self.requested_channels} 个通道')
        ttk.Label(root,textvariable=self.channel_status).pack(fill='x')
        table_frame=ttk.Frame(root)
        table_frame.pack(fill='x',padx=10,pady=5)
        self.table=ttk.Treeview(table_frame,columns=('status','nm','dbm','prom'),show='tree headings',height=10)
        self.channel_scrollbar=ttk.Scrollbar(table_frame,orient='vertical',command=self.table.yview)
        self.table.configure(yscrollcommand=self.channel_scrollbar.set)
        self.channel_scrollbar.pack(side='right',fill='y')
        self.table.heading('#0',text='通道（按初始波长排序）')
        for k,label in [('status','状态'),('nm','峰顶波长 / nm'),('dbm','峰值功率 / dBm'),('prom','局部突出度 / dB')]:
            self.table.heading(k,text=label);self.table.column(k,width=180,anchor='center')
        self.reset_table(self.requested_channels)
        self.table.pack(side='left',fill='both',expand=True)
        root.after(250,self.poll_channels)

    def draw(self):
        super().draw()
        if not self.data or not hasattr(self,'plot_bounds'):
            return
        # Use the result belonging to this exact displayed spectrum, never
        # the newest worker frame or an old/fixed-wavelength peak position.
        result=self.display_results.get(id(self.data))
        if not result or (hasattr(self,'requested_channels') and result.get('channel_count',10)!=self.requested_channels):
            return
        left,top,right,bottom,xmin,xmax,ymin,ymax=self.plot_bounds
        c=self.canvas
        for p in result['channels']:
            if p['status']!='ok' or p['peak_nm'] is None:
                continue
            number=int(p['channel'][2:])
            if number<1:
                continue
            color=self.CHANNEL_COLORS[(number-1)%len(self.CHANNEL_COLORS)]
            px=left+(p['peak_nm']-xmin)/(xmax-xmin)*(right-left)
            py=bottom-(p['peak_dbm']-ymin)/(ymax-ymin)*(bottom-top)
            if not left<=px<=right or not top<=py<=bottom:
                continue
            label_y=max(top+12,py-25)
            tag=('channel_marker',p['channel'])
            c.create_line(px,label_y+9,px,py-6,fill=color,dash=(3,2),tags=tag)
            c.create_polygon(px,py,px-5,py-8,px+5,py-8,
                             fill=color,outline='#111827',tags=tag)
            c.create_rectangle(px-14,label_y-10,px+14,label_y+10,
                               fill='#111827',outline=color,tags=tag)
            c.create_text(px,label_y,text=str(number),fill=color,
                          font=('Segoe UI',11,'bold'),tags=(*tag,'channel_number'))

    def on_spectrum(self,data):
        with self.config_lock:
            count=self.pending_channels
            self.pending_channels=None
            distance=self.pending_distance
            self.pending_distance=None
            reset=self.reidentify_requested.is_set()
            self.reidentify_requested.clear()
        if count is not None or distance is not None or reset:
            config=replace(self.tracker.config,
                channels=count if count is not None else self.tracker.config.channels,
                min_distance_nm=distance if distance is not None else self.tracker.config.min_distance_nm)
            self.tracker=ChannelTracker(config)
            self.channel_epoch+=1
            self.config_initialized=False
            # Persist the requested count even when too few peaks are present.
            metadata=dict(self.tracker.metadata(),channel_epoch=self.channel_epoch)
            for name in ('channel_config.json',f'channel_config_epoch_{self.channel_epoch}.json'):
                (self.folder/name).write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
        previous_count=len(self.rows)
        super().on_spectrum(data)
        if self.save_current_spectrum and len(self.rows)==previous_count:
            raise OSError('Raw spectrum was not saved; channel recording stopped')
        x,y,unit,_,_=data
        result=self.tracker.update(x,y,unit)
        stamp=self.rows[-1]['timestamp'] if self.save_current_spectrum else datetime.now().isoformat(sep=' ',timespec='milliseconds')
        import time
        elapsed=self.rows[-1]['elapsed_s'] if self.save_current_spectrum else time.monotonic()-self.started
        result.update(timestamp=stamp,elapsed_s=elapsed,channel_epoch=self.channel_epoch,channel_count=self.tracker.config.channels)
        fields=['timestamp','elapsed_s','channel_epoch','channel','status','reason','anchor_nm','peak_nm','peak_dbm','sampled_nm','sampled_dbm','prominence_db','fit_valid']
        if self.save_current_spectrum:
            path=self.folder/'channel_peaks.csv'
            new=not path.exists()
            with path.open('a',newline='',encoding='utf-8-sig') as f:
                writer=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore')
                if new:writer.writeheader()
                for p in result['channels']:writer.writerow(dict(timestamp=stamp,elapsed_s=result['elapsed_s'],channel_epoch=self.channel_epoch,**p))
            with (self.folder/'channel_diagnostics.jsonl').open('a',encoding='utf-8') as f:
                f.write(json.dumps({k:v for k,v in result.items() if k!='channels'},ensure_ascii=False)+'\n')
        (self.folder/'channel_status.json').write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
        if self.tracker.anchors and not self.config_initialized:
            metadata=dict(self.tracker.metadata(),channel_epoch=self.channel_epoch)
            (self.folder/'channel_config.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
            (self.folder/f'channel_config_epoch_{self.channel_epoch}.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
            self.config_initialized=True
        self.display_results[id(data)]=result
        if len(self.display_results)>20:self.display_results.pop(next(iter(self.display_results)))
        if self.channel_events.full():
            try:self.channel_events.get_nowait()
            except queue.Empty:pass
        self.channel_events.put_nowait(result)

    def poll_channels(self):
        # Refresh the table together with the 2-second spectrum display.
        result=self.display_results.get(id(self.data))
        if result and result.get('channel_count',10)==self.requested_channels:
            labels={'ok':'已跟踪','not_detected':'未检测到有效峰','lost_lock':'通道失锁',
                    'ambiguous_identity':'通道身份歧义','reacquiring':'重捕获确认中',
                    'tracking':'正常跟踪','reacquired':'已重新捕获','partial_detection':'部分通道未检出',
                    'no_peaks':'未检测到有效峰','waiting_for_peaks':f'等待识别 {self.requested_channels} 个峰'}
            for p in result['channels']:
                self.table.item(p['channel'],values=(labels.get(p['status'],p['status']),f"{p['peak_nm']:.5f}" if p['peak_nm'] is not None else '—',
                    f"{p['peak_dbm']:.4f}" if p['peak_dbm'] is not None else '—',
                    f"{p['prominence_db']:.2f}" if p['prominence_db'] is not None else ''))
            shifts=result.get('shift_hypotheses_nm',[])
            hint=' | 整组位移候选 '+', '.join(f'{s:+.4f} nm' for s in shifts[:3]) if shifts else ''
            if result['status']=='waiting_for_peaks':
                hint+=f' | 需要 {self.requested_channels} 个峰；请核对最小峰间距、扫描范围和功率阈值'
            self.channel_status.set(f"{result['timestamp']} | {labels.get(result['status'],result['status'])} | 候选峰 {result['candidate_count']} | 编号段 {result['channel_epoch']}"+hint)
        self.root.after(250,self.poll_channels)

    def request_reidentify(self):
        if not self.apply_channel_count():return False
        with self.config_lock:
            self.reidentify_requested.set()
        self.channel_status.set(f'等待下一帧：按最小间距 {self.peak_distance.get()} nm 搜索最强 {self.requested_channels} 个峰，建立新编号段')
        return True

    def reset_table(self,count):
        children=self.table.get_children()
        if children:self.table.delete(*children)
        self.table.configure(height=min(count,10))
        for i in range(count):
            name=f'CH{i+1:02d}'
            self.table.insert('', 'end',iid=name,text=name)

    def apply_channel_count(self):
        try:
            count=int(self.channel_count.get())
            distance=float(self.peak_distance.get())
            SearchConfig(channels=count,min_distance_nm=distance)
        except ValueError:
            messagebox.showerror('参数错误','通道数量必须为 1–100 的整数，最小通道间距必须不小于 0.3 nm 且为有限数值')
            return False
        with self.config_lock:
            current_distance=self.pending_distance if self.pending_distance is not None else self.tracker.config.min_distance_nm
            if count==self.requested_channels and distance==current_distance:return True
            self.pending_channels=count
            self.pending_distance=distance
        self.requested_channels=count
        self.reset_table(count)
        self.channel_status.set(f'已设置 {count} 个通道，最小峰间距 {distance:g} nm；下一帧重新识别并建立新编号段')
        return True


if __name__=='__main__':
    root=tk.Tk();ChannelMonitor(root);root.mainloop()
