"""AQ6370E 多通道动态寻峰监控 · 云端推送版。

在 monitor_channels.ChannelMonitor 之上，把每一帧记录到的光谱同时推送到 DFB
云端应用。本地记录、寻峰与云端显示并行进行，且只占用仪器那一条控制连接。

入口：runtime\\python.exe -B monitor_cloud.py（或 start_cloud_monitor.bat）

云端地址与令牌在界面顶栏填写。令牌只存在于输入框内，不写入磁盘，也不记录
到日志；窗口关闭即失效。
"""
import json
import queue
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from tkinter import ttk

from monitor_channels import ChannelMonitor

# Cloudflare（挡在 base44.app 前面）会拒绝默认的 "Python-urllib/x.y"
# User-Agent 并返回 403，所以带一个浏览器 UA。
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 云端应用拒收 wavelengths/powers 序列化后超过约 20000 字符的帧（返回
# 400 "HTTPError"），两个数组都留出余量。
MAX_ARRAY_CHARS = 19000

DEFAULT_URL = 'https://glxk.base44.app'


def http_post(url, token, payload, timeout=20):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _array_chars(values):
    return len(json.dumps(values, separators=(",", ":")))


def _decimate(x, y, target):
    """Keep the min and max of each wavelength bin so peaks survive."""
    lo, hi = x[0], x[-1]
    span = hi - lo
    bins = {}
    for i, v in enumerate(x):
        k = int((v - lo) / span * target) if span > 0 else 0
        if k not in bins:
            bins[k] = [i, i]
        else:
            if y[i] < y[bins[k][0]]:
                bins[k][0] = i
            if y[i] > y[bins[k][1]]:
                bins[k][1] = i
    idx = sorted({i for pair in bins.values() for i in pair})
    return [x[i] for i in idx], [y[i] for i in idx]


def fit_frame(x, y):
    """Round and, if still too long, decimate a frame to fit the app's limit.

    仪器返回的 X 带浮点噪声（1308.0100000000002 nm），Y 有 8 位小数，本身就会
    让两个数组双双超限。
    """
    x = [round(v, 4) for v in x]
    y = [round(v, 4) for v in y]
    target = len(x)
    while target > 2 and max(_array_chars(x), _array_chars(y)) > MAX_ARRAY_CHARS:
        target //= 2
        x, y = _decimate(x, y, target)
    return x, y


class CloudPusher:
    """Pushes recorded frames to the web app from a background thread.

    只保留最新一帧：云端显示的是实时光谱，链路慢时应当丢弃过期帧，而不是越
    积越多地落后于仪器。
    """

    def __init__(self, base, token):
        self.base = base.rstrip('/')
        self.token = token
        self.frames = queue.Queue(maxsize=1)
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.pushed = 0
        self.failed = 0
        self.last_error = ''
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def submit(self, x, y, unit, trace):
        frame = (x, y, unit, trace)
        try:
            self.frames.put_nowait(frame)
        except queue.Full:
            try:
                self.frames.get_nowait()
            except queue.Empty:
                pass
            try:
                self.frames.put_nowait(frame)
            except queue.Full:
                pass

    def stats(self):
        with self.lock:
            return self.pushed, self.failed, self.last_error

    def _run(self):
        while not self.stopping.is_set():
            try:
                x, y, unit, trace = self.frames.get(timeout=0.5)
            except queue.Empty:
                continue
            x, y = fit_frame(x, y)
            try:
                http_post(self.base + '/api/spectrum-frame', self.token, {
                    'trace': trace,
                    'wavelengths': x,
                    'powers': y,
                    'unit': unit,
                    'point_count': len(x),
                    'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
                })
                with self.lock:
                    self.pushed += 1
                    self.last_error = ''
            except Exception as exc:
                with self.lock:
                    self.failed += 1
                    self.last_error = '%s: %s' % (type(exc).__name__, exc)
                # 链路故障时退避，避免空转刷屏。
                self.stopping.wait(2.0)

    def close(self):
        self.stopping.set()
        self.thread.join(timeout=3)


class CloudChannelMonitor(ChannelMonitor):
    """Channel monitor that mirrors every recorded frame to the web app."""

    def __init__(self, root):
        self.pusher = None
        # 先打包，让云端控制栏落在通道控制栏之上。
        self.cloud_bar = ttk.Frame(root, padding=(10, 8))
        self.cloud_bar.pack(fill='x')
        super().__init__(root)
        root.title('AQ6370E 多通道动态寻峰监控 · 云端推送')
        self.build_cloud_controls()

    def build_cloud_controls(self):
        self.cloud_url = tk.StringVar(value=DEFAULT_URL)
        self.cloud_token = tk.StringVar(value='')
        self.cloud_push = tk.BooleanVar(value=True)
        self.cloud_status = tk.StringVar(value='云端推送：待填写令牌')
        bar = self.cloud_bar
        ttk.Label(bar, text='云端推送').pack(side='left')
        ttk.Label(bar, text='地址').pack(side='left', padx=(10, 3))
        url_entry = ttk.Entry(bar, textvariable=self.cloud_url, width=34)
        url_entry.pack(side='left')
        ttk.Label(bar, text='令牌').pack(side='left', padx=(8, 3))
        token_entry = ttk.Entry(bar, textvariable=self.cloud_token, width=16, show='*')
        token_entry.pack(side='left')
        ttk.Checkbutton(bar, text='启用', variable=self.cloud_push,
                        command=self.refresh_pusher).pack(side='left', padx=(8, 0))
        ttk.Label(bar, textvariable=self.cloud_status).pack(side='left', padx=10)
        # 回车或移开焦点时重读输入框，避免每敲一个字符就重启一次发送线程。
        for entry in (url_entry, token_entry):
            entry.bind('<Return>', lambda event: self.refresh_pusher())
            entry.bind('<FocusOut>', lambda event: self.refresh_pusher())
        self.refresh_pusher()

    def refresh_pusher(self):
        url = self.cloud_url.get().strip().rstrip('/')
        token = self.cloud_token.get().strip()
        if not (self.cloud_push.get() and url and token):
            if self.pusher:
                self.pusher.close()
                self.pusher = None
            return
        if self.pusher and (self.pusher.base != url or self.pusher.token != token):
            self.pusher.close()
            self.pusher = None
        if not self.pusher:
            self.pusher = CloudPusher(url, token)

    def on_spectrum(self, data):
        super().on_spectrum(data)
        pusher = self.pusher
        if pusher and self.save_current_spectrum:
            x, y, unit, _, trace = data
            pusher.submit(x, y, unit, trace)

    def poll(self):
        super().poll()
        pusher = self.pusher
        if pusher is None:
            if not self.cloud_push.get():
                text = '云端推送：已关闭'
            elif not self.cloud_token.get().strip():
                text = '云端推送：待填写令牌'
            else:
                text = '云端推送：未启用'
        else:
            pushed, failed, error = pusher.stats()
            text = '云端推送 %s：已推送 %d 帧' % (pusher.base, pushed)
            if failed:
                text += ' · 失败 %d' % failed
            if error:
                text += ' · ' + error
        self.cloud_status.set(text)

    def quit(self):
        if self.pusher:
            self.pusher.close()
            self.pusher = None
        super().quit()


if __name__ == '__main__':
    root = tk.Tk()
    CloudChannelMonitor(root)
    root.mainloop()
