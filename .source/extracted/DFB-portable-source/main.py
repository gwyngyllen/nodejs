import csv
import math
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import queue
import socket
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

log = logging.getLogger('osa')
log.setLevel(logging.DEBUG)
if not log.handlers:
    log_folder = Path(__file__).resolve().parent/'tmp'/'logs'
    log_folder.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_folder/'osa_debug.log', maxBytes=1_000_000, backupCount=2, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    log.addHandler(handler)

class OSA:
    def __init__(self, host, port, user, password):
        self.stage = 'TCP 连接'
        if port == 20001:
            raise ValueError('20001 是 Viewer 屏幕监控端口。请使用仪器 REMOTE PORT NO. 指定的 SCPI 控制端口（默认 10001）。')
        log.info('Connecting host=%s port=%s', host, port)
        try:
            self.sock = socket.create_connection((host, port), timeout=10)
        except OSError as exc:
            log.exception('TCP connection failed')
            raise ConnectionError(f'TCP 连接失败 {host}:{port}：{exc}。请检查 NETWORK(SOCKET)、REMOTE PORT NO. 和控制端口开关，并断开其他控制客户端。') from exc
        # AQ6370E manual section 1.3: auto offset can pause replies for ~30 s.
        self.sock.settimeout(60)
        self.buffer = bytearray()
        self.old_format = None
        try:
            self.stage = '用户认证'
            response = self.query(f'OPEN "{user}"')
            if 'AUTHENTICATE' not in response.upper():
                raise RuntimeError(response)
            if self.query(password, sensitive=True).upper() != 'READY':
                raise RuntimeError('Authentication failed')
            self.stage = '仪器识别'
            self.identity = self.query('*IDN?')
            if 'AQ6370E' not in self.identity.upper():
                raise RuntimeError('Unexpected instrument: ' + self.identity)
            self.stage = '传输格式设置'
            self.old_format = self.query(':FORMAT:DATA?')
            self.send(':FORMAT:DATA ASCII')
        except Exception as exc:
            log.error('Initialization failed stage=%s error=%s', self.stage, type(exc).__name__)
            self.sock.close()
            raise RuntimeError(f'{self.stage}失败：{exc}') from exc

    def send(self, command):
        self.sock.sendall((command + '\r\n').encode('ascii'))

    def query(self, command, sensitive=False):
        log.debug('Query %s', '[authentication response redacted]' if sensitive else command)
        self.send(command)
        while b'\n' not in self.buffer:
            part = self.sock.recv(65536)
            if not part:
                raise ConnectionError('Instrument disconnected')
            self.buffer.extend(part)
            if len(self.buffer) > 32_000_000:
                raise RuntimeError('Response too large')
        line, _, rest = self.buffer.partition(b'\n')
        self.buffer = bytearray(rest)
        result = line.decode('ascii').strip()
        log.debug('Response bytes=%d preview=%s', len(line), result[:160] if not sensitive else '[redacted]')
        return result

    def spectrum(self, trace):
        self.stage = '读取光谱'
        unit = self.query(':DISPLAY:TRACE:Y1:UNIT?')
        x = [float(v)*1e9 for v in self.query(f':TRACE:DATA:X? {trace}').split(',')]
        y = [float(v) for v in self.query(f':TRACE:DATA:Y? {trace}').split(',')]
        if len(x) != len(y) or len(x) < 2 or not all(math.isfinite(v) for v in x+y):
            raise ValueError('Invalid spectrum; retry after sweep completes')
        unit = {'0':'dBm', '1':'W', '2':'dBm/nm or dBm/THz', '3':'W/nm or W/THz'}.get(unit, unit)
        return x, y, unit

    def close(self):
        try:
            if self.old_format is not None:
                self.send(':FORMAT:DATA ' + self.old_format)
            self.send('CLOSE')
        except OSError:
            pass
        finally:
            self.sock.close()

class Monitor:
    def __init__(self, root):
        self.root = root
        root.title('AQ6370E LAN 光谱监测')
        root.geometry('1150x700')
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.worker = None
        self.data = None
        self.intervals = (0.5, 2.0)
        self.timing_version = 0
        self.save_current_spectrum = True
        bar = ttk.Frame(root, padding=10)
        bar.pack(fill='x')
        self.fields = {}
        for label, value, width in [('IP','10.200.5.10',16), ('端口','10001',6), ('用户','anonymous',12), ('密码','',10)]:
            ttk.Label(bar, text=label).pack(side='left', padx=3)
            var = tk.StringVar(value=value)
            ttk.Entry(bar, textvariable=var, width=width, show='*' if label=='密码' else '').pack(side='left')
            self.fields[label] = var
        self.trace = tk.StringVar(value='TRA')
        ttk.Combobox(bar, textvariable=self.trace, values=['TR'+v for v in 'ABCDEFG'], width=5, state='readonly').pack(side='left', padx=5)
        self.button = ttk.Button(bar, text='开始监测', command=self.start)
        self.button.pack(side='left', padx=5)
        ttk.Button(bar, text='停止', command=self.stop.set).pack(side='left')
        ttk.Button(bar, text='导出 CSV', command=self.export).pack(side='left', padx=5)
        timing_bar = ttk.Frame(root, padding=(10,0,10,5))
        timing_bar.pack(fill='x')
        self.timing_inputs={}
        for label,value,step in [('记录间隔(s)','0.5',0.5),('UI刷新间隔(s)','2',2.0)]:
            ttk.Label(timing_bar,text=label).pack(side='left',padx=3)
            var=tk.StringVar(value=value)
            spin=ttk.Spinbox(timing_bar,textvariable=var,width=7,
                from_=step,to=86400,increment=step,
                command=lambda:self.apply_intervals(show_error=False))
            spin.pack(side='left',padx=3)
            spin.bind('<Return>',lambda event:self.apply_intervals())
            spin.bind('<FocusOut>',lambda event:self.apply_intervals(show_error=False))
            self.timing_inputs[label]=spin
            self.fields[label]=var
        self.timing_status=tk.StringVar(value='记录 0.5 秒 / UI刷新 2 秒；箭头即时生效，手动输入后回车或移开焦点')
        ttk.Label(timing_bar,textvariable=self.timing_status).pack(side='left',padx=5)
        self.status = tk.StringVar(value='就绪：请在仪器上开启 REPEAT 扫描，然后开始监测。')
        ttk.Label(root, textvariable=self.status, padding=10).pack(fill='x')
        self.canvas = tk.Canvas(root, bg='#111827', highlightthickness=0)
        self.canvas.pack(fill='both', expand=True, padx=10, pady=10)
        self.canvas.bind('<Configure>', lambda e: self.draw())
        self.canvas.bind('<Motion>', self.cursor)
        root.protocol('WM_DELETE_WINDOW', self.quit)
        root.after(100, self.poll)

    def start(self):
        if self.worker and self.worker.is_alive(): return
        try:
            values = {k:v.get() for k,v in self.fields.items()}
            port = int(values['端口'])
            if not 1 <= port <= 65535:
                raise ValueError('端口范围 1–65535')
            if any(c in values['用户'] for c in '\r\n"') or any(c in values['密码'] for c in '\r\n'):
                raise ValueError('用户名或密码含无效字符')
        except ValueError as exc:
            messagebox.showerror('参数错误', str(exc))
            return
        if not self.apply_intervals(): return
        self.stop.clear()
        self.button.config(state='disabled')
        self.status.set('正在连接…')
        trace = self.trace.get()
        def run():
            osa = None
            try:
                osa = OSA(values['IP'], port, values['用户'], values['密码'])
                self.events.put(('status', osa.identity))
                next_record = next_display = 0.0
                version = self.timing_version
                while not self.stop.is_set():
                    record_interval,display_interval=self.intervals
                    if version != self.timing_version:
                        next_record=next_display=0.0
                        version=self.timing_version
                    tick=time.monotonic()
                    if tick < min(next_record,next_display):
                        self.stop.wait(min(0.1,min(next_record,next_display)-tick))
                        continue
                    self.save_current_spectrum=tick>=next_record
                    display_due=tick>=next_display
                    data = (*osa.spectrum(trace), time.strftime('%Y-%m-%d %H:%M:%S'), trace)
                    self.on_spectrum(data)
                    if self.save_current_spectrum: next_record=tick+record_interval
                    if display_due:
                        self.events.put(('data', data))
                        next_display=tick+display_interval
            except Exception as exc:
                self.events.put(('status', '连接/读取失败：'+str(exc)))
            finally:
                if osa: osa.close()
                self.events.put(('done', None))
        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def apply_intervals(self,show_error=True):
        try:
            record=float(self.fields['记录间隔(s)'].get())
            display=float(self.fields['UI刷新间隔(s)'].get())
            if not all(math.isfinite(v) and v>=.2 for v in (record,display)):
                raise ValueError('记录间隔和UI刷新间隔都必须为有限数值，且至少0.2秒')
        except ValueError as exc:
            self.timing_status.set('间隔无效，保留原设置：'+str(exc))
            if show_error:messagebox.showerror('间隔设置错误',str(exc))
            return False
        if self.intervals==(record,display):return True
        self.intervals=(record,display)
        self.timing_version+=1
        self.timing_status.set(f'已应用：记录 {record:g} 秒 / UI刷新 {display:g} 秒')
        self.on_intervals_changed(record,display)
        return True

    def on_intervals_changed(self,record,display):
        pass

    def on_spectrum(self, data):
        """Optional worker-thread persistence hook, independent of display rate."""
        pass

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'data':
                    self.data = value
                    x,y,unit,stamp,trace = value
                    i = max(range(len(y)), key=y.__getitem__)
                    self.status.set(f'{stamp} | {trace} | {len(x)} 点 | 峰值 {x[i]:.5f} nm / {y[i]:.6g} {unit} | 扫描快照')
                    self.draw()
                elif kind == 'done':
                    self.button.config(state='normal')
                    self.status.set(self.status.get()+' | 已停止监测')
                else: self.status.set(value)
        except queue.Empty: pass
        self.root.after(100, self.poll)

    def draw(self):
        c = self.canvas
        c.delete('all')
        if not self.data:
            c.create_text(450,200,text='等待仪器光谱数据',fill='white',font=('Microsoft YaHei',18))
            return
        x,y,unit,_,_ = self.data
        w,h = c.winfo_width(),c.winfo_height()
        left,top,right,bottom = 90,35,max(110,w-25),max(60,h-60)
        xmin,xmax,ymin,ymax = min(x),max(x),min(y),max(y)
        if xmin == xmax: xmax += 1
        pad = max((ymax-ymin)*.05,1e-12)
        ymin,ymax = ymin-pad,ymax+pad
        self.axes = left,top,right,bottom,xmin,xmax
        self.plot_bounds = left,top,right,bottom,xmin,xmax,ymin,ymax
        for i in range(6):
            px,py = left+(right-left)*i/5,top+(bottom-top)*i/5
            c.create_line(px,top,px,bottom,fill='#334155')
            c.create_line(left,py,right,py,fill='#334155')
            c.create_text(px,bottom+18,text=f'{xmin+(xmax-xmin)*i/5:.3f}',fill='white')
            c.create_text(left-8,py,text=f'{ymax-(ymax-ymin)*i/5:.4g}',anchor='e',fill='white')
        c.create_text((left+right)/2,bottom+42,text='波长 (nm)',fill='white')
        c.create_text(left,15,text=f'功率 ({unit})',anchor='w',fill='white')
        # Preserve minima and maxima in each horizontal pixel bin.
        bins = {}
        for i,v in enumerate(x):
            key = int((v-xmin)/(xmax-xmin)*(right-left))
            if key not in bins: bins[key] = [i,i]
            else:
                if y[i] < y[bins[key][0]]: bins[key][0] = i
                if y[i] > y[bins[key][1]]: bins[key][1] = i
        points = []
        for i in sorted({i for pair in bins.values() for i in pair}):
            points.extend((left+(x[i]-xmin)/(xmax-xmin)*(right-left),bottom-(y[i]-ymin)/(ymax-ymin)*(bottom-top)))
        if len(points) >= 4: c.create_line(*points,fill='#38bdf8',width=1.5)

    def cursor(self,event):
        if not self.data or not hasattr(self,'axes'): return
        left,top,right,bottom,xmin,xmax = self.axes
        self.canvas.delete('cursor')
        if not left <= event.x <= right: return
        target = xmin+(event.x-left)/(right-left)*(xmax-xmin)
        x,y,unit,_,_ = self.data
        i = min(range(len(x)),key=lambda n:abs(x[n]-target))
        self.canvas.create_line(event.x,top,event.x,bottom,fill='#fbbf24',tags='cursor')
        self.canvas.create_text(right,top+10,anchor='e',text=f'{x[i]:.5f} nm  {y[i]:.6g} {unit}',fill='#fbbf24',tags='cursor')

    def export(self):
        if not self.data: return
        path = filedialog.asksaveasfilename(defaultextension='.csv',initialfile='spectrum_'+time.strftime('%Y%m%d_%H%M%S')+'.csv',filetypes=[('CSV','*.csv')])
        if not path: return
        try:
            x,y,unit,stamp,trace = self.data
            with open(path,'w',newline='',encoding='utf-8-sig') as f:
                writer = csv.writer(f)
                writer.writerow(['timestamp','trace','wavelength_nm','power_'+unit])
                writer.writerows((stamp,trace,a,b) for a,b in zip(x,y))
        except OSError as exc: messagebox.showerror('保存失败',str(exc))

    def quit(self):
        self.stop.set()
        if self.worker and self.worker.is_alive():
            self.status.set('正在关闭连接…')
            self.root.after(100,self.quit)
        else: self.root.destroy()

if __name__ == '__main__':
    root = tk.Tk()
    Monitor(root)
    root.mainloop()
