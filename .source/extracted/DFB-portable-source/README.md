# AQ6370E 可调通道动态光谱监控

主入口：`monitor_channels.py`。在 PyCharm 中运行，或执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\start_monitor.ps1
```

默认连接 `10.200.5.10:10001`，TRA轨迹。仪器启用NETWORK(SOCKET)控制，并在仪器上开启REPEAT扫描；20001是Viewer监控端口，不能用于SCPI控制。仪器通常只允许一个控制客户端。

每帧动态寻峰并保持CH01–CHNN身份；显示中心峰顶波长、峰值功率、状态和图上编号。记录间隔与UI刷新间隔独立设置，使用带上下箭头的输入框，步长分别为0.5秒和2秒。点击箭头立即生效，手动输入后回车或移开焦点生效，无需应用按钮。通道数量默认16，可设置为1–100，修改通道数量或间距后点击顶部“重新搜索通道”，于下一帧生效并建立新编号段。详见 [动态寻峰说明](PEAK_SEARCH_README.md)。

## 目录

| 目录 | 内容 |
|---|---|
| `bak/` | 已注释停用的固定波长旧代码、一次性旧工具及原README |
| `tmp/logs/` | 当前运行日志与历史日志；日志自动轮转 |
| `tmp/diagnostics/` | 连接诊断临时结果 |
| `scripts/reports/` | 下午汇总分析与PDF/HTML报告生成脚本 |
| `scripts/diagnostics/` | 连接、历史跟踪验证及手册读取工具 |
| `tests/` | 算法、时间调度、峰标记和模拟仪器验证 |
| `docs/manuals/` | AQ6370E参考PDF手册 |
| `recordings/` | 原始光谱、逐帧十通道数据及会话状态 |
| `output/` | 已生成的报告、图表及汇总数据 |
| `.venv/`、`.tools/` | 项目环境及本地报告工具依赖 |

根目录核心模块：`main.py`（通信/UI基础）、`monitor_live.py`（记录基础）、`monitor_channels.py`（十通道UI）、`peak_search.py`（寻峰与跟踪）、`record_trend.py`（全谱辅助统计）、`replay_channels.py`（离线十通道回放）。

## 常用命令

在项目根目录执行，使用已安装依赖的Python解释器：

```powershell
python replay_channels.py --folder recordings/<会话目录>
python scripts/reports/analyze_afternoon.py
python scripts/reports/build_afternoon_report.py
python -m unittest tests.test_peak_search tests.test_monitor_timing tests.test_channel_markers.ChannelMarkerTests -v
python tests/verify_monitor.py
```

两个下午报告脚本保留2026-10-15的分析范围和输出目录，属于该次报告的可复现工具。诊断连接时应先停止监控，避免争用仪器控制连接。

日志位置：`tmp/logs/osa_debug.log`，轮转文件为`.1`、`.2`。整理前的三份调试日志保存在`tmp/logs/archive/`，不会覆盖新的运行日志。

## 测量与历史说明

谱峰位置为拟合峰顶，功率为峰值谱电平，不是谱线质心或积分功率。连续扫描X/Y分开读取，不保证每份快照均为独立完整扫描。RES未逐帧保存，拟合小数不代表仪器精度。

旧固定点分析源于沟通错误，已整体注释并移入`bak`；其结果不得当作十通道中心波长或峰值功率结论。原始光谱、报告和环境本次整理均保留。


默认监控16个通道，最小通道间距默认0.30 nm，允许设置为0.30 nm或更大。数量、间距及“重新搜索通道”按钮均位于主界面顶部。点击重新搜索，下一帧先按间距筛选有效峰，再选取设定数量的最强峰，按波长升序编号；候选不足时等待，不补造通道。每次重新搜索建立新编号段。不符合新下限的旧配置会迁移至0.30 nm并重新识别。若存在上次会话，仍恢复上次已保存的有效参数。


