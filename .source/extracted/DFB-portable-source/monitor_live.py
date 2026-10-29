"""Visible live spectrum monitor with persistent snapshots on one OSA connection."""
import csv
import gzip
import json
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path

from main import Monitor, log
from record_trend import metrics, report


class RecordingMonitor(Monitor):
    def __init__(self, root):
        self.folder = Path('recordings') / datetime.now().strftime('%Y%m%d_%H%M%S')
        self.folder.mkdir(parents=True)
        Path('recordings/latest.txt').write_text(str(self.folder.resolve()), encoding='utf-8')
        self.rows = []
        self.baseline = None
        self.last_saved = None
        self.previous_data = None
        self.started = time.monotonic()
        self.record_error = None
        self.last_report = float('-inf')
        self.latest_unit = ''
        super().__init__(root)
        root.title('AQ6370E LAN 光谱监测 · 自动记录')
        from tkinter import ttk
        ttk.Label(root, text=f'记录目录：{self.folder.resolve()}（实际间隔受仪器响应和保存耗时限制）').pack(fill='x')
        root.after(300, self.start)

    def on_spectrum(self, data):
        if not self.save_current_spectrum:
            return
        now = time.monotonic()
        x, y, unit, stamp, trace = data
        stamp = datetime.now().isoformat(sep=' ', timespec='milliseconds')
        self.latest_unit = unit
        if self.baseline is None:
            self.baseline = (x, y, unit)
            self.started = now
        row = dict(timestamp=stamp, elapsed_s=now-self.started, unit=unit,
                   **metrics(x, y, unit, self.baseline))
        try:
            self.store_frame(stamp, trace, x, y, unit, row)
            path = self.folder / 'metrics.csv'
            with path.open('a', newline='', encoding='utf-8-sig') as f:
                writer = csv.DictWriter(f, fieldnames=list(row))
                if not self.rows:
                    writer.writeheader()
                writer.writerow(row)
            self.rows.append(row)
            self.last_saved = now
            if now - self.last_report >= 10:
                report(self.folder, self.rows, unit, '记录中', 0)
                self.last_report = now
            self.record_error = None
        except OSError as exc:
            log.exception('Automatic recording failed')
            self.record_error = str(exc)

    def store_frame(self, stamp, trace, x, y, unit, row):
        """Persist one raw spectrum. Override to change the storage backend.

        `row` carries the metrics computed for this frame (elapsed_s, peak_nm,
        peak_power, shape_rms, fingerprint); `self.rows` has not been appended
        yet, so use it for anything that must stay aligned with the save order.
        """
        with gzip.open(self.folder / f'spectrum_{len(self.rows):05d}.json.gz', 'wt', encoding='utf-8') as f:
            json.dump(dict(timestamp=stamp, trace=trace, unit=unit, wavelength_nm=x, power=y), f)

    def on_intervals_changed(self,record,display):
        with (self.folder/'timing_changes.jsonl').open('a',encoding='utf-8') as f:
            f.write(json.dumps(dict(timestamp=datetime.now().isoformat(timespec='milliseconds'),
                                   record_interval_s=record,ui_refresh_interval_s=display))+'\n')

    def poll(self):
        super().poll()
        if self.record_error:
            self.status.set('自动保存失败：' + self.record_error)
        elif self.rows and self.worker and not self.worker.is_alive():
            report(self.folder, self.rows, self.latest_unit, self.status.get(), 0)

    def quit(self):
        if self.rows and (not self.worker or not self.worker.is_alive()):
            report(self.folder, self.rows, self.latest_unit, '监测结束', 0)
        super().quit()


if __name__ == '__main__':
    root = tk.Tk()
    RecordingMonitor(root)
    root.mainloop()
