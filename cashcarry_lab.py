"""
cashcarry_lab.py — Railway-ready: cash-and-carry DELTA-NEUTRAL (long spot + short perp).

Income ≈ funding yang DIPANEN oleh leg short perp. Gerak harga spot vs perp saling tutup
(risiko arah ~0), sisanya cuma basis (perp−spot) yang kecil & mean-revert.
-> jauh lebih predictable daripada carry perp-only kemarin.

Deploy sama seperti funding_lab.py: Procfile `web: python cashcarry_lab.py`, requirements pybit/flask.

MODEL (harian, realistis, tanpa look-ahead):
  posisi statis: long spot $1, short perp $1 (delta-neutral).
  return harian/notional = funding_hari (diterima short bila +) + (spot_ret − perp_ret) [basis].
  - ALWAYS-ON : tahan terus (1x entry + 1x exit, biaya teramortisasi -> kecil).
  - GATED     : hanya tahan saat funding kemarin > 0; flat saat negatif (hindari bayar funding).
  biaya: tiap on/off bayar buka+tutup 2 leg (spot+perp). Sweep maker/taker.

CATATAN JUJUR:
  - return DI ATAS NOTIONAL. Modal nyata = notional x (1 + 1/leverage_perp). 1x penuh -> bagi 2.
  - funding bisa NEGATIF lama (bear) -> GATED menanganinya, tapi return jadi benjol.
  - risiko: fee spot lebih mahal, basis blow-out saat squeeze (kalau leverage tinggi bisa kena),
    risiko exchange/custody. Ini riset, bukan jaminan.
"""
import os, time, datetime as dt
import pandas as pd, numpy as np
from pybit.unified_trading import HTTP

COINS = ['BTCUSDT','ETHUSDT','SOLUSDT','XRPUSDT','BNBUSDT','DOGEUSDT','ADAUSDT','AVAXUSDT',
         'LINKUSDT','LTCUSDT','SUIUSDT','APTUSDT','ARBUSDT','OPUSDT','INJUSDT','NEARUSDT','UNIUSDT','AAVEUSDT']
START = '2024-06-01'
CACHE_DIR = os.path.join(os.environ.get('RAILWAY_VOLUME_MOUNT_PATH', '.'), 'cccache')
FORCE = os.environ.get('FORCE_REFETCH', '0') == '1'
os.makedirs(CACHE_DIR, exist_ok=True)
session = HTTP(testnet=False)

def _ms(d): return int(pd.Timestamp(d).timestamp() * 1000)
def _cache(n): return os.path.join(CACHE_DIR, n + '.csv.gz')
def _save(df, n): df.to_csv(_cache(n), index=False, compression='gzip')
def _load(n):
    p = _cache(n)
    return pd.read_csv(p, compression='gzip') if (os.path.exists(p) and not FORCE) else None

def fetch_funding(sym):
    c = _load(f'fund_{sym}')
    if c is not None: return c
    rows, end, start_ms = [], _ms(dt.datetime.utcnow()), _ms(START)
    for _ in range(400):
        try:
            r = session.get_funding_rate_history(category='linear', symbol=sym, endTime=end, limit=200)
            lst = r['result']['list']
        except Exception as e:
            print(f'  fund {sym} err: {e}'); break
        if not lst: break
        for x in lst: rows.append((int(x['fundingRateTimestamp']), float(x['fundingRate'])))
        oldest = min(int(x['fundingRateTimestamp']) for x in lst)
        if oldest <= start_ms or len(lst) < 200: break
        end = oldest - 1; time.sleep(0.2)
    df = pd.DataFrame(rows, columns=['ts_ms','funding']).drop_duplicates('ts_ms').sort_values('ts_ms')
    _save(df, f'fund_{sym}'); print(f'  fund {sym}: {len(df)}'); return df

def fetch_kline(sym, category):
    tag = f'{category}_{sym}'
    c = _load(tag)
    if c is not None: return c
    rows, end, start_ms = [], _ms(dt.datetime.utcnow()), _ms(START)
    for _ in range(20):
        try:
            r = session.get_kline(category=category, symbol=sym, interval='D', end=end, limit=1000)
            lst = r['result']['list']
        except Exception as e:
            print(f'  kline {category} {sym} err: {e}'); break
        if not lst: break
        for x in lst: rows.append((int(x[0]), float(x[1]), float(x[4])))   # ts, open, close
        oldest = min(int(x[0]) for x in lst)
        if oldest <= start_ms or len(lst) < 1000: break
        end = oldest - 1; time.sleep(0.2)
    df = pd.DataFrame(rows, columns=['ts_ms','open','close']).drop_duplicates('ts_ms').sort_values('ts_ms')
    _save(df, tag); print(f'  {category} {sym}: {len(df)}'); return df

def to_daily(ts): return pd.to_datetime(ts, unit='ms').dt.floor('D')

def build_panel():
    spotC={}; perpC={}; F={}
    for s in COINS:
        perp = fetch_kline(s, 'linear')
        spot = fetch_kline(s, 'spot')            # butuh pasangan spot; kalau kosong -> skip
        if perp is None or spot is None or len(perp) < 60 or len(spot) < 60:
            print(f'  skip {s} (spot/perp tdk lengkap)'); continue
        fd = fetch_funding(s)
        if fd is None or not len(fd): continue
        perp['d']=to_daily(perp['ts_ms']); spot['d']=to_daily(spot['ts_ms']); fd['d']=to_daily(fd['ts_ms'])
        perpC[s]=perp.groupby('d')['close'].last(); spotC[s]=spot.groupby('d')['close'].last()
        F[s]=fd.groupby('d')['funding'].sum()
    perpC=pd.DataFrame(perpC); spotC=pd.DataFrame(spotC); F=pd.DataFrame(F)
    idx=perpC.dropna(how='all').index
    return spotC.reindex(idx), perpC.reindex(idx), F.reindex(idx)

# ---------- BACKTEST cash-and-carry ----------
def cc_returns(spotC, perpC, F, gated=False, gate=0.0, fee=0.0002, slip=0.0005):
    """Return harian per-coin (di atas notional) untuk posisi long spot + short perp.
       gated=True: hanya aktif saat funding kemarin > gate. Biaya saat ganti status."""
    common = spotC.index.intersection(perpC.index).intersection(F.index)
    sC=spotC.reindex(common); pC=perpC.reindex(common); Ff=F.reindex(common)
    sret = sC.pct_change(); pret = pC.pct_change()
    daily = Ff.fillna(0) + (sret - pret)         # funding diterima short + basis
    cost = 2*(fee+slip)                          # buka/tutup 2 leg (spot+perp) sekali ganti status
    out = {}
    for c_ in daily.columns:
        r = daily[c_].copy()
        valid = r.notna()
        if gated:
            sig = (Ff[c_].shift(1) > gate).astype(float)   # status hari ini dari funding kemarin
            r = r * sig
            toggles = sig.diff().abs().fillna(sig)          # 1 saat status berubah
            r = r - toggles * cost
        else:
            r = r.copy()
            r.iloc[0] = r.iloc[0] - cost                    # 1x entry
            r.iloc[-1] = r.iloc[-1] - cost                  # 1x exit
        out[c_] = r.where(valid)
    R = pd.DataFrame(out)
    port = R.mean(axis=1, skipna=True)                      # equal-weight lintas coin
    return port.dropna(), R

def stats(r, per=365):
    r = np.asarray(r)
    if len(r) < 8: return None
    eq = np.cumsum(r); dd = (eq - np.maximum.accumulate(eq)).min()
    sh = r.mean()/r.std()*np.sqrt(per) if r.std() > 0 else 0
    return dict(N=len(r), mean=round(r.mean()*100,4), ann=round(r.mean()*per*100,1),
                sharpe=round(sh,2), wr=round((r>0).mean()*100,1), maxDD=round(dd*100,1),
                total=round(r.sum()*100,1))

# ---------- PANEL LEVERAGE / risiko basis ----------
def leverage_panel(spotC, perpC, ann_notional):
    """Hitung dari data: distribusi basis (perp-spot), return-on-capital & jarak likuidasi per leverage.
       Struktur modal: spot didanai penuh (L_spot=1), perp di-leverage L.
       return-on-capital = ann_notional * L/(L+1)  (asimtot ke ann_notional, tak bisa lewat)."""
    basis = (perpC - spotC) / spotC * 100.0          # % premium perp atas spot per coin/hari
    b = basis.values.flatten(); b = b[~np.isnan(b)]
    db = basis.diff().values.flatten(); db = np.abs(db[~np.isnan(db)])  # perubahan basis harian (%)
    bstat = dict(mean=round(b.mean(),3), std=round(b.std(),3),
                 p1=round(np.percentile(b,1),3), p99=round(np.percentile(b,99),3),
                 min=round(b.min(),3), max=round(b.max(),3))
    dstat = dict(std=round(db.std(),3), p99=round(np.percentile(db,99),3), max=round(db.max(),3))
    rows = []
    for L in [1,2,3,5,10]:
        roc = ann_notional * L/(L+1)
        liq_iso = (1.0/L)*100 - 0.5                   # % PUMP yg melikuidasi perp short (margin isolated)
        rows.append((L, round(roc,2), round(liq_iso,1)))
    return bstat, dstat, rows

# ---------- WEB ----------
import threading, traceback
from flask import Flask
STATUS = {'state':'starting','msg':'worker belum mulai','report':None}

def run_research():
    try:
        STATUS.update(state='running', msg='fetch spot+perp+funding (lama di awal, lalu cache)...')
        spotC, perpC, F = build_panel()
        html=['<h2>Cash-and-Carry delta-neutral — riset realistis</h2>',
              f'<p>{perpC.shape[1]} coin (ada pasangan spot), {perpC.shape[0]} hari, sejak {START}</p>']
        # berapa sering funding positif (inti edge)
        posfrac = (F > 0).mean().mean()*100
        html.append(f'<p>Funding positif rata-rata: <b>{posfrac:.1f}%</b> dari waktu '
                    '(makin tinggi = makin sering short perp dibayar).</p>')

        html.append('<h3>A. Sweep konfigurasi (return DI ATAS NOTIONAL)</h3><pre>')
        html.append(f"{'varian':<28}{'N':>5}{'ann%':>8}{'Sharpe':>8}{'WR%':>7}{'maxDD%':>9}{'total%':>9}\n")
        configs = [
            ('always-on maker',  dict(gated=False, fee=0.0002,  slip=0.0005)),
            ('always-on taker',  dict(gated=False, fee=0.00055, slip=0.0005)),
            ('gated f>0 maker',  dict(gated=True, gate=0.0,     fee=0.0002,  slip=0.0005)),
            ('gated f>0 taker',  dict(gated=True, gate=0.0,     fee=0.00055, slip=0.0005)),
            ('gated f>0.005% maker', dict(gated=True, gate=0.00005, fee=0.0002, slip=0.0005)),
        ]
        saved = {}
        for name, kw in configs:
            port, _ = cc_returns(spotC, perpC, F, **kw); s = stats(port)
            saved[name] = (port, s)
            if s:
                html.append(f"{name:<28}{s['N']:>5}{s['ann']:>8.1f}{s['sharpe']:>8.2f}"
                            f"{s['wr']:>7.1f}{s['maxDD']:>+9.1f}{s['total']:>+9.1f}\n")
        html.append('</pre>')
        html.append('<p><i>Return di atas notional. Modal nyata 1x penuh (perp tak leverage) ≈ bagi 2. '
                    'Pakai leverage perp moderat menaikkan return-on-capital tapi menambah risiko basis.</i></p>')

        # B. walk-forward per kuartal pada always-on maker (uji konsistensi)
        port, _ = saved['always-on maker']
        html.append('<h3>B. Walk-forward per-kuartal — always-on maker</h3><pre>')
        dfq = pd.DataFrame({'r': port.values}, index=pd.to_datetime(port.index))
        pos=0; tot=0
        for q,g in dfq.groupby(pd.Grouper(freq='QE')):
            if len(g)>=10:
                sq=stats(g['r'].values); tot+=1
                if sq['ann']>0: pos+=1
                html.append(f"  {q.date()}  N={sq['N']:>3} ann={sq['ann']:+.1f}% Sharpe={sq['sharpe']:+.2f} maxDD={sq['maxDD']:+.1f}%\n")
        html.append(f"  --> kuartal positif: {pos}/{tot}\n</pre>")
        html.append('<p><b>Cara baca:</b> cash-carry seharusnya JAUH lebih konsisten per-kuartal daripada '
                    'carry perp-only. Kalau kuartal positif tinggi (≥8/9) dengan Sharpe stabil & maxDD kecil, '
                    'baru ini layak paper-trade. Funding positif% rendah = edge musiman, hati-hati.</p>')

        # C. panel leverage + risiko basis
        ann_notional = saved['always-on maker'][1]['ann']
        bstat, dstat, rows = leverage_panel(spotC, perpC, ann_notional)
        html.append('<h3>C. Leverage: return-on-capital vs risiko likuidasi</h3>')
        html.append(f'<pre>Basis (premium perp atas spot, %): mean={bstat["mean"]} std={bstat["std"]} '
                    f'p1={bstat["p1"]} p99={bstat["p99"]} min={bstat["min"]} max={bstat["max"]}\n'
                    f'Perubahan basis harian |Δ| (%): std={dstat["std"]} p99={dstat["p99"]} max={dstat["max"]}\n\n')
        html.append(f"{'leverage perp':<15}{'return-on-capital%':>20}{'PUMP yg likuidasi (isolated)':>32}\n")
        for L, roc, liq in rows:
            tag = '  <- bahaya pump' if liq < 25 else ''
            html.append(f"{str(L)+'x':<15}{roc:>20.2f}{str(round(liq,1))+'%':>32}{tag}\n")
        html.append('</pre>')
        html.append('<p><b>Cara baca C — INI penentu sizing:</b><br>'
                    '1. Return-on-capital <b>tak pernah lewat ann notional</b> (asimtot). Spot harus didanai penuh, '
                    'jadi leverage perp cuma menggeser dari ~separuh menuju ~penuh ann. Mau lebih tinggi → harus '
                    'margin spot juga = biaya pinjam + risiko likuidasi dua sisi (biasanya tak sepadan).<br>'
                    '2. Kolom "PUMP yg likuidasi" = berlaku untuk <b>margin ISOLATED</b> (spot & perp terpisah). '
                    'Di 5x/10x, pump 10–20% (sering terjadi di alt) bisa melikuidasi perp short padahal spot-mu untung '
                    '— gain spot terdampar, hedge hilang. <b>Jangan isolated di leverage tinggi.</b><br>'
                    '3. Pakai <b>UNIFIED/cross margin</b> (spot+perp satu akun) → gerak arah saling tutup, '
                    'likuidasi hanya kalau BASIS melebar tajam. Lihat |Δ| basis p99/max di atas: itu ukuran risiko nyatamu. '
                    'Kalau max basis-move ≪ 1/leverage, relatif aman.</p>')

        report='\n'.join(html)
        with open('report.html','w') as f: f.write(report)
        STATUS.update(state='done', msg='selesai', report=report)
        print('-> done')
    except Exception as e:
        STATUS.update(state='error', msg=str(e), report='<pre>'+traceback.format_exc()+'</pre>')
        print('ERROR:', e)

app = Flask(__name__)
PAGE = """<!doctype html><meta charset=utf-8><title>Cash-Carry Lab</title>
<style>body{{font-family:system-ui;max-width:860px;margin:24px auto;padding:0 14px;background:#0b0f17;color:#d7e0ea}}
pre{{background:#121826;padding:12px;border-radius:8px;overflow:auto;font-size:13px;white-space:pre-wrap}}
h2,h3{{color:#7fd1ff}} .bar{{padding:10px 12px;border-radius:8px;background:#162133;margin-bottom:14px}}</style>
<div class=bar>Status: <b>{state}</b> — {msg}</div>{body}"""

@app.route('/')
def index():
    st=STATUS
    body = st['report'] if st['state']=='done' else (('<p>Error:</p>'+(st['report'] or '')) if st['state']=='error'
            else '<meta http-equiv="refresh" content="5"><p>Sedang berjalan… auto-refresh 5 dtk.</p>')
    return PAGE.format(state=st['state'], msg=st['msg'], body=body)

@app.route('/health')
def health(): return 'ok'

@app.route('/rerun')
def rerun():
    if STATUS['state'] != 'running':
        threading.Thread(target=run_research, daemon=True).start()
    return ('', 302, {'Location': '/'})

threading.Thread(target=run_research, daemon=True).start()
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
