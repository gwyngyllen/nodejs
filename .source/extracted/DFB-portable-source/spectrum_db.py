"""OSA 光谱的 SQLite 存储：建表、写入、读取/回放、历史数据迁移。

设计要点（依据实际数据实测）：

- X 轴在仪器上是均匀网格（步长 0.01 nm，用 start + step 重建误差 2e-13 nm），
  所以默认只存 start/step，非均匀时才回退到存整个数组。
- Y 用 zlib 压缩的 float32 存 BLOB（15.7 KB 的 gz JSON -> 6.9 KB），
  float32 量化误差 1.9e-6 dB，远低于测量噪声。
- 峰值等标量提升为独立列，查询趋势时不必解压任何 BLOB。
- 绝不把每个波长点存成一行：2 帧/秒 × 2001 点 = 每天 3.4 亿行。

命令行：
    python spectrum_db.py migrate <会话目录> [--db recordings/dfb.db]
    python spectrum_db.py sessions  [--db ...]
    python spectrum_db.py replay <session_id> [--db ...] [--limit N]
"""
import argparse
import array
import csv
import gzip
import json
import sqlite3
import sys
import time
import zlib
from datetime import datetime
from pathlib import Path

DEFAULT_DB = Path('recordings') / 'dfb.db'
GRID_TOLERANCE_NM = 1e-9


# ---------------------------------------------------------------- 编码 helpers

def pack_floats(values):
    """float32 小端 + zlib。约 6.9 KB / 2001 点。"""
    buf = array.array('f', values)
    if sys.byteorder != 'little':
        buf.byteswap()
    return zlib.compress(buf.tobytes(), 6)


def unpack_floats(blob):
    buf = array.array('f')
    buf.frombytes(zlib.decompress(blob))
    if sys.byteorder != 'little':
        buf.byteswap()
    return buf.tolist()


def grid_of(x):
    """返回 (start, step, uniform)。均匀网格不必存整个 X 数组。"""
    n = len(x)
    if n < 2:
        return (x[0] if x else 0.0), 0.0, True
    start = x[0]
    step = x[1] - start
    prev = start
    for value in x[1:]:
        if abs((value - prev) - step) > GRID_TOLERANCE_NM:
            return start, step, False
        prev = value
    return start, step, True


# ---------------------------------------------------------------- 数据库

SCHEMA = """
CREATE TABLE IF NOT EXISTS session (
    id               INTEGER PRIMARY KEY,
    started_at       TEXT    NOT NULL,
    folder           TEXT    UNIQUE,
    instrument       TEXT,
    trace            TEXT    NOT NULL,
    unit             TEXT    NOT NULL,
    point_count      INTEGER,
    wavelength_start REAL,
    wavelength_step  REAL,
    channel_config   TEXT
);

CREATE TABLE IF NOT EXISTS frame (
    id               INTEGER PRIMARY KEY,
    session_id       INTEGER NOT NULL REFERENCES session(id),
    captured_at      TEXT    NOT NULL,
    elapsed_s        REAL    NOT NULL,
    unit             TEXT    NOT NULL,
    point_count      INTEGER NOT NULL,
    wavelength_start REAL    NOT NULL,
    wavelength_step  REAL    NOT NULL,
    uniform_grid     INTEGER NOT NULL,
    wavelength_z     BLOB,
    power_z          BLOB    NOT NULL,
    peak_nm          REAL,
    peak_power       REAL,
    shape_rms        REAL,
    fingerprint      TEXT
);
CREATE INDEX IF NOT EXISTS frame_session_time ON frame(session_id, elapsed_s);
CREATE INDEX IF NOT EXISTS frame_time         ON frame(captured_at);
CREATE INDEX IF NOT EXISTS frame_peak_nm      ON frame(peak_nm);

CREATE TABLE IF NOT EXISTS channel_peak (
    frame_id        INTEGER NOT NULL REFERENCES frame(id),
    channel_epoch   INTEGER,
    channel         TEXT    NOT NULL,
    status          TEXT,
    reason          TEXT,
    anchor_nm       REAL,
    peak_nm         REAL,
    peak_dbm        REAL,
    sampled_nm      REAL,
    sampled_dbm     REAL,
    prominence_db   REAL,
    fit_valid       INTEGER,
    PRIMARY KEY (frame_id, channel)
) WITHOUT ROWID;
"""

FRAME_COLUMNS = (
    "id, session_id, captured_at, elapsed_s, unit, point_count, "
    "wavelength_start, wavelength_step, uniform_grid, wavelength_z, power_z, "
    "peak_nm, peak_power, shape_rms, fingerprint"
)


class SpectrumDB:
    def __init__(self, path=DEFAULT_DB, batch_size=256):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.batch_size = batch_size
        # 采集线程写、分析脚本读；只允许一个写者，所以关闭线程检查是安全的。
        self.con = sqlite3.connect(str(self.path), check_same_thread=False)
        self.con.execute('PRAGMA journal_mode=WAL')
        self.con.execute('PRAGMA synchronous=NORMAL')
        self.con.executescript(SCHEMA)
        self._in_tx = False
        self._pending = 0

    # -- 写入 ---------------------------------------------------------

    def _begin(self):
        if not self._in_tx:
            self.con.execute('BEGIN')
            self._in_tx = True

    def flush(self):
        if self._in_tx:
            self.con.commit()
            self._in_tx = False
            self._pending = 0

    def close(self):
        self.flush()
        self.con.close()

    def create_session(self, started_at, folder=None, instrument=None, trace='TRA',
                       unit='dBm', point_count=None, wavelength_start=None,
                       wavelength_step=None, channel_config=None):
        self._begin()
        cur = self.con.execute(
            "INSERT INTO session (started_at, folder, instrument, trace, unit, "
            "point_count, wavelength_start, wavelength_step, channel_config) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (started_at, str(folder) if folder else None, instrument, trace, unit,
             point_count, wavelength_start, wavelength_step,
             json.dumps(channel_config, ensure_ascii=False) if channel_config else None))
        self.flush()
        return cur.lastrowid

    def update_session_channel_config(self, session_id, channel_config):
        self._begin()
        self.con.execute(
            "UPDATE session SET channel_config = ? WHERE id = ?",
            (json.dumps(channel_config, ensure_ascii=False), session_id))
        self.flush()

    def add_frame(self, session_id, captured_at, elapsed_s, x, y, unit,
                  peak_nm=None, peak_power=None, shape_rms=None, fingerprint=None):
        if len(x) != len(y) or not y:
            raise ValueError('wavelengths/powers mismatch (%d vs %d)' % (len(x), len(y)))
        start, step, uniform = grid_of(x)
        self._begin()
        cur = self.con.execute(
            "INSERT INTO frame (session_id, captured_at, elapsed_s, unit, point_count, "
            "wavelength_start, wavelength_step, uniform_grid, wavelength_z, power_z, "
            "peak_nm, peak_power, shape_rms, fingerprint) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, captured_at, elapsed_s, unit, len(y), start, step,
             1 if uniform else 0, None if uniform else pack_floats(x),
             pack_floats(y), peak_nm, peak_power, shape_rms, fingerprint))
        self._pending += 1
        if self._pending >= self.batch_size:
            self.flush()
        return cur.lastrowid

    def add_channel_peaks(self, frame_id, channels, channel_epoch=None):
        rows = [
            (frame_id, channel_epoch, p.get('channel'), p.get('status'), p.get('reason'),
             p.get('anchor_nm'), p.get('peak_nm'), p.get('peak_dbm'),
             p.get('sampled_nm'), p.get('sampled_dbm'), p.get('prominence_db'),
             1 if p.get('fit_valid') else 0)
            for p in channels
        ]
        if not rows:
            return
        self._begin()
        self.con.executemany(
            "INSERT OR REPLACE INTO channel_peak (frame_id, channel_epoch, channel, "
            "status, reason, anchor_nm, peak_nm, peak_dbm, sampled_nm, sampled_dbm, "
            "prominence_db, fit_valid) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    # -- 读取 ---------------------------------------------------------

    @staticmethod
    def _to_frame(row):
        (fid, sid, ts, elapsed, unit, n, start, step, uniform, wl_z, pw_z,
         peak_nm, peak_power, shape_rms, fingerprint) = row
        if uniform:
            x = [start + i * step for i in range(n)]
        else:
            x = unpack_floats(wl_z)
        return dict(id=fid, session_id=sid, captured_at=ts, elapsed_s=elapsed,
                    unit=unit, wavelengths=x, powers=unpack_floats(pw_z),
                    peak_nm=peak_nm, peak_power=peak_power,
                    shape_rms=shape_rms, fingerprint=fingerprint)

    def iter_frames(self, session_id=None, since=None, until=None, limit=None):
        sql = "SELECT " + FRAME_COLUMNS + " FROM frame"
        where, args = [], []
        if session_id is not None:
            where.append('session_id = ?')
            args.append(session_id)
        if since is not None:
            where.append('captured_at >= ?')
            args.append(since)
        if until is not None:
            where.append('captured_at <= ?')
            args.append(until)
        if where:
            sql += ' WHERE ' + ' AND '.join(where)
        sql += ' ORDER BY session_id, elapsed_s'
        if limit:
            sql += ' LIMIT %d' % int(limit)
        for row in self.con.execute(sql, args):
            yield self._to_frame(row)

    def get_frame(self, frame_id):
        row = self.con.execute(
            "SELECT " + FRAME_COLUMNS + " FROM frame WHERE id = ?", (frame_id,)).fetchone()
        return self._to_frame(row) if row else None

    def channel_peaks(self, frame_id):
        cur = self.con.execute(
            "SELECT channel, status, reason, anchor_nm, peak_nm, peak_dbm, "
            "sampled_nm, sampled_dbm, prominence_db, fit_valid, channel_epoch "
            "FROM channel_peak WHERE frame_id = ? ORDER BY channel", (frame_id,))
        keys = ('channel', 'status', 'reason', 'anchor_nm', 'peak_nm', 'peak_dbm',
                'sampled_nm', 'sampled_dbm', 'prominence_db', 'fit_valid', 'channel_epoch')
        return [dict(zip(keys, row)) for row in cur]

    def sessions(self):
        sql = ("SELECT s.id, s.started_at, s.folder, s.trace, s.unit, "
               "COUNT(f.id), MIN(f.captured_at), MAX(f.captured_at) "
               "FROM session s LEFT JOIN frame f ON f.session_id = s.id "
               "GROUP BY s.id ORDER BY s.id")
        keys = ('id', 'started_at', 'folder', 'trace', 'unit',
                'frames', 'first_frame', 'last_frame')
        return [dict(zip(keys, row)) for row in self.con.execute(sql)]

    def latest_channel_config(self):
        row = self.con.execute(
            "SELECT channel_config FROM session WHERE channel_config IS NOT NULL "
            "ORDER BY id DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def stats(self):
        frames, sessions = self.con.execute(
            "SELECT (SELECT COUNT(*) FROM frame), (SELECT COUNT(*) FROM session)").fetchone()
        size = self.path.stat().st_size if self.path.exists() else 0
        # 刚写完的数据可能还在 WAL 里，统计时要算上。
        wal = Path(str(self.path) + '-wal')
        if wal.exists():
            size += wal.stat().st_size
        return dict(frames=frames, sessions=sessions, bytes=size)


# ---------------------------------------------------------------- 迁移

class _Peekable:
    """一行缓冲，用于按时间戳对齐两条流，缺行/多行都不会永久错位。"""

    def __init__(self, iterable):
        self._it = iter(iterable)
        self._next = None
        self._has = False

    def peek(self):
        if not self._has:
            self._next = next(self._it, None)
            self._has = True
        return self._next

    def pop(self):
        value = self.peek()
        self._has = False
        return value


def _channel_groups(path):
    """按 timestamp 分组产出 channel_peaks.csv 的行（同帧行是连续的）。"""
    if not path.exists():
        return
    with path.open(encoding='utf-8-sig', newline='') as f:
        group, key = [], None
        for row in csv.DictReader(f):
            ts = row.get('timestamp')
            if key is None:
                key = ts
            elif ts != key:
                yield key, group
                group, key = [], ts
            group.append(row)
        if group:
            yield key, group


def _parse_float(value):
    try:
        return float(value) if value not in (None, '') else None
    except ValueError:
        return None


def _to_channel(row):
    return dict(channel=row.get('channel'), status=row.get('status'),
                reason=row.get('reason'), anchor_nm=_parse_float(row.get('anchor_nm')),
                peak_nm=_parse_float(row.get('peak_nm')),
                peak_dbm=_parse_float(row.get('peak_dbm')),
                sampled_nm=_parse_float(row.get('sampled_nm')),
                sampled_dbm=_parse_float(row.get('sampled_dbm')),
                prominence_db=_parse_float(row.get('prominence_db')),
                fit_valid=row.get('fit_valid') in ('True', 'true', '1'))


def _frame_index(path):
    """spectrum_00042.json.gz -> 42.

    必须按数值排序：文件名是 %05d，序号过 99999 后变成 6 位，字典序会把
    spectrum_243659 排到 spectrum_24366 前面。
    """
    return int(path.name.split('_')[1].split('.')[0])


def migrate_session(db, folder, instrument=None, log=print):
    """把一个 recordings/<session> 目录导入数据库。

    可重复执行：如果该目录已有部分数据，会从最后一帧之后续跑。
    返回 (session_id, 本次导入帧数)。
    """
    folder = Path(folder)
    db_folder = str(folder.resolve())
    paths = sorted(folder.glob('spectrum_*.json.gz'), key=_frame_index)
    if not paths:
        log('no spectrum files in %s' % folder)
        return None, 0

    config = None
    config_path = folder / 'channel_config.json'
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding='utf-8'))

    metrics_path = folder / 'metrics.csv'
    metrics = _Peekable(
        csv.DictReader(metrics_path.open(encoding='utf-8-sig', newline=''))) \
        if metrics_path.exists() else _Peekable(iter(()))

    groups = _channel_groups(folder / 'channel_peaks.csv')

    # -- 续跑：定位上次停下的位置 --------------------------------------
    session_id = None
    resume_ts = None
    start_index = 0
    first_dt = None
    row = db.con.execute("SELECT id FROM session WHERE folder = ?", (db_folder,)).fetchone()
    if row:
        session_id = row[0]
        count, last_ts = db.con.execute(
            "SELECT COUNT(*), MAX(captured_at) FROM frame WHERE session_id = ?",
            (session_id,)).fetchone()
        if count:
            resume_ts = last_ts
            probe = count
            # 跳过已经确认是坏的文件，找到第一个可读帧来校验续跑位置。
            probe_ts = None
            while probe < len(paths) and probe_ts is None:
                try:
                    with gzip.open(paths[probe], 'rt', encoding='utf-8') as f:
                        probe_ts = json.load(f).get('timestamp') or None
                except Exception:
                    probe += 1
            if probe_ts is not None and probe_ts <= last_ts:
                raise RuntimeError(
                    'cannot resume %s safely: file %d (%s) is not after %s; '
                    'previous run skipped files. Delete session %d and re-run.'
                    % (folder, probe, probe_ts, last_ts, session_id))
            start_index = probe
            started_row = db.con.execute(
                "SELECT started_at FROM session WHERE id = ?", (session_id,)).fetchone()
            if started_row:
                try:
                    first_dt = datetime.fromisoformat(started_row[0])
                except ValueError:
                    pass
            log('resume session %d at file %d/%d (last frame %s)'
                % (session_id, start_index, len(paths), last_ts))
        else:
            db.con.execute("DELETE FROM session WHERE id = ?", (session_id,))
            db.con.commit()
            session_id = None

    if resume_ts:
        head = metrics.peek()
        while head is not None and (head.get('timestamp') or '') <= resume_ts:
            metrics.pop()
            head = metrics.peek()

    pending_group = next(groups, None)
    while pending_group and resume_ts and pending_group[0] <= resume_ts:
        pending_group = next(groups, None)

    # -- 逐帧导入 ------------------------------------------------------
    imported = 0
    started = time.monotonic()
    total = len(paths)
    for index, path in enumerate(paths[start_index:], start=start_index):
        try:
            with gzip.open(path, 'rt', encoding='utf-8') as f:
                obj = json.load(f)
        except Exception as exc:
            # 损坏或截断的 .json.gz 不能让整次迁移中断。
            log('  ! unreadable %s: %s: %s' % (path.name, type(exc).__name__, exc))
            continue

        x, y = obj.get('wavelength_nm') or [], obj.get('power') or []
        if not x or len(x) != len(y):
            log('  ! bad frame %s (x=%d y=%d)' % (path.name, len(x), len(y)))
            continue

        ts = obj.get('timestamp') or ''
        trace = obj.get('trace') or 'TRA'
        unit = obj.get('unit') or 'dBm'
        if first_dt is None and ts:
            try:
                first_dt = datetime.fromisoformat(ts)
            except ValueError:
                pass

        # 按时间戳对齐 metrics.csv：坏文件不会让后面的帧永久错位一格。
        head = metrics.peek()
        while head is not None and (head.get('timestamp') or '') < ts:
            metrics.pop()
            head = metrics.peek()
        mrow = metrics.pop() if head is not None and (head.get('timestamp') or '') == ts else None

        if session_id is None:
            session_id = db.create_session(
                started_at=ts, folder=db_folder, instrument=instrument, trace=trace,
                unit=unit, point_count=len(x), wavelength_start=x[0],
                wavelength_step=(x[1] - x[0]) if len(x) > 1 else 0.0,
                channel_config=config)

        if mrow is not None:
            elapsed = _parse_float(mrow.get('elapsed_s'))
            peak_nm = _parse_float(mrow.get('peak_nm'))
            peak_power = _parse_float(mrow.get('peak_power'))
            shape_rms = _parse_float(mrow.get('shape_rms'))
            fingerprint = mrow.get('fingerprint')
        else:
            # CSV 缺行（通常是录制进程在写 CSV 前被中断）：直接从光谱补算。
            peak_index = max(range(len(y)), key=y.__getitem__)
            peak_nm, peak_power = x[peak_index], y[peak_index]
            shape_rms = None
            fingerprint = None
            elapsed = float(index)
            if first_dt is not None:
                try:
                    elapsed = (datetime.fromisoformat(ts) - first_dt).total_seconds()
                except ValueError:
                    pass

        frame_id = db.add_frame(
            session_id=session_id, captured_at=ts, elapsed_s=elapsed,
            x=x, y=y, unit=unit, peak_nm=peak_nm, peak_power=peak_power,
            shape_rms=shape_rms, fingerprint=fingerprint)
        imported += 1

        while pending_group and pending_group[0] < ts:
            pending_group = next(groups, None)
        if pending_group and pending_group[0] == ts:
            db.add_channel_peaks(frame_id, [_to_channel(r) for r in pending_group[1]],
                                 channel_epoch=config.get('channel_epoch') if config else None)
            pending_group = next(groups, None)

        if (index + 1) % 2000 == 0:
            elapsed = time.monotonic() - started
            done = index + 1 - start_index
            rate = done / elapsed if elapsed else 0
            remain = (total - index - 1) / rate if rate else 0
            log('  %d/%d frames  %.0f/s  eta %.0f s'
                % (index + 1, total, rate, remain))

    db.flush()
    return session_id, imported


# ---------------------------------------------------------------- CLI

def cmd_migrate(args):
    db = SpectrumDB(args.db)
    try:
        if args.restart:
            folder = str(Path(args.folder).resolve())
            db.con.execute(
                "DELETE FROM channel_peak WHERE frame_id IN "
                "(SELECT id FROM frame WHERE session_id IN "
                "(SELECT id FROM session WHERE folder = ?))", (folder,))
            db.con.execute(
                "DELETE FROM frame WHERE session_id IN "
                "(SELECT id FROM session WHERE folder = ?)", (folder,))
            db.con.execute("DELETE FROM session WHERE folder = ?", (folder,))
            db.con.commit()
            print('restart: removed existing session for', folder)
        session_id, imported = migrate_session(db, args.folder, instrument=args.instrument)
        if session_id:
            log = db.con.execute(
                "SELECT COUNT(*) FROM frame WHERE session_id = ?", (session_id,)).fetchone()[0]
            log2 = db.con.execute(
                "SELECT COUNT(*) FROM channel_peak WHERE frame_id IN "
                "(SELECT id FROM frame WHERE session_id = ?)", (session_id,)).fetchone()[0]
            print('session %d: %d frames, %d channel rows' % (session_id, log, log2))
        print('db: %s  %s' % (db.path, db.stats()))
    finally:
        db.close()


def cmd_export(args):
    """把一帧解压成 CSV，方便用 Excel / 文本编辑器查看每一个点。"""
    db = SpectrumDB(args.db)
    try:
        frame = db.get_frame(args.frame_id)
        if frame is None:
            print('no such frame:', args.frame_id)
            return
        if args.csv:
            with open(args.csv, 'w', newline='', encoding='utf-8-sig') as f:
                w = csv.writer(f)
                w.writerow(['wavelength_nm', 'power_' + frame['unit']])
                w.writerows(zip(frame['wavelengths'], frame['powers']))
            print('wrote %d points (%s, %s) to %s'
                  % (len(frame['powers']), frame['captured_at'], frame['unit'], args.csv))
        else:
            print('# frame %d  %s  %s' % (frame['id'], frame['captured_at'], frame['unit']))
            for a, b in zip(frame['wavelengths'], frame['powers']):
                print('%.6f,%.6f' % (a, b))
    finally:
        db.close()


def cmd_migrate_all(args):
    """把 recordings/ 下所有会话依次导入；已导入的会话会自动续跑。"""
    db = SpectrumDB(args.db)
    try:
        root = Path(args.recordings)
        folders = sorted(p for p in root.iterdir()
                         if p.is_dir() and p.name[:8].isdigit())
        if not folders:
            print('no session folders under', root)
            return
        for folder in folders:
            if folder.name in args.skip:
                print('== %s  (skipped)' % folder.name, flush=True)
                continue
            print('== %s' % folder.name, flush=True)
            try:
                session_id, imported = migrate_session(
                    db, folder, instrument=args.instrument)
            except Exception as exc:
                print('   !! failed: %s: %s' % (type(exc).__name__, exc), flush=True)
                continue
            if session_id is None:
                print('   -> no frames', flush=True)
                continue
            total = db.con.execute(
                "SELECT COUNT(*) FROM frame WHERE session_id = ?",
                (session_id,)).fetchone()[0]
            print('   -> session %d: %d frames (%d imported now)'
                  % (session_id, total, imported), flush=True)
        print('db:', db.stats())
    finally:
        db.close()


def cmd_sessions(args):
    db = SpectrumDB(args.db)
    try:
        for s in db.sessions():
            print('#%-3d %s -> %s  %-4s %-4s %7d frames  %s'
                  % (s['id'], s['started_at'], s['last_frame'], s['trace'], s['unit'],
                     s['frames'], s['folder']))
        print('db: %s  %s' % (db.path, db.stats()))
    finally:
        db.close()


def cmd_replay(args):
    db = SpectrumDB(args.db)
    try:
        count = 0
        for frame in db.iter_frames(session_id=args.session_id, limit=args.limit):
            peak = frame['peak_nm']
            if peak is None and frame['powers']:
                powers = frame['powers']
                peak = frame['wavelengths'][max(range(len(powers)), key=powers.__getitem__)]
            ts = frame['captured_at']
            print('%s  %5.2fs  %4d pts  peak %.5f nm  %.4f %s'
                  % (ts, frame['elapsed_s'], len(frame['powers']), peak,
                     max(frame['powers']) if frame['powers'] else 0.0, frame['unit']))
            count += 1
            if args.step:
                time.sleep(args.step)
        print('replayed %d frames' % count)
    finally:
        db.close()


def main():
    ap = argparse.ArgumentParser(description='OSA spectrum SQLite store')
    sub = ap.add_subparsers(dest='cmd')
    sub.required = True

    m = sub.add_parser('migrate', help='import a recordings/<session> folder')
    m.add_argument('folder')
    m.add_argument('--db', default=str(DEFAULT_DB))
    m.add_argument('--instrument')
    m.add_argument('--restart', action='store_true',
                   help='drop any existing data for this folder and import from scratch')
    m.set_defaults(func=cmd_migrate)

    s = sub.add_parser('sessions', help='list sessions')
    s.add_argument('--db', default=str(DEFAULT_DB))
    s.set_defaults(func=cmd_sessions)

    a = sub.add_parser('migrate-all', help='import every session folder under recordings/')
    a.add_argument('--db', default=str(DEFAULT_DB))
    a.add_argument('--recordings', default='recordings')
    a.add_argument('--instrument')
    a.add_argument('--skip', action='append', default=[],
                   help='session folder name to skip (repeatable)')
    a.set_defaults(func=cmd_migrate_all)

    r = sub.add_parser('replay', help='print frames of a session')
    r.add_argument('session_id', type=int)
    r.add_argument('--db', default=str(DEFAULT_DB))
    r.add_argument('--limit', type=int)
    r.add_argument('--step', type=float, default=0.0)
    r.set_defaults(func=cmd_replay)

    e = sub.add_parser('export', help='dump every point of one frame to CSV')
    e.add_argument('frame_id', type=int)
    e.add_argument('--db', default=str(DEFAULT_DB))
    e.add_argument('--csv', help='output path (omit to print to stdout)')
    e.set_defaults(func=cmd_export)

    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
