CASH-AND-CARRY LAB — deploy Railway
====================================
Long spot + short perp (delta-neutral), panen funding. Risiko arah ~0.

FILE: cashcarry_lab.py | requirements.txt (otomatis dibaca) | Procfile (perintah run)

LANGKAH:
  1. Repo baru -> 3 file ini -> push GitHub -> Railway Deploy from GitHub.
  2. Tambah VOLUME, set env RAILWAY_VOLUME_MOUNT_PATH = path mount (cache + report persist).
  3. Buka URL -> riset jalan otomatis, auto-refresh sampai "done".
ENDPOINT: /  /rerun  /health     ENV opsional: FORCE_REFETCH=1

BACA HASIL:
  - "Funding positif %": makin tinggi, makin sering short perp dibayar.
  - Tabel A: always-on vs gated, maker vs taker. Return DI ATAS NOTIONAL
    (modal 1x penuh ~ bagi 2).
  - Tabel B: walk-forward per-kuartal. Cash-carry harusnya KONSISTEN (>=8/9 positif),
    Sharpe stabil, maxDD kecil. Kalau ya -> layak paper-trade (BUKAN langsung uang).

RISIKO JUJUR:
  - Funding bisa negatif lama (bear) -> gated menanganinya, return jadi benjol.
  - Fee spot lebih mahal dari perp; basis bisa melebar saat squeeze.
  - Pakai leverage perp = return-on-capital naik TAPI risiko likuidasi basis naik.
  - Tetap riset, bukan jaminan profit.
