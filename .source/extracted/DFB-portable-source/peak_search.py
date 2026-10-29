"""Per-frame peak detection and persistent wavelength-ordered channel identities.

Pure Python: no scipy dependency in the acquisition process. Power is peak
spectral level in dBm, not integrated channel power. Fits do not improve RES.
"""
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
import math
import statistics


@dataclass(frozen=True)
class SearchConfig:
    channels: int = 16
    min_power_dbm: float = -40.0
    min_prominence_db: float = 6.0
    prominence_half_window_nm: float = 0.25
    min_distance_nm: float = 0.30
    match_gate_nm: float = 0.08
    reacquire_tolerance_nm: float = 0.025
    reacquire_confirm_frames: int = 3

    def __post_init__(self):
        if type(self.channels) is not int or not 1 <= self.channels <= 100:
            raise ValueError('通道数量必须为 1–100 的整数')
        if not math.isfinite(self.min_distance_nm) or self.min_distance_nm < 0.3:
            raise ValueError('最小峰间距必须为不小于 0.3 nm 的有限数值')


def search_peaks(x, y, config=SearchConfig()):
    """Local maxima (plateau midpoint), bounded prominence, distance suppression.

    Prominence walks each side until a higher sample or the bounded window;
    the larger side minimum defines the local contour. Both sides must be
    fully inside the scan, so scan-edge maxima are never reported as peaks.
    Three-point concave quadratic fit on raw dBm uses actual x coordinates.
    """
    if len(x) != len(y) or len(x) < 5:
        raise ValueError('Invalid spectrum length')
    if not all(math.isfinite(v) for v in (*x, *y)) or any(b <= a for a,b in zip(x,x[1:])):
        raise ValueError('Spectrum must be finite and wavelength strictly increasing')
    candidates=[]
    i=1
    while i < len(y)-1:
        j=i
        while j+1 < len(y) and y[j+1] == y[i]: j+=1
        if j < len(y)-1 and y[i] > y[i-1] and y[j] > y[j+1]:
            k=(i+j)//2
            if y[k] >= config.min_power_dbm and x[k]-x[0] >= config.prominence_half_window_nm and x[-1]-x[k] >= config.prominence_half_window_nm:
                lo=bisect_left(x,x[k]-config.prominence_half_window_nm)
                hi=bisect_right(x,x[k]+config.prominence_half_window_nm)-1
                floors=[]
                for indices in (range(k-1,lo-1,-1),range(k+1,hi+1)):
                    floor=y[k]
                    for q in indices:
                        if y[q] > y[k]: break
                        floor=min(floor,y[q])
                    floors.append(floor)
                prominence=y[k]-max(floors)
                if prominence >= config.min_prominence_db:
                    center,power=x[k],y[k]
                    fit=False
                    if i == j:
                        left,right=x[k-1]-x[k],x[k+1]-x[k]
                        dl,dr=y[k-1]-y[k],y[k+1]-y[k]
                        a=(dl/left-dr/right)/(left-right)
                        b=dl/left-a*left
                        if a < 0:
                            offset=-b/(2*a)
                            if left/2 <= offset <= right/2:
                                center=x[k]+offset
                                power=y[k]+a*offset*offset+b*offset
                                fit=True
                    candidates.append(dict(sample_index=k,sampled_nm=x[k],sampled_dbm=y[k],
                        peak_nm=center,peak_dbm=power,prominence_db=prominence,fit_valid=fit))
        i=j+1
    accepted=[]
    for p in sorted(candidates,key=lambda p:p['sampled_dbm'],reverse=True):
        if all(abs(p['peak_nm']-q['peak_nm']) >= config.min_distance_nm for q in accepted):
            accepted.append(p)
    return sorted(accepted,key=lambda p:p['peak_nm'])


class ChannelTracker:
    def __init__(self, config=SearchConfig()):
        self.config=config
        self.anchors=[]
        self.previous=[]
        self.frame=0
        self.pending_shift=None
        self.pending_count=0

    def _assign(self,peaks,targets,gate):
        # Ordered one-to-one assignment: skip an extra peak at zero cost,
        # mark missing channel at cost 1.1, match within gate at cost <=1.
        # This avoids relabeling channels when their relative powers change.
        n,m=len(targets),len(peaks)
        costs=[[float('inf')]*(m+1) for _ in range(n+1)]
        paths=[[None]*(m+1) for _ in range(n+1)]
        costs[0][0]=0
        for i in range(n+1):
            for j in range(m+1):
                c=costs[i][j]
                moves=[]
                if j<m: moves.append((i,j+1,c,'extra'))
                if i<n: moves.append((i+1,j,c+1.1,'missing'))
                if i<n and j<m:
                    gap=abs(peaks[j]['peak_nm']-targets[i])
                    if gap <= gate:
                        moves.append((i+1,j+1,c+(gap/gate)**2,'match'))
                for a,b,value,action in moves:
                    if value < costs[a][b]:
                        costs[a][b]=value;paths[a][b]=(i,j,action)
        assignments={}
        i,j=n,m
        while i or j:
            a,b,action=paths[i][j]
            if action == 'match': assignments[a]=peaks[b]
            i,j=a,b
        return assignments

    def update(self,x,y,unit='dBm'):
        if unit != 'dBm': raise ValueError('Channel tracking requires dBm spectra')
        peaks=search_peaks(x,y,self.config)
        self.frame+=1
        if not self.anchors:
            if len(peaks) < self.config.channels:
                return dict(frame=self.frame,status='waiting_for_peaks',candidate_count=len(peaks),channels=[])
            initial=sorted(sorted(peaks,key=lambda p:p['peak_dbm'],reverse=True)[:self.config.channels],key=lambda p:p['peak_nm'])
            self.anchors=[p['peak_nm'] for p in initial]
            self.previous=self.anchors[:]
        n=len(self.anchors)
        spacing=min(b-a for a,b in zip(self.previous,self.previous[1:])) if n>1 else 1.
        gate=min(self.config.match_gate_nm,spacing*.2)
        assignments=self._assign(peaks,self.previous,gate)
        quorum=min(n,max(2,math.ceil(n*.8)))
        overall='tracking' if len(assignments)==n else 'partial_detection'
        reason=''
        shifts=[]
        if len(assignments)<quorum:
            # Estimate translations of the entire comb, not individual wide
            # channel windows. Keep alternative orders to detect half-spacing
            # aliases in a periodic spectrum.
            offsets=sorted(p['peak_nm']-v for v in self.previous for p in peaks
                           if abs(p['peak_nm']-v)<=spacing*.75)
            clusters=[]
            for offset in offsets:
                if not clusters or offset-statistics.median(clusters[-1])>self.config.reacquire_tolerance_nm:
                    clusters.append([offset])
                else:clusters[-1].append(offset)
            hypotheses=[]
            for cluster in clusters:
                shift=statistics.median(cluster)
                matched=self._assign(peaks,[v+shift for v in self.previous],self.config.reacquire_tolerance_nm)
                if len(matched)>=quorum:
                    residual=statistics.mean(abs(p['peak_nm']-self.previous[i]-shift) for i,p in matched.items())
                    hypotheses.append((shift,matched,residual))
            hypotheses.sort(key=lambda h:(-len(h[1]),abs(h[0]),h[2]))
            shifts=[round(h[0],6) for h in hypotheses]
            if hypotheses:
                shift,matched,_=hypotheses[0]
                aliased=abs(shift)>=spacing*.4 or any(
                    len(h[1])>=len(matched)-1 and abs(h[0]-shift)>spacing*.7
                    and abs(abs(h[0])-abs(shift))<spacing*.2 for h in hypotheses[1:])
                if aliased:
                    overall='ambiguous_identity';reason='comb_shift_near_half_spacing_or_alias'
                    assignments={};self.pending_count=0;self.pending_shift=None
                else:
                    if self.pending_shift is not None and abs(shift-self.pending_shift)<=self.config.reacquire_tolerance_nm:
                        self.pending_count+=1
                    else:self.pending_count=1
                    self.pending_shift=shift
                    if self.pending_count>=self.config.reacquire_confirm_frames:
                        assignments=matched;overall='reacquired';reason='common_shift_confirmed'
                        self.pending_count=0;self.pending_shift=None
                    else:
                        overall='reacquiring';reason='waiting_for_consecutive_common_shift'
                        assignments={}
            else:
                self.pending_count=0;self.pending_shift=None
                if not peaks:overall='no_peaks';reason='no_valid_peaks_detected'
                else:overall='lost_lock';reason='no_consistent_group_assignment'
                assignments={}
        else:
            self.pending_count=0;self.pending_shift=None
        channels=[]
        for i,anchor in enumerate(self.anchors):
            p=assignments.get(i)
            if p: self.previous[i]=p['peak_nm']
            status='ok' if p else ('not_detected' if overall in ('tracking','partial_detection','no_peaks') else overall)
            channels.append(dict(channel=f'CH{i+1:02d}',anchor_nm=anchor,status=status,reason='' if p else reason,
                                 **(p or dict(peak_nm=None,peak_dbm=None,sampled_nm=None,sampled_dbm=None,
                                             sample_index=None,prominence_db=None,fit_valid=False))))
        return dict(frame=self.frame,status=overall,reason=reason,candidate_count=len(peaks),channels=channels,
                    shift_hypotheses_nm=shifts,reacquire_confirm_count=self.pending_count)

    def metadata(self):
        return dict(algorithm='common-shift-confirmed-channel-tracking-v2',config=asdict(self.config),
                    initial_channel_wavelengths_nm=self.anchors,selection=f'{self.config.channels} strongest valid peaks in first valid frame, ordered by wavelength',
                    wavelength_definition='three-point quadratic peak apex; sampled apex retained',
                    power_definition='peak spectral level in dBm, not integrated power')
