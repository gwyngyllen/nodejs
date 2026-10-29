"""Reanalyze raw spectra with per-frame peak search; never uses fixed wavelengths."""
import argparse
import csv
import gzip
import html
import json
from datetime import datetime
from pathlib import Path
from peak_search import ChannelTracker, SearchConfig


FIELDS=['frame','timestamp','elapsed_s','channel','status','reason','anchor_nm','peak_nm','peak_dbm',
        'sampled_nm','sampled_dbm','prominence_db','fit_valid']


def replay(folder,out,channel_count=None,peak_distance=None):
    config_path=folder/'channel_config.json'
    saved=json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {}
    settings=dict(saved.get('config',{}))
    if channel_count is not None:settings['channels']=channel_count
    if peak_distance is not None:settings['min_distance_nm']=peak_distance
    config=SearchConfig(**settings)
    channel_count=config.channels
    out.mkdir(parents=True,exist_ok=False)
    with (folder/'metrics.csv').open(encoding='utf-8-sig') as f:
        rows=list(csv.DictReader(f))
    tracker=ChannelTracker(config)
    channels={f'CH{i+1:02d}':[] for i in range(channel_count)}
    missing_frames=0
    candidates=[]
    reference_grid=None
    with (out/'channel_peaks.csv').open('w',newline='',encoding='utf-8-sig') as f:
        writer=csv.DictWriter(f,fieldnames=FIELDS,extrasaction='ignore');writer.writeheader()
        for i,row in enumerate(rows):
            with gzip.open(folder/f'spectrum_{i:05d}.json.gz','rt',encoding='utf-8') as raw:d=json.load(raw)
            if reference_grid is None:reference_grid=d['wavelength_nm']
            if d['trace']!='TRA' or d['wavelength_nm']!=reference_grid:
                raise ValueError(f'Trace/grid changed at frame {i}; split session before analysis')
            result=tracker.update(d['wavelength_nm'],d['power'],d['unit'])
            candidates.append(result['candidate_count'])
            if len(result['channels'])!=channel_count or any(p['status']!='ok' for p in result['channels']):missing_frames+=1
            for p in result['channels']:
                record=dict(frame=i,timestamp=d['timestamp'],elapsed_s=float(row['elapsed_s']),**p)
                writer.writerow(record);channels[p['channel']].append(record)
            if i%1000==0:print('Frames analyzed:',i,flush=True)
    stats=[]
    for key,records in channels.items():
        good=[p for p in records if p['status']=='ok']
        first=[p for p in good if p['elapsed_s']<=float(rows[0]['elapsed_s'])+30]
        last=[p for p in good if p['elapsed_s']>=float(rows[-1]['elapsed_s'])-30]
        def mean(values,field):return sum(p[field] for p in values)/len(values) if values else None
        early_nm,late_nm=mean(first,'peak_nm'),mean(last,'peak_nm')
        early_p,late_p=mean(first,'peak_dbm'),mean(last,'peak_dbm')
        stats.append(dict(channel=key,valid_frames=len(good),missing_frames=len(rows)-len(good),
            initial_nm=early_nm,latest_nm=late_nm,wavelength_change_pm=(late_nm-early_nm)*1000 if early_nm is not None and late_nm is not None else None,
            initial_dbm=early_p,latest_dbm=late_p,power_change_db=late_p-early_p if early_p is not None and late_p is not None else None,
            first_window_valid=len(first),last_window_valid=len(last),
            wavelength_range_nm=[min(p['peak_nm'] for p in good),max(p['peak_nm'] for p in good)] if good else None))
    result=dict(source=str(folder.resolve()),start=rows[0]['timestamp'],end=rows[-1]['timestamp'],
        frames=len(rows),incomplete_frames=missing_frames,channels=stats,
        candidate_count_range=[min(candidates),max(candidates)],**tracker.metadata())
    (out/'channel_summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    with (out/'channel_summary.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=list(stats[0]));w.writeheader();w.writerows(stats)
    make_report(out,result,channels)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return result


def make_report(out,result,channels):
    import sys
    sys.path.insert(0,'.tools')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    plt.rcParams['font.sans-serif']=['Microsoft YaHei'];plt.rcParams['axes.unicode_minus']=False
    series=[]
    for s in result['channels']:
        records=channels[s['channel']]
        t=np.array([r['elapsed_s']/60 for r in records])
        nm=np.array([r['peak_nm'] if r['status']=='ok' else np.nan for r in records])
        power=np.array([r['peak_dbm'] if r['status']=='ok' else np.nan for r in records])
        series.append((t,(nm-s['initial_nm'])*1000,power-s['initial_dbm']))
    limits_by_metric=[]
    for metric in (1,2):
        low=min(float(np.nanmin(a[metric])) for a in series)
        high=max(float(np.nanmax(a[metric])) for a in series)
        pad=max((high-low)*.06,.01)
        limits_by_metric.append((low-pad,high+pad))
    groups=[]
    for start_index in range(0,len(result['channels']),5):
        subset=result['channels'][start_index:start_index+5]
        label=f"{subset[0]['channel']} - {subset[-1]['channel']}"
        fig,axs=plt.subplots(2,1,figsize=(11,9),layout='constrained')
        for j,s in enumerate(subset,start_index):
            t,nm_delta,power_delta=series[j]
            color=plt.get_cmap('tab10')(j)
            # NaN breaks preserve missing measurements; no interpolation or carry-forward.
            axs[0].plot(t,nm_delta,lw=.8,color=color,label=f"{s['channel']} ({s['wavelength_change_pm']:+.2f} pm)")
            axs[1].plot(t,power_delta,lw=.8,color=color,label=f"{s['channel']} ({s['power_change_db']:+.3f} dB)")
        axs[0].set(ylabel='峰顶波长相对首30秒均值变化（pm）',xlabel='本轮时间（分钟）',ylim=limits_by_metric[0])
        axs[1].set(ylabel='峰值功率相对首30秒均值变化（dB）',xlabel='本轮时间（分钟）',ylim=limits_by_metric[1])
        for ax in axs:ax.grid(alpha=.2);ax.legend(ncol=3,fontsize=9)
        fig.suptitle(f'{label}：五通道动态峰位及峰值功率变化',fontsize=14)
        filename=f"dynamic_channels_{start_index+1:02d}_{start_index+5:02d}.png"
        fig.savefig(out/filename,dpi=160);plt.close(fig)
        weakest=min(subset,key=lambda s:s['power_change_db'])
        strongest=max(subset,key=lambda s:s['power_change_db'])
        nm_min=min(s['wavelength_change_pm'] for s in subset)
        nm_max=max(s['wavelength_change_pm'] for s in subset)
        group_text=f"本组首尾30秒平均峰顶波长变化范围{nm_min:+.2f}至{nm_max:+.2f} pm；峰值功率变化范围{weakest['power_change_db']:+.3f}至{strongest['power_change_db']:+.3f} dB。下降最多的是{weakest['channel']}（{weakest['power_change_db']:+.3f} dB），下降最少的是{strongest['channel']}（{strongest['power_change_db']:+.3f} dB）。各通道未匹配帧数为"+'、'.join(f"{s['channel']} {s['missing_frames']}帧" for s in subset)+'；缺口不插值、不沿用上一帧。'
        groups.append((label,filename,group_text))
    heading=f"{len(result['channels'])} 通道动态峰值监控报告"
    meta=f"{result['start']} 至 {result['end']} | {result['frames']}帧 | 不完整帧{result['incomplete_frames']}"
    correction='沟通错误说明：此前把十个光谱通道误解成十个固定波长点，旧通道结论不成立。本报告从原始光谱逐帧重新寻峰，不读取旧固定波长列表。'
    method=f"每帧全谱寻找局部极大值，要求峰值≥{result['config']['min_power_dbm']:g} dBm、局部突出度≥{result['config']['min_prominence_db']:g} dB、峰间距≥{result['config']['min_distance_nm']:g} nm，突出度窗口为±{result['config']['prominence_half_window_nm']:g} nm。首个有效帧选取最强{result['config']['channels']}峰，按波长升序标记通道，此后按波长连续性作有序一对一匹配，不按功率重新排序。"
    limits='连续匹配门限±0.08 nm，相对上一有效位置跟踪，不限制累计漂移。失锁后用至少80%通道估计共同位移，残差门限0.025 nm，连续3帧确认后重捕获；接近半个通道间距或存在编号别名时标记身份歧义。未检出、失锁及确认阶段保留空值，不用边界或旧值补齐。峰位/功率用原始dBm峰顶三点拟合，同时保留采样值。中心波长指峰顶，不是质心；功率指峰值谱电平，不是积分功率，拟合小数不代表仪器分辨率提升。'
    conditions='首尾比较使用各30秒有效数据均值。AQ6370E连续扫描的X/Y分开读取，不能证明每份快照均为独立、同一次完成扫描；RES未逐帧查询。参数、初始十峰、逐帧匹配状态均已保存，可复查。'
    table=[['通道','初期波长 nm','末期波长 nm','变化 pm','峰值变化 dB','缺失帧']]
    for s in result['channels']:
        table.append([s['channel'],f"{s['initial_nm']:.5f}",f"{s['latest_nm']:.5f}",f"{s['wavelength_change_pm']:+.2f}",f"{s['power_change_db']:+.3f}",str(s['missing_frames'])])
    conclusion='；'.join(f"{s['channel']}：{s['wavelength_change_pm']:+.2f} pm / {s['power_change_db']:+.3f} dB" for s in result['channels'])+'。'
    grouped_md='\n\n'.join(f'## {label}\n\n![五通道趋势]({filename})\n\n{text}' for label,filename,text in groups)
    md='# '+heading+'\n\n'+meta+'\n\n'+correction+'\n\n## 各通道结果\n\n|'+'|'.join(table[0])+'|\n|'+'|'.join(['---']*6)+'|\n'+'\n'.join('|'+'|'.join(r)+'|' for r in table[1:])+'\n\n'+grouped_md+'\n\n'+method+'\n\n'+limits+'\n\n'+conditions
    (out/'dynamic_channel_report.md').write_text(md,encoding='utf-8')
    import base64
    trs=''.join('<tr>'+''.join('<td>'+html.escape(v)+'</td>' for v in r)+'</tr>' for r in table)
    group_html=''.join('<h2>'+label+'</h2><img src="data:image/png;base64,'+base64.b64encode((out/filename).read_bytes()).decode()+'"><p>'+text+'</p>' for label,filename,text in groups)
    (out/'dynamic_channel_report.html').write_text('<!doctype html><meta charset="utf-8"><style>body{font-family:Microsoft YaHei;max-width:1100px;margin:35px auto;line-height:1.8;color:#172033}table{width:100%;border-collapse:collapse}td{padding:7px;border-bottom:1px solid #ddd}img{width:100%}</style><h1>'+heading+'</h1><p>'+meta+'</p><p>'+correction+'</p><table>'+trs+'</table>'+group_html+'<p>'+method+'</p><p>'+limits+'</p><p>'+conditions+'</p>',encoding='utf-8')
    from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,Table,TableStyle,PageBreak,Image
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib import colors
    import matplotlib.font_manager as fm
    pdfmetrics.registerFont(TTFont('CN',fm.findfont(fm.FontProperties(family='SimHei'))))
    normal=ParagraphStyle('normal',fontName='CN',fontSize=10,leading=16,spaceAfter=10)
    title=ParagraphStyle('title',parent=normal,fontSize=21,leading=29)
    tab=Table(table,colWidths=[55,105,105,70,95,75])
    tab.setStyle(TableStyle([('FONTNAME',(0,0),(-1,-1),'CN'),('FONTSIZE',(0,0),(-1,-1),9),('BACKGROUND',(0,0),(-1,0),colors.HexColor('#e8eef5')),('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),7),('LINEBELOW',(0,0),(-1,-1),.3,colors.lightgrey)]))
    story=[Paragraph(heading,title),Paragraph(meta,normal),Paragraph(correction,normal),tab,Spacer(1,14),Paragraph(method,normal),Paragraph(limits,normal),Paragraph(conditions,normal)]
    for label,filename,text in groups:
        story.extend([PageBreak(),Paragraph(label+'：五通道趋势分析',title),Image(str(out/filename),width=505,height=413.18),Spacer(1,15),Paragraph(text,normal),Paragraph('两组纵轴范围一致；每条曲线的通道身份始终不变，图例括号为首尾30秒均值之差。',normal)])
    pdf=out/'dynamic_channel_report.pdf'
    SimpleDocTemplate(str(pdf),leftMargin=35,rightMargin=35,topMargin=30,bottomMargin=30).build(story)
    import pymupdf
    with pymupdf.open(pdf) as d:
        for i,p in enumerate(d):p.get_pixmap(matrix=pymupdf.Matrix(1.2,1.2)).save(out/f'preview_{i+1}.png')
    Path('output/latest_channel_report.txt').write_text(str(out.resolve()),encoding='utf-8')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--folder',type=Path);parser.add_argument('--out',type=Path)
    parser.add_argument('--channels',type=int,help='重分析的通道数量（默认读取会话配置，旧会话默认10）')
    parser.add_argument('--peak-distance',type=float,help='重分析的最小峰间距(nm)，默认读取会话配置')
    args=parser.parse_args()
    folder=args.folder or Path(Path('recordings/latest.txt').read_text().strip())
    out=args.out or Path('output/pdf')/('dynamic_channels_'+datetime.now().strftime('%Y%m%d_%H%M%S'))
    replay(folder,out,args.channels,args.peak_distance)

