import json, os, math, sys
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict

ET             = ZoneInfo('America/New_York')
OHLCV_DIR      = r'c:\Users\iamtc\Documents\DayTrading\GitHub\TCTradeJournal\dashboard\ohlcv'
CAL_PATH       = r'c:\Users\iamtc\Documents\DayTrading\GitHub\TCTradeJournal\dashboard\calendar\calendar_data_2026.json'
CUTOFF         = '2026-01-01'
SCRATCH        = 0.01
BE_MULT        = 1.005
RISK_PER_TRADE = 10.0
LOSS_CAP       = 1.25 * RISK_PER_TRADE  # 1.25R = $12.50

def load_ohlcv(ds, sym):
    path = os.path.join(OHLCV_DIR, f'ohlcv_{ds}.json')
    if not os.path.exists(path): return []
    with open(path) as f: d = json.load(f)
    return d.get(sym) or []

def bmin(b):
    dt = datetime.fromtimestamp(b['time'], tz=ET)
    return dt.hour*60+dt.minute

def to_min(hhmm):
    h,m = map(int,hhmm.split(':'))
    return h*60+m

def infer_dir(pnl,ep,xp):
    d=xp-ep
    if abs(d)<0.0001: return 'long'
    return 'long' if (pnl>=0)==(d>=0) else 'short'

def make_2min(bars):
    bars=sorted(bars,key=lambda b:b['time']); result=[]; i=0
    while i<len(bars):
        slot=(bmin(bars[i])//2)*2
        g=[bars[j] for j in range(i,len(bars)) if (bmin(bars[j])//2)*2==slot]
        result.append({'slot':slot,'high':max(x['high'] for x in g),
                       'low':min(x['low'] for x in g),'close':g[-1]['close']})
        i+=len(g)
    return result

def compute_macd(bars2, fast=12, slow=26, sig=9):
    """Returns dict: slot -> (macd_line, signal_line)"""
    if len(bars2) < slow + sig: return {}
    closes = [b['close'] for b in bars2]
    def ema_list(vals, p):
        k = 2.0/(p+1); out=[vals[0]]
        for v in vals[1:]: out.append(v*k + out[-1]*(1-k))
        return out
    ef=ema_list(closes,fast); es=ema_list(closes,slow)
    ml=[f-s for f,s in zip(ef,es)]
    sl=ema_list(ml,sig)
    return {bars2[i]['slot']:(ml[i],sl[i]) for i in range(len(bars2))}

def get_rps(rt, all_bars):
    em=to_min(rt['entry_time']); ep=rt['entry_price']
    dirn=infer_dir(rt['pnl'],ep,rt['exit_price'])
    all_s=sorted(all_bars,key=lambda b:b['time'])
    bars2=make_2min(all_s)
    prev2=[b2 for b2 in bars2 if b2['slot']<(em//2)*2]
    if not prev2: return None
    last2=prev2[-1]
    if dirn=='long': istop=last2['low']*0.99; Rps=ep-istop
    else:            istop=last2['high']*1.01; Rps=istop-ep
    if Rps<=0: return None
    return Rps, dirn, istop, bars2

def simulate(rt, all_bars, use_be_stop, use_macd_exit=False):
    em=to_min(rt['entry_time']); xm=to_min(rt['exit_time'])
    ep=rt['entry_price']
    res=get_rps(rt,all_bars)
    if res is None: return None
    Rps,dirn,istop,bars2=res
    all_s=sorted(all_bars,key=lambda b:b['time'])
    tb=[b for b in all_s if em<=bmin(b)<=xm]
    if not tb: return None

    qty=RISK_PER_TRADE/Rps
    be_stop=(ep*BE_MULT) if dirn=='long' else (ep/BE_MULT)
    macd_map=compute_macd(bars2) if use_macd_exit else {}

    slice_qty=qty*0.10; remaining=qty; pnl_total=0.0
    first_done=False; trail=istop; next_r=1

    def target(r): return (ep+Rps*r) if dirn=='long' else (ep-Rps*r)
    def effective_stop(tv):
        if not first_done or not use_be_stop: return tv
        return max(be_stop,tv) if dirn=='long' else min(be_stop,tv)

    for bar in tb:
        bm=bmin(bar)
        prev=[b2 for b2 in bars2 if b2['slot']<(bm//2)*2]
        if prev: trail=prev[-1]['low']*0.99 if dirn=='long' else prev[-1]['high']*1.01
        if remaining<=0: break

        if not first_done:
            if (dirn=='long' and bar['high']>=target(1)) or (dirn=='short' and bar['low']<=target(1)):
                pnl_total+=(target(1)-ep)*(qty*0.5) if dirn=='long' else (ep-target(1))*(qty*0.5)
                remaining-=qty*0.5; first_done=True; next_r=2
            elif (dirn=='long' and bar['low']<=istop) or (dirn=='short' and bar['high']>=istop):
                pnl_total+=(istop-ep)*remaining if dirn=='long' else (ep-istop)*remaining
                remaining=0; break
            elif use_macd_exit and remaining>0 and len(prev)>=2:
                s1,s2=prev[-1]['slot'],prev[-2]['slot']
                if s1 in macd_map and s2 in macd_map:
                    m1,sg1=macd_map[s1]; m2,sg2=macd_map[s2]
                    if dirn=='long' and m1<sg1 and m2<sg2 and bar['low']<=prev[-1]['low']*0.99:
                        xp=prev[-1]['low']*0.99
                        pnl_total+=(xp-ep)*remaining; remaining=0; break
                    elif dirn=='short' and m1>sg1 and m2>sg2 and bar['high']>=prev[-1]['high']*1.01:
                        xp=prev[-1]['high']*1.01
                        pnl_total+=(ep-xp)*remaining; remaining=0; break
        else:
            while next_r<=5 and remaining>0:
                if (dirn=='long' and bar['high']>=target(next_r)) or (dirn=='short' and bar['low']<=target(next_r)):
                    eq=min(slice_qty,remaining)
                    pnl_total+=(target(next_r)-ep)*eq if dirn=='long' else (ep-target(next_r))*eq
                    remaining-=eq; next_r+=1
                else: break
            if remaining>0:
                eff_stop=effective_stop(trail)
                if (dirn=='long' and bar['low']<=eff_stop) or (dirn=='short' and bar['high']>=eff_stop):
                    pnl_total+=(eff_stop-ep)*remaining if dirn=='long' else (ep-eff_stop)*remaining
                    remaining=0; break
            if use_macd_exit and remaining>0 and len(prev)>=2:
                s1,s2=prev[-1]['slot'],prev[-2]['slot']
                if s1 in macd_map and s2 in macd_map:
                    m1,sg1=macd_map[s1]; m2,sg2=macd_map[s2]
                    if dirn=='long' and m1<sg1 and m2<sg2 and bar['low']<=prev[-1]['low']*0.99:
                        xp=prev[-1]['low']*0.99
                        pnl_total+=(xp-ep)*remaining; remaining=0; break
                    elif dirn=='short' and m1>sg1 and m2>sg2 and bar['high']>=prev[-1]['high']*1.01:
                        xp=prev[-1]['high']*1.01
                        pnl_total+=(ep-xp)*remaining; remaining=0; break

    if remaining>0:
        fp=tb[-1]['close']
        pnl_total+=(fp-ep)*remaining if dirn=='long' else (ep-fp)*remaining
    return round(max(pnl_total,-LOSS_CAP),2)

with open(CAL_PATH) as f: data=json.load(f)
orig=[]
for yr,months in data.items():
    for mo,mdata in months.items():
        for day,ddata in (mdata.get('days') or {}).items():
            ds=f'{yr}-{mo.zfill(2)}-{day.zfill(2)}'
            if ds<CUTOFF: continue
            for rt in (ddata.get('roundtrips') or []): orig.append({**rt,'date':ds})

by_date=defaultdict(list)
for rt in orig: by_date[rt['date']].append(rt)
cache={}
for ds,drs in by_date.items():
    for sym in set(r['symbol'] for r in drs): cache[(ds,sym)]=load_ohlcv(ds,sym)

print(f'Running sims on {len(orig)} trades...', file=sys.stderr)
r_actual=[]; r_base=[]; r_be=[]; r_be_m=[]
for ds,drs in sorted(by_date.items()):
    for rt in drs:
        bars=cache.get((ds,rt['symbol']),[])
        if not bars: continue
        res=get_rps(rt,bars)
        if res is None: continue
        Rps,_,_,_=res
        norm_qty=RISK_PER_TRADE/Rps
        norm_pnl=round(max((rt['pnl']/rt['qty'])*norm_qty,-LOSS_CAP),2) if rt['qty']>0 else 0
        r_actual.append({**rt,'pnl':norm_pnl})
        pb=simulate(rt,bars,False)
        pbe=simulate(rt,bars,True)
        pbem=simulate(rt,bars,True,True)
        if pb   is not None: r_base.append({**rt,'pnl':pb})
        if pbe  is not None: r_be.append({**rt,'pnl':pbe})
        if pbem is not None: r_be_m.append({**rt,'pnl':pbem})

def stats(rts):
    wins=[r for r in rts if r['pnl']>SCRATCH]
    loss=[r for r in rts if r['pnl']<-SCRATCH]
    scrt=[r for r in rts if abs(r['pnl'])<=SCRATCH]
    n=len(rts); tot=sum(r['pnl'] for r in rts)
    gw=sum(r['pnl'] for r in wins); gl=abs(sum(r['pnl'] for r in loss))
    aw=gw/len(wins) if wins else 0; al=sum(r['pnl'] for r in loss)/len(loss) if loss else 0
    at=tot/n; pf=gw/gl if gl else None
    std=math.sqrt(sum((r['pnl']-at)**2 for r in rts)/(n-1)) if n>1 else 0
    sqn=(at/std)*math.sqrt(n) if std else None
    wr=len(wins)/n; kelly=wr-(1-wr)*(abs(al)/aw) if aw>0 else None
    byday=defaultdict(list)
    for r in rts: byday[r['date']].append(r)
    dp=[sum(r['pnl'] for r in v) for v in byday.values()]
    mxw=mxl=cw=cl=0
    for r in rts:
        if r['pnl']>SCRATCH: cw+=1;cl=0;mxw=max(mxw,cw)
        elif r['pnl']<-SCRATCH: cl+=1;cw=0;mxl=max(mxl,cl)
        else: cw=cl=0
    ts=sum(r['qty'] for r in rts)
    return dict(n=n,wins=len(wins),loss=len(loss),scrt=len(scrt),tot=tot,gw=gw,gl=-gl,
                aw=aw,al=al,at=at,pf=pf,std=std,sqn=sqn,kelly=kelly,
                ad=sum(dp)/len(dp) if dp else 0,days=len(byday),
                mxw=mxw,mxl=mxl,wr=wr*100,lg=max(r['pnl'] for r in rts),
                ll=min(r['pnl'] for r in rts),ts=int(ts),
                byday={k:sum(r['pnl'] for r in v) for k,v in byday.items()})

sa=stats(r_actual); sb=stats(r_base); sbe=stats(r_be); sbem=stats(r_be_m)

def f(v,dec=2,pct=False):
    if v is None: return 'N/A'
    if pct: return f'{v:.1f}%'
    return f'${v:+.{dec}f}'
def fn(v,dec=2):
    if v is None: return 'N/A'
    return f'{v:.{dec}f}'

rows=[
    ('Total PnL',       f(sa['tot']),  f(sb['tot']),  f(sbe['tot']),  f(sbem['tot'])),
    ('Gross Profit',    f(sa['gw']),   f(sb['gw']),   f(sbe['gw']),   f(sbem['gw'])),
    ('Gross Loss',      f(sa['gl']),   f(sb['gl']),   f(sbe['gl']),   f(sbem['gl'])),
    ('Profit Factor',   fn(sa['pf']),  fn(sb['pf']),  fn(sbe['pf']),  fn(sbem['pf'])),
    ('Win Rate',        f(sa['wr'],pct=True),f(sb['wr'],pct=True),f(sbe['wr'],pct=True),f(sbem['wr'],pct=True)),
    ('Avg Win',         f(sa['aw']),   f(sb['aw']),   f(sbe['aw']),   f(sbem['aw'])),
    ('Avg Loss',        f(sa['al']),   f(sb['al']),   f(sbe['al']),   f(sbem['al'])),
    ('Avg Trade',       f(sa['at']),   f(sb['at']),   f(sbe['at']),   f(sbem['at'])),
    ('Largest Gain',    f(sa['lg']),   f(sb['lg']),   f(sbe['lg']),   f(sbem['lg'])),
    ('Largest Loss',    f(sa['ll']),   f(sb['ll']),   f(sbe['ll']),   f(sbem['ll'])),
    ('Avg Daily PnL',   f(sa['ad']),   f(sb['ad']),   f(sbe['ad']),   f(sbem['ad'])),
    ('Std Deviation',   fn(sa['std']), fn(sb['std']), fn(sbe['std']), fn(sbem['std'])),
    ('SQN',             fn(sa['sqn']), fn(sb['sqn']), fn(sbe['sqn']), fn(sbem['sqn'])),
    ('Kelly %',         f(sa['kelly']*100 if sa['kelly'] else None,pct=True),
                        f(sb['kelly']*100 if sb['kelly'] else None,pct=True),
                        f(sbe['kelly']*100 if sbe['kelly'] else None,pct=True),
                        f(sbem['kelly']*100 if sbem['kelly'] else None,pct=True)),
    ('Max Consec Wins', 'N/A', str(sb['mxw']), str(sbe['mxw']), str(sbem['mxw'])),
    ('Max Consec Loss', 'N/A', str(sb['mxl']), str(sbe['mxl']), str(sbem['mxl'])),
]

W=22; C=13
cols=['Actual(norm)','Scl(no BE)','Scl+BE','Scl+BE+MACD']
print('\n'+f"{'Metric':<{W}}"+''.join(f'  {c:>{C}}' for c in cols))
print('-'*(W+(C+2)*4))
for row in rows:
    label=row[0]; vals=row[1:]
    print(f'{label:<{W}}'+''.join(f'  {v:>{C}}' for v in vals))

print(f'\nNorm: 1R=$10 | Loss cap: 1.25R=${LOSS_CAP:.2f} | BE: entry*{BE_MULT} | MACD(12,26,9) on 2-min bars')

print('\nMonthly breakdown:')
bma=defaultdict(list); bmb=defaultdict(list); bmbe=defaultdict(list); bmbem=defaultdict(list)
for r in r_actual: bma[r['date'][:7]].append(r)
for r in r_base:   bmb[r['date'][:7]].append(r)
for r in r_be:     bmbe[r['date'][:7]].append(r)
for r in r_be_m:   bmbem[r['date'][:7]].append(r)
months=sorted(set(list(bma)+list(bmb)+list(bmbe)+list(bmbem)))
print(f"  {'Month':<10}  {'Actual':>9}  {'Scl':>9}  {'Scl+BE':>9}  {'Scl+BE+M':>10}")
for mo in months:
    if not bma[mo]: continue
    ap=sum(r['pnl'] for r in bma[mo]); bp=sum(r['pnl'] for r in bmb[mo])
    bep=sum(r['pnl'] for r in bmbe[mo]); bemp=sum(r['pnl'] for r in bmbem[mo])
    print(f'  {mo:<10}  {ap:>+9.2f}  {bp:>+9.2f}  {bep:>+9.2f}  {bemp:>+10.2f}')

print('\nDaily PnL:')
all_days=sorted(set(list(sa['byday'])+list(sb['byday'])+list(sbe['byday'])+list(sbem['byday'])))
print(f"  {'Date':<12}  {'Actual':>9}  {'Scl':>9}  {'Scl+BE':>9}  {'Scl+BE+M':>10}  {'d_be':>8}  {'d_macd':>8}")
for d in all_days:
    a=sa['byday'].get(d,0); b=sb['byday'].get(d,0)
    be=sbe['byday'].get(d,0); bem=sbem['byday'].get(d,0)
    print(f'  {d:<12}  {a:>+9.2f}  {b:>+9.2f}  {be:>+9.2f}  {bem:>+10.2f}  {be-a:>+8.2f}  {bem-a:>+8.2f}')
