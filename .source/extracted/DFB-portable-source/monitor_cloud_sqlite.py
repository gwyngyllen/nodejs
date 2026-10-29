"""AQ6370E 多通道寻峰监控 · 云端推送 · SQLite 存档。

在 monitor_cloud.CloudChannelMonitor 之上，把每帧光谱写进 SQLite，而不是每帧
生成一个 .json.gz 文件（单个会话 24 万个文件 -> 1 个库）。云端推送、本地寻峰
和数据库写入三者互不阻塞。

启动时会从库里最后一组通道身份恢复锚点，让新会话接得上历史数据。

用法：
    runtime\\python.exe -B monitor_cloud_sqlite.py [--db recordings\\dfb.db]

注意：SQLite 同一时刻只允许一个写者。迁移或分析脚本运行期间不要同时开本程序。
"""
import argparse
import tkinter as tk
from tkinter import ttk

from main import log
from monitor_cloud import CloudChannelMonitor
from spectrum_db import DEFAULT_DB, SpectrumDB


class SqliteChannelMonitor(CloudChannelMonitor):
    """Channel monitor that stores frames in SQLite instead of per-frame files."""

    def __init__(self, root, db_path=DEFAULT_DB):
        self.db_path = str(db_path)
        self.db = None
        self.session_id = None
        self._last_frame_id = None
        self._db_frames = 0
        self._db_error = ''
        self._config_saved = False
        super().__init__(root)
        root.title('AQ6370E 多通道动态寻峰监控 · 云端推送 · SQLite')
        self.open_database()
        self.db_status = tk.StringVar(value='数据库：连接中…')
        ttk.Label(root, textvariable=self.db_status).pack(fill='x')
        self.refresh_db_status()

    # -- 生命周期 -----------------------------------------------------

    def open_database(self):
        try:
            self.db = SpectrumDB(self.db_path)
        except Exception as exc:
            self.db = None
            self._db_error = '%s: %s' % (type(exc).__name__, exc)
            log.exception('SQLite open failed')
            return
        self.restore_channel_config()

    def restore_channel_config(self):
        """沿用库里最后一组通道锚点，让新旧会话的通道编号接得上。"""
        if self.db is None or self.tracker.anchors:
            return
        config = self.db.latest_channel_config()
        if not config:
            return
        anchors = config.get('initial_channel_wavelengths_nm') or []
        if len(anchors) != self.tracker.config.channels:
            return
        self.tracker.anchors = anchors[:]
        self.tracker.previous = anchors[:]
        log.info('Restored %d channel anchors from %s', len(anchors), self.db_path)

    def close_database(self):
        if self.db is not None:
            try:
                self.db.close()
            except Exception:
                log.exception('SQLite close failed')
            self.db = None

    def quit(self):
        if self.worker and self.worker.is_alive():
            super().quit()
            return
        self.close_database()
        super().quit()

    # -- 存储 ---------------------------------------------------------

    def store_frame(self, stamp, trace, x, y, unit, row):
        if self.db is None:
            return
        try:
            if self.session_id is None:
                self.session_id = self.db.create_session(
                    started_at=stamp, folder=str(self.folder.resolve()),
                    trace=trace, unit=unit, point_count=len(x),
                    wavelength_start=x[0],
                    wavelength_step=(x[1] - x[0]) if len(x) > 1 else 0.0)
            self._last_frame_id = self.db.add_frame(
                session_id=self.session_id, captured_at=stamp,
                elapsed_s=row['elapsed_s'], x=x, y=y, unit=unit,
                peak_nm=row.get('peak_nm'), peak_power=row.get('peak_power'),
                shape_rms=row.get('shape_rms'), fingerprint=row.get('fingerprint'))
            self._db_frames += 1
            self._db_error = ''
        except Exception as exc:
            self._db_error = '%s: %s' % (type(exc).__name__, exc)
            log.exception('SQLite write failed')

    def on_spectrum(self, data):
        super().on_spectrum(data)
        if self.db is None or not self.save_current_spectrum:
            return
        if not self._config_saved and self.session_id is not None and self.tracker.anchors:
            try:
                self.db.update_session_channel_config(
                    self.session_id,
                    dict(self.tracker.metadata(), channel_epoch=self.channel_epoch))
                self._config_saved = True
            except Exception as exc:
                self._db_error = '%s: %s' % (type(exc).__name__, exc)
                log.exception('SQLite channel config write failed')
        result = self.display_results.get(id(data))
        if result is None or self._last_frame_id is None:
            return
        try:
            self.db.add_channel_peaks(self._last_frame_id, result['channels'],
                                      channel_epoch=result.get('channel_epoch'))
        except Exception as exc:
            self._db_error = '%s: %s' % (type(exc).__name__, exc)
            log.exception('SQLite channel peaks write failed')

    # -- 状态栏 -------------------------------------------------------

    def poll(self):
        super().poll()
        self.refresh_db_status()

    def refresh_db_status(self):
        if self.db is None:
            self.db_status.set('数据库：不可用 · ' + (self._db_error or self.db_path))
            return
        text = '数据库 %s · 会话 %s · 已写入 %d 帧' % (
            self.db_path,
            '#%d' % self.session_id if self.session_id else '待建立',
            self._db_frames)
        if self._db_error:
            text += ' · ' + self._db_error
        self.db_status.set(text)


def main():
    ap = argparse.ArgumentParser(description='AQ6370E monitor: cloud push + SQLite')
    ap.add_argument('--db', default=str(DEFAULT_DB),
                    help='SQLite file (default: recordings/dfb.db)')
    args = ap.parse_args()
    root = tk.Tk()
    SqliteChannelMonitor(root, db_path=args.db)
    root.mainloop()


if __name__ == '__main__':
    main()
