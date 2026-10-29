"""Render audited afternoon results, emphasizing measured spectral changes and loss of identity."""
import sys
import os
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
os.chdir(ROOT)
sys.path.insert(0,'.tools')
import base64
import gzip
import html
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from PIL import Image as PILImage
from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,Table,TableStyle,PageBreak,Image,KeepTogether
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import matplotlib.font_manager as fm
import pymupdf

OUT=Path('output/pdf/afternoon_summary_20261015')
s=json.loads((OUT/'afternoon_analysis.json').read_text(encoding='utf-8'))
frames=json.load(gzip.open(OUT/'frame_analysis.json.gz','rt',encoding='utf-8'))
plt.rcParams['font.sans-serif']=['Microsoft YaHei'];plt.rcParams['axes.unicode_minus']=False
def dt(v):return datetime.fromisoformat(v)
sessions=s['sessions'];events=s['events'];channel_stats=s['channel_stats']
states=Counter()
for session in sessions:states.update(session['states'])
lost_count=sum(states[k] for k in ('lost_lock','ambiguous_identity','reacquiring'))
loss_seconds=sum(e['observed_seconds'] for e in events)
span=(dt(s['cutoff'])-dt(s['start'])).total_seconds()
max_event=max(events,key=lambda e:e['observed_seconds'])
by_session={session['session']:[r for r in frames if r['session']==session['session']] for session in sessions}
shades={'tracking':'#16a34a','partial_detection':'#eab308','lost_lock':'#dc2626',
        'ambiguous_identity':'#9333ea','reacquiring':'#f97316','reacquired':'#06b6d4','no_peaks':'#475569'}
state_names={'tracking':'正常跟踪','partial_detection':'部分峰未检出','lost_lock':'通道失锁',
             'ambiguous_identity':'身份歧义','reacquiring':'重捕获确认','reacquired':'已重捕获','no_peaks':'无有效峰'}
# Timeline: observation segments only. White background explicitly retains acquisition gaps.
fig,axs=plt.subplots(2,1,figsize=(11,7),layout='constrained',sharex=True,gridspec_kw={'height_ratios':[1,1.8]})
used=set()
for si,session in enumerate(sessions):
    rows=by_session[session['session']]
    for i,r in enumerate(rows):
        end=dt(rows[i+1]['timestamp']) if i+1<len(rows) else dt(r['timestamp'])
        width=(end-dt(r['timestamp'])).total_seconds()
        if width>3 or width<=0:continue
        state=r['state'];label=state_names.get(state,state) if state not in used else None;used.add(state)
        axs[0].broken_barh([(mdates.date2num(dt(r['timestamp'])),width/86400)],(si-.3,.6),facecolors=shades.get(state,'gray'),label=label)
    axs[1].plot([dt(r['timestamp']) for r in rows],[r['candidates'] for r in rows],'.',ms=1.2,color='#2563eb')
axs[0].set(yticks=range(len(sessions)),yticklabels=[f'S{i+1}' for i in range(len(sessions))],ylabel='采集轮次',title='状态时间线（空白为未采集；紫色为身份歧义，红色为失锁）')
axs[0].set_title('状态时间线（空白为未采集；紫色为身份歧义，红色为失锁）',pad=35)
axs[0].legend(ncol=4,fontsize=8,loc='lower left',bbox_to_anchor=(0,1.02))
axs[1].set(ylabel='每帧有效候选峰数',xlabel='2026-10-15 本地时间',title='失锁期间仍检测到多个谱峰：不能解释为光信号消失')
for ax in axs:ax.grid(alpha=.2);ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'));ax.xaxis.set_major_locator(mdates.MinuteLocator(byminute=[0,20,40]))
fig.savefig(OUT/'lock_timeline.png',dpi=170);plt.close(fig)

# Evidence around the longest fully observed event, using original frames.
event_rows=by_session[max_event['session']]
before=next(r for r in reversed(event_rows) if r['index']<max_event['first_index'] and r['state']=='tracking')
during=next(r for r in event_rows if r['index']>=max_event['first_index'] and r['state']=='ambiguous_identity')
after=next(r for r in event_rows if r['index']>max_event['last_index'] and r['state']=='tracking')
evidence=[]
for label,r in [('失锁前',before),('失锁中',during),('恢复后',after)]:
    raw=json.load(gzip.open(Path('recordings')/r['session']/f"spectrum_{r['index']:05d}.json.gz",'rt',encoding='utf-8'))
    evidence.append((label,r,raw))
fig,axs=plt.subplots(2,1,figsize=(11,8),layout='constrained')
for color,(label,r,raw) in zip(('#2563eb','#dc2626','#16a34a'),evidence):
    x=np.array(raw['wavelength_nm']);y=np.array(raw['power'])
    axs[0].plot(x,y,lw=.8,color=color,label=f"{label} {r['timestamp'][11:19]}")
    mask=(x>=1307.4)&(x<=1310.2)
    axs[1].plot(x[mask],y[mask],lw=1.2,color=color,label=label)
axs[0].set(xlabel='波长（nm）',ylabel='功率（dBm）',title='原始光谱叠加：整体峰列位置发生显著变化')
axs[1].set(xlabel='波长（nm）',ylabel='功率（dBm）',title='局部放大：约半个通道间距的谱峰位置切换')
for ax in axs:ax.grid(alpha=.2);ax.legend(fontsize=9)
fig.savefig(OUT/'loss_spectral_evidence.png',dpi=170);plt.close(fig)

# Per-session baselines and disconnected segments avoid claims about gaps.
series=[]
for si,session in enumerate(sessions):
    rows=by_session[session['session']]
    for ch in range(10):
        key=f'CH{ch+1:02d}'
        stat=next(a for a in channel_stats if a['session']==session['session'] and a['channel']==key)
        ts=[];nm=[];pw=[];previous=None
        for r in rows:
            when=dt(r['timestamp'])
            if previous and (when-previous).total_seconds()>3:
                ts.append(when);nm.append(np.nan);pw.append(np.nan)
            p=next((p for p in r['channels'] if p['channel']==key),None)
            ts.append(when)
            nm.append((p['peak_nm']-stat['initial_nm'])*1000 if p and p['status']=='ok' and stat['initial_nm'] is not None else np.nan)
            pw.append(p['peak_dbm']-stat['initial_dbm'] if p and p['status']=='ok' and stat['initial_dbm'] is not None else np.nan)
            previous=when
        series.append((si,ch,ts,np.array(nm),np.array(pw)))
ranges=[]
for k in (3,4):
    low=min(float(np.nanmin(a[k])) for a in series);high=max(float(np.nanmax(a[k])) for a in series)
    pad=max((high-low)*.05,.01);ranges.append((low-pad,high+pad))
for group in (0,1):
    fig,axs=plt.subplots(2,1,figsize=(11,8.3),layout='constrained',sharex=True)
    seen=set()
    for si,ch,ts,nm,pw in series:
        if ch//5!=group:continue
        color=plt.get_cmap('tab10')(ch);label=f'CH{ch+1:02d}' if ch not in seen else None;seen.add(ch)
        axs[0].plot(ts,nm,lw=.7,color=color,label=label)
        axs[1].plot(ts,pw,lw=.7,color=color,label=label)
    for ax,yl in zip(axs,ranges):
        ax.set_ylim(*yl);ax.grid(alpha=.2);ax.legend(ncol=5,fontsize=9)
        for e in events:ax.axvspan(dt(e['start']),dt(e['end']),color='#dc2626',alpha=.12)
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'));ax.xaxis.set_major_locator(mdates.MinuteLocator(byminute=[0,20,40]))
    axs[0].set(ylabel='相对本轮首30秒波长变化（pm）')
    axs[1].set(ylabel='相对本轮首30秒峰值功率变化（dB）',xlabel='2026-10-15 本地时间（各轮独立基准；缺口不连线）')
    fig.suptitle(f'CH{group*5+1:02d} - CH{group*5+5:02d}：动态寻峰趋势（红色阴影为身份失锁事件）',fontsize=13)
    fig.savefig(OUT/f'channels_group_{group+1}.png',dpi=170);plt.close(fig)

# Normalize the saved CUA screenshot encoding for document embedding.
with PILImage.open(OUT/'main_ui.png') as im:im.convert('RGB').save(OUT/'main_ui_clean.png')

pdfmetrics.registerFont(TTFont('CN',fm.findfont(fm.FontProperties(family='SimHei'))))
styles={
    'body':ParagraphStyle('body',fontName='CN',fontSize=10,leading=15.5,spaceAfter=9),
    'small':ParagraphStyle('small',fontName='CN',fontSize=8.5,leading=12.5,spaceAfter=7),
    'title':ParagraphStyle('title',fontName='CN',fontSize=22,leading=29,spaceAfter=12,textColor=colors.HexColor('#123653')),
    'heading':ParagraphStyle('heading',fontName='CN',fontSize=14,leading=20,spaceAfter=9,spaceBefore=8,textColor=colors.HexColor('#123653'))}
def P(text,style='body'):return Paragraph(html.escape(text),styles[style])
def T(rows,widths,fontsize=8.5):
    processed=[[P(str(v),'small') for v in r] for r in rows]
    table=Table(processed,colWidths=widths,repeatRows=1,hAlign='LEFT')
    table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#e8eff6')),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),5),('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4),('LINEBELOW',(0,0),(-1,-1),.3,colors.HexColor('#d8dfe6'))]))
    return table
def IMG(name,width=515):
    with PILImage.open(OUT/name) as im:w,h=im.size
    return Image(str(OUT/name),width=width,height=width*h/w)

md_sections=[];html_sections=[]
def section(title,paragraphs=(),image=None):
    md_sections.append('## '+title+'\n\n'+'\n\n'.join(paragraphs)+(f'\n\n![{title}]({image})' if image else ''))
    html_sections.append('<h2>'+html.escape(title)+'</h2>'+''.join('<p>'+html.escape(p)+'</p>' for p in paragraphs)+(f'<img src="data:image/png;base64,{base64.b64encode((OUT/image).read_bytes()).decode()}">' if image else ''))

title='下午光谱监控汇总与波长失锁分析'
subtitle=f"2026-10-15 | {s['start'][11:19]} 至 {s['cutoff'][11:19]} | 5轮记录 | {s['total_frames']:,}份有效快照"
executive=f"主要发现：原始光谱中反复出现约0.57 nm、接近半个峰间距的整组位置变化，导致十通道身份无法唯一匹配。统一回放识别出{len(events)}段失锁事件，涉及{lost_count}帧（{lost_count/s['total_frames']*100:.2f}%）；这些片段仍有14至15个有效候选峰，属于有光谱但通道身份失锁。"
coverage=f"可用数据覆盖{s['start'][11:19]}至{s['cutoff'][11:19]}，总时间跨度{span/60:.1f}分钟；扣除轮间停采及超过3秒的轮内缺口，实际有连续相邻快照支撑约{s['covered_seconds']/60:.1f}分钟。更早已删除的缓存未纳入。13,294份为快照数量，不代表独立扫描次数。"
key_event=f"最长连续可观测事件：{max_event['start'][11:19]}至{max_event['end'][11:19]}，持续至少{max_event['observed_seconds']:.1f}秒，{max_event['frames']}帧；{max_event['recovery'][11:19]}重新出现可匹配峰列。18:01:09后的事件在18:02:26仍未恢复，随后采集缺口约8分钟，不能推定整个缺口持续失锁或给出精确恢复时刻。"
interpretation='本报告的“波长失锁”特指监控中谱峰身份失锁/歧义，已测得大幅谱峰位置切换；不等同于已确认光源锁波控制失效。用户在AQ6370E自身WDM功能也观察到大幅波长变化，属于仪器侧佐证，但未取得该界面的同步截图、导出值或控制器锁定状态。'
story=[P(title,'title'),P(subtitle,'small'),P('核心结论：反复出现半通道间距量级的位置切换','heading'),P(executive),P(key_event),P(interpretation),P('数据范围与质量','heading'),P(coverage)]
session_table=[['轮次','时间范围','有效快照','支持覆盖 min','身份失锁帧']]
for i,a in enumerate(sessions):session_table.append([f'S{i+1}',a['start'][11:19]+' - '+a['end'][11:19],a['valid_frames'],f"{a['covered_seconds']/60:.1f}",sum(a['states'].get(k,0) for k in ('lost_lock','ambiguous_identity','reacquiring'))])
story.extend([T(session_table,[42,170,85,110,108]),Spacer(1,8),P('身份失锁统计由当前V2算法统一回放得到，不混用旧版固定波长结论；原始记录与当时在线状态保留。','small')])
section('核心结论',[executive,key_event,interpretation,coverage])

ui_texts=[
    '连接与控制：设置AQ6370E地址、控制端口和TRA轨迹，启动/停止监测并导出当前光谱。程序读取仪器光谱，不改变仪器扫描参数。',
    '记录与显示：记录间隔和UI刷新间隔独立设置，点击“应用间隔”可运行中生效；截图中为0.5秒记录、2秒显示。实际速度受仪器响应及保存耗时限制。',
    '动态寻峰：逐帧搜索有效峰，按通道身份匹配，光谱图上1至10标号随峰位移动；下方表格显示峰顶拟合波长、峰值功率、突出度和状态。',
    '失锁处置：区分未检出、通道失锁、重捕获确认、身份歧义。可辨认的共同位移须连续3帧确认；歧义不补造数值。主动“重新识别十通道”会建立新编号段，不能直接拼接成同一物理通道。']
story.extend([PageBreak(),P('主UI截图与主要功能','title'),P(s['screenshot_caption'],'small'),IMG('main_ui_clean.png'),Spacer(1,10)])
for p in ui_texts:story.append(P(p,'small'))
section('主UI截图与主要功能',[s['screenshot_caption'],*ui_texts],image='main_ui_clean.png')

event_table=[['事件','开始 - 最后异常帧','帧数','已观测 s','恢复/边界']]
for i,e in enumerate(events):event_table.append([f'E{i+1}',e['start'][11:19]+' - '+e['end'][11:19],e['frames'],f"{e['observed_seconds']:.1f}",e['recovery'][11:19] if e['recovery'] else '缺口前未恢复'])
event_text=f"六段事件中，失锁/歧义帧合计{lost_count}帧，异常帧首末之间连续观测时长合计{loss_seconds:.1f}秒（约{loss_seconds/60:.2f}分钟，下界）；未加入两帧之间未知的精确起止，也未加入停采时间。状态计数：正常跟踪{states['tracking']}帧、身份歧义{states['ambiguous_identity']}帧、失锁{states['lost_lock']}帧、部分通道未检出{states['partial_detection']}帧。未出现全谱无有效峰。"
story.extend([PageBreak(),P('波长失锁事件时间线','title'),T(event_table,[40,190,55,80,150]),Spacer(1,8),P(event_text,'small'),IMG('lock_timeline.png',505)])
section('波长失锁事件时间线',[event_text],image='lock_timeline.png')

evidence_text=f"选取E5（最长事件）的三个原始快照：失锁前{before['timestamp'][11:23]}、失锁中{during['timestamp'][11:23]}、恢复后{after['timestamp'][11:23]}。失锁前后峰列接近原位置，异常期间主要峰列整体切换约半个峰间距。共同位移存在约−0.569 nm和+0.570 nm两种相邻通道编号解释，不能直接将其中一个符号作为同一CH的真实漂移方向。"
why='约1.14 nm周期的峰列整体偏移约0.57 nm时，原通道左右相邻峰都可能成为候选。简单放宽±0.08 nm门限会有串号风险；按峰高重新选十峰也会改变身份。新版保持原身份并标记歧义，保留完整原谱和诊断候选，待返回原峰列或获得独立身份依据后恢复。'
story.extend([PageBreak(),P('重点事件：异常前、失锁中、恢复后','title'),P(evidence_text,'small'),IMG('loss_spectral_evidence.png',505),Spacer(1,8),P(why,'small')])
section('最长失锁事件的原始光谱证据',[evidence_text,why],image='loss_spectral_evidence.png')

first_session=sessions[0]['session'];last_session=sessions[-1]['session']
for group in (0,1):
    selected=[a for a in channel_stats if a['session']==first_session and group*5<int(a['channel'][2:])<=group*5+5]
    low=min(a['power_change_db'] for a in selected);high=max(a['power_change_db'] for a in selected)
    wlow=min(a['wavelength_change_pm'] for a in selected);whigh=max(a['wavelength_change_pm'] for a in selected)
    text=f"第一轮长时记录中，本组首尾30秒均值比较：峰顶波长变化{wlow:+.2f}至{whigh:+.2f} pm，峰值功率变化{low:+.3f}至{high:+.3f} dB。图中红色阴影为身份失锁事件，期间通道数值留空，不把约0.57 nm的歧义跳变硬连成漂移曲线。"
    note='各轮分别以本轮最初30秒有效数据为零点，停采缺口不连线。两组纵轴相同；后续轮次回到零附近不表示光源瞬时复原，而是新一轮独立基准。CH按初始波长顺序对应，停采期间无法证明物理通道身份连续。'
    image=f'channels_group_{group+1}.png';label=f'CH{group*5+1:02d} - CH{group*5+5:02d} 动态监控'
    story.extend([PageBreak(),P(label,'title'),IMG(image,515),Spacer(1,10),P(text),P(note,'small')])
    section(label,[text,note],image=image)

story.extend([PageBreak(),P('十通道量化结果与方法边界','title')])
for label,session in [('S1：第一轮长时记录',first_session),('S5：最新一轮稳定记录',last_session)]:
    table=[['CH','初期 nm','末期 nm','变化 pm','峰值变化 dB','未分配帧']]
    for a in channel_stats:
        if a['session']!=session:continue
        table.append([a['channel'],f"{a['initial_nm']:.5f}" if a['initial_nm'] is not None else '—',f"{a['latest_nm']:.5f}" if a['latest_nm'] is not None else '—',f"{a['wavelength_change_pm']:+.2f}" if a['wavelength_change_pm'] is not None else '—',f"{a['power_change_db']:+.3f}" if a['power_change_db'] is not None else '—',a['unassigned_frames']])
    story.extend([P(label,'heading'),T(table,[45,105,105,80,95,85]),Spacer(1,6)])
method='每帧全谱局部极大值搜索：峰值≥−40 dBm、局部突出度≥6 dB、最小峰间距0.70 nm、突出度窗口±0.25 nm。首有效帧选最强十峰后保持身份；三点抛物线拟合峰顶，并保留采样值。失锁时至少80%通道共同位移一致（残差≤0.025 nm）、连续3帧后才重捕获；近半个通道间距的竞争解释保留歧义。'
limits='这里中心波长指峰顶位置，功率指峰值谱电平，不是谱线质心或通道积分功率。0.01 nm为采样间隔，RES未逐帧记录；拟合小数不代表测量精度。连续扫描X/Y分开读取，无法保证每份快照均为同一完整扫描；自动调零、激光温度、电流、锁定指示和扫描完成标志尚未同步保存，因此不对失锁原因作归因。'
# Keep quantitative tables together; methodology receives its own final page.
story.extend([PageBreak(),P('数据审计、判断边界与后续验证','title'),P(method),P(limits),P('数据质量','heading')])
quality=f"共核验{len(sessions)}轮、{s['total_frames']}份原始文件，时间戳、TRA轨迹、dBm单位和各轮波长网格一致；全部通过当前算法有效性检查。相邻完全相同快照合计{sum(a['identical_adjacent_snapshots'] for a in sessions)}对，不能视为独立扫描。旧固定点分析已停用，未纳入本报告。"
story.append(P(quality))
gap_table=[['缺口开始','下一份数据','未采集/无快照支撑 s']]
for g in sorted(s['gaps'],key=lambda g:g['start']):gap_table.append([g['start'][11:19],g['end'][11:19],f"{g['seconds']:.1f}"])
story.extend([T(gap_table,[155,155,205]),Spacer(1,12),P('建议下一步','heading'),P('优先同步记录AQ6370E WDM分析数值或画面、扫描完成状态，以及光源温度、电流、锁波/告警状态；用共同时间戳对齐下一次约半通道间距跳变。核对是否伴随模式切换、测试条件变化或仪器扫描/调零过程，形成可验证的因果证据。'),P('随附审计文件','heading'),P('input_manifest.json记录冻结边界；afternoon_analysis.json保存汇总与事件；all_channel_peaks.csv保存全部逐帧通道值及状态；lock_events.csv、channel_changes_by_session.csv、session_summary.csv、acquisition_gaps.csv可独立复算。','small')])
section('方法、数据质量与后续验证',[method,limits,quality,'优先同步WDM、扫描完成和光源锁定/温度/电流状态，以共同时间戳复核下一次跳变。'])

def footer(canvas,doc):
    canvas.setFont('CN',8);canvas.setFillColor(colors.HexColor('#64748b'))
    canvas.drawString(35,18,'AQ6370E | 2026-10-15 下午汇总 | 截止 '+s['cutoff'][11:19])
    canvas.drawRightString(A4[0]-35,18,str(doc.page))
pdf=OUT/'下午光谱监控与波长失锁分析报告.pdf'
SimpleDocTemplate(str(pdf),pagesize=A4,leftMargin=35,rightMargin=35,topMargin=30,bottomMargin=32,title=title,author='光谱监控分析').build(story,onFirstPage=footer,onLaterPages=footer)
(OUT/'下午光谱监控与波长失锁分析报告.md').write_text('# '+title+'\n\n'+subtitle+'\n\n'+'\n\n'.join(md_sections),encoding='utf-8')
(OUT/'下午光谱监控与波长失锁分析报告.html').write_text('<!doctype html><html lang="zh"><meta charset="utf-8"><title>'+title+'</title><style>body{font-family:Microsoft YaHei;max-width:1100px;margin:35px auto;color:#172033;line-height:1.8}img{width:100%}h2{color:#123653;border-bottom:1px solid #ddd}</style><h1>'+title+'</h1><p>'+subtitle+'</p>'+''.join(html_sections)+'</html>',encoding='utf-8')
with pymupdf.open(pdf) as doc:
    texts=[]
    for i,page in enumerate(doc):
        page.get_pixmap(matrix=pymupdf.Matrix(1.2,1.2)).save(OUT/f'preview_{i+1}.png')
        texts.append(page.get_text())
    assert all(len(t)>70 for t in texts)
    validation=dict(pages=len(doc),total_frames=s['total_frames'],cutoff=s['cutoff'],events=len(events),
        lost_identity_frames=lost_count,loss_observed_lower_bound_s=loss_seconds,states=dict(states),
        invalid_raw_frames=sum(len(a['invalid_frames']) for a in sessions),screenshot_timestamp='2026-10-15 18:26:43')
(OUT/'report_validation.json').write_text(json.dumps(validation,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(validation,ensure_ascii=False,indent=2))
