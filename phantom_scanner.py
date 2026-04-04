"""
PHANTOM SCANNER v3.0
====================
Pine Script PHANTOM_V3 ile birebir ayni mantik:
- Likidite havuzu (pivot high/low sweep edilmemisler)
- TP2 = sweep edilmemis en yakin likidite seviyesi
- Pivot tespiti: P_LEFT=15, P_RIGHT=5 (bar bazli)
- SFP: fitil/govde orani kontrolu
- ATR bazli stop
- Min RR: 1.95

Her zaman dilimi icin dogru gun sayisi:
- 5M  -> 35 gun
- 15M -> 90 gun
- 1H  -> 90 gun
"""

import requests
import pandas as pd
import schedule
import time
import logging
from datetime import datetime, timedelta, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger()

# =========================================================================
# AYARLAR
# =========================================================================
TG_BOT_URL  = "https://api.telegram.org/bot8730809758:AAH5pxgy3PWA4cd0_m6N1Jb5-QOTQcPJJ6Q/sendMessage"
TG_CHAT_ID  = "811792517"

TF_DAYS = {
    "5m":  35,
    "15m": 90,
    "1h":  90,
}
TIMEFRAMES = list(TF_DAYS.keys())
TOP_N      = 15

# Filtreler
MIN_TRADES   = 10
MIN_NET_R    = 10.0
MIN_KAR_FAKT = 1.5

# PHANTOM_V3 parametreleri — Pine Script ile ayni
WICK_MULT    = 1.5
P_LEFT       = 15
P_RIGHT      = 5
ATR_LEN      = 14
ATR_MULT     = 1.0
MIN_RR       = 1.95
ATR_TP_MULT  = 3.0   # likidite yoksa ATR fallback
LIQ_LOOKBACK = 100
LIQ_LEFT     = 10
LIQ_RIGHT    = 3

# =========================================================================
# TELEGRAM
# =========================================================================
def send_tg(text):
    try:
        r = requests.post(TG_BOT_URL, json={
            "chat_id": TG_CHAT_ID,
            "text":    text
        }, timeout=10)
        return r.status_code == 200
    except Exception as e:
        log.error(f"Telegram hatasi: {e}")
        return False

def tf_label(tf):
    return {"5m":"5M","15m":"15M","1h":"1H","4h":"4H"}.get(tf, tf.upper())

# =========================================================================
# BINANCE API
# =========================================================================
BINANCE_URL = "https://fapi.binance.com"

def get_all_usdt_pairs():
    try:
        r = requests.get(f"{BINANCE_URL}/fapi/v1/exchangeInfo", timeout=15)
        data = r.json()
        pairs = []
        for s in data.get("symbols", []):
            if (s.get("quoteAsset") == "USDT" and
                s.get("status") == "TRADING" and
                s.get("contractType") == "PERPETUAL"):
                pairs.append(s["symbol"])
        log.info(f"Toplam {len(pairs)} USDT.P paritesi bulundu")
        return pairs
    except Exception as e:
        log.error(f"Parite listesi alinamadi: {e}")
        return []

def get_klines(symbol, interval, days):
    try:
        now      = datetime.now(timezone.utc)
        end_ms   = int(now.timestamp() * 1000)
        start_ms = int((now - timedelta(days=days)).timestamp() * 1000)
        all_klines = []
        limit = 1000
        while start_ms < end_ms:
            r = requests.get(f"{BINANCE_URL}/fapi/v1/klines", params={
                "symbol":    symbol,
                "interval":  interval,
                "startTime": start_ms,
                "endTime":   end_ms,
                "limit":     limit
            }, timeout=15)
            if r.status_code != 200:
                break
            klines = r.json()
            if not klines:
                break
            all_klines.extend(klines)
            last_time = klines[-1][0]
            if last_time >= end_ms or len(klines) < limit:
                break
            start_ms = last_time + 1
            time.sleep(0.05)
        if not all_klines:
            return None
        df = pd.DataFrame(all_klines, columns=[
            "time","open","high","low","close","volume",
            "close_time","quote_vol","trades","taker_base","taker_quote","ignore"
        ])
        for col in ["open","high","low","close","volume"]:
            df[col] = df[col].astype(float)
        df["time"] = pd.to_datetime(df["time"], unit="ms")
        df = df.drop_duplicates(subset=["time"]).reset_index(drop=True)
        return df
    except Exception as e:
        log.error(f"{symbol} {interval} veri hatasi: {e}")
        return None

# =========================================================================
# ATR — Pine Script EMA bazli (RMA)
# =========================================================================
def calc_atr(df, period=14):
    high  = df["high"].values
    low   = df["low"].values
    close = df["close"].values
    n     = len(df)
    atr   = [float('nan')] * n

    # TR hesapla
    tr = [0.0] * n
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i-1]),
            abs(low[i]  - close[i-1])
        )

    # RMA (Pine Script'in ta.atr kullandigi yontem)
    alpha = 1.0 / period
    # Ilk ATR: basit ortalama
    if n >= period:
        atr[period-1] = sum(tr[:period]) / period
        for i in range(period, n):
            atr[i] = alpha * tr[i] + (1 - alpha) * atr[i-1]

    return atr

# =========================================================================
# PIVOT TESPITI — Pine Script ile ayni
# P_LEFT bardan buyuk, P_RIGHT bar sonra onaylaniyor
# =========================================================================
def find_pivots(highs, lows, left, right):
    """
    Pine Script ta.pivothigh/pivotlow gibi calisir.
    i barindaki pivot, i+right barinda onaylanir.
    Yani bar i'de pivot varsa, bunu bar i+right aninda biliyoruz.
    """
    n    = len(highs)
    p_hi = [float('nan')] * n
    p_lo = [float('nan')] * n

    for i in range(left, n - right):
        # Pivot high: i bari etrafindaki left+right+1 bardan en yuksek
        h = highs[i]
        is_ph = True
        for j in range(i - left, i + right + 1):
            if j != i and highs[j] >= h:
                is_ph = False
                break
        if is_ph:
            p_hi[i] = h

        # Pivot low
        l = lows[i]
        is_pl = True
        for j in range(i - left, i + right + 1):
            if j != i and lows[j] <= l:
                is_pl = False
                break
        if is_pl:
            p_lo[i] = l

    return p_hi, p_lo

# =========================================================================
# LIKVDLIK PIVOT TESPITI
# =========================================================================
def find_liq_pivots(highs, lows, left, right):
    n    = len(highs)
    p_hi = [float('nan')] * n
    p_lo = [float('nan')] * n

    for i in range(left, n - right):
        h = highs[i]
        is_ph = True
        for j in range(i - left, i + right + 1):
            if j != i and highs[j] >= h:
                is_ph = False
                break
        if is_ph:
            p_hi[i] = h

        l = lows[i]
        is_pl = True
        for j in range(i - left, i + right + 1):
            if j != i and lows[j] <= l:
                is_pl = False
                break
        if is_pl:
            p_lo[i] = l

    return p_hi, p_lo

# =========================================================================
# BACKTEST — Pine Script PHANTOM_V3 ile birebir
# =========================================================================
def backtest_sfp(df, symbol="?", tf="?"):
    if df is None or len(df) < P_LEFT + P_RIGHT + ATR_LEN + 50:
        return None

    highs  = df["high"].values
    lows   = df["low"].values
    closes = df["close"].values
    opens  = df["open"].values
    n      = len(df)

    atr_vals = calc_atr(df, ATR_LEN)

    # SFP pivot tespiti
    p_hi, p_lo = find_pivots(highs, lows, P_LEFT, P_RIGHT)

    # Likidite pivot tespiti
    liq_hi, liq_lo = find_liq_pivots(highs, lows, LIQ_LEFT, LIQ_RIGHT)

    # Sonuc sayaclari
    r_full_win   = 0
    r_risksiz_be = 0
    r_stop       = 0
    r_net        = 0.0
    r_win_total  = 0.0
    sinyal_say   = 0

    # State
    last_ph  = None
    last_pl  = None
    swept_ph = None
    swept_pl = None

    # Likidite havuzlari (sweep edilmemis seviyeler)
    bull_liq = []  # yukari likidite (short hedefi)
    bear_liq = []  # asagi likidite (long hedefi)

    in_trade  = False
    ep = sl = tp1 = tp2 = sl_dist = 0.0
    direction = 0
    tp1_hit   = False

    start_i = max(P_LEFT + P_RIGHT + ATR_LEN, LIQ_LEFT + LIQ_RIGHT) + 1

    for i in range(start_i, n):
        high_  = highs[i]
        low_   = lows[i]
        close_ = closes[i]
        open_  = opens[i]
        atr_v  = atr_vals[i]

        if atr_v is None or atr_v != atr_v or atr_v <= 0:
            continue

        # Likidite pivot guncelle
        # P_RIGHT kadar geriden bak (o bar artik onaylandi)
        liq_confirm_i = i - LIQ_RIGHT
        if liq_confirm_i >= 0:
            if liq_hi[liq_confirm_i] == liq_hi[liq_confirm_i]:  # nan degil
                lvl = liq_hi[liq_confirm_i]
                if lvl == lvl:
                    bull_liq.append(lvl)
                    if len(bull_liq) > LIQ_LOOKBACK:
                        bull_liq.pop(0)
            if liq_lo[liq_confirm_i] == liq_lo[liq_confirm_i]:
                lvl = liq_lo[liq_confirm_i]
                if lvl == lvl:
                    bear_liq.append(lvl)
                    if len(bear_liq) > LIQ_LOOKBACK:
                        bear_liq.pop(0)

        # Sweep edilmis likidite seviyelerini temizle
        bull_liq = [lvl for lvl in bull_liq if high_ <= lvl]
        bear_liq = [lvl for lvl in bear_liq if low_  >= lvl]

        # SFP pivot guncelle (P_RIGHT kadar geriden)
        sfp_confirm_i = i - P_RIGHT
        if sfp_confirm_i >= 0:
            if p_hi[sfp_confirm_i] == p_hi[sfp_confirm_i]:
                last_ph = p_hi[sfp_confirm_i]
            if p_lo[sfp_confirm_i] == p_lo[sfp_confirm_i]:
                last_pl = p_lo[sfp_confirm_i]

        body    = max(abs(close_ - open_), 1e-10)
        wick_up = high_ - max(close_, open_)
        wick_dn = min(close_, open_) - low_

        # Aktif islem yonetimi
        if in_trade:
            if not tp1_hit:
                if direction == 1 and high_ >= tp1:
                    tp1_hit = True
                    sl = ep  # breakeven
                elif direction == -1 and low_ <= tp1:
                    tp1_hit = True
                    sl = ep

            if direction == 1:
                if low_ <= sl:
                    if tp1_hit:
                        r_risksiz_be += 1
                        r_net        += 1.0
                        r_win_total  += 1.0
                    else:
                        r_stop += 1
                        r_net  -= 1.0
                    in_trade = False
                    tp1_hit  = False
                elif high_ >= tp2:
                    rr = abs(tp2 - ep) / sl_dist
                    r_full_win  += 1
                    r_net       += rr
                    r_win_total += rr
                    in_trade    = False
                    tp1_hit     = False
            else:
                if high_ >= sl:
                    if tp1_hit:
                        r_risksiz_be += 1
                        r_net        += 1.0
                        r_win_total  += 1.0
                    else:
                        r_stop += 1
                        r_net  -= 1.0
                    in_trade = False
                    tp1_hit  = False
                elif low_ <= tp2:
                    rr = abs(ep - tp2) / sl_dist
                    r_full_win  += 1
                    r_net       += rr
                    r_win_total += rr
                    in_trade    = False
                    tp1_hit     = False
            continue

        # Yeni sinyal ara
        if last_pl is not None and last_pl != swept_pl:
            # LONG SFP: low pivot'u asagi kirar ama mum yukari kapanir
            if (low_ < last_pl and
                close_ > last_pl and
                wick_dn >= body * WICK_MULT):

                sl_v  = low_ - atr_v * ATR_MULT
                sld   = max(abs(close_ - sl_v), atr_v * 0.1)
                tp1_v = close_ + sld

                # TP2: bear_liq icinden en yakin ve MIN_RR'yi karsilayan seviye
                tp2_v = None
                for lvl in sorted(bear_liq):
                    if lvl > close_ and (lvl - close_) / sld >= MIN_RR:
                        tp2_v = lvl
                        break
                if tp2_v is None:
                    tp2_v = close_ + sld * ATR_TP_MULT

                rr_chk = abs(tp2_v - close_) / sld
                if rr_chk >= MIN_RR:
                    in_trade  = True
                    ep        = close_
                    sl        = sl_v
                    tp1       = tp1_v
                    tp2       = tp2_v
                    sl_dist   = sld
                    direction = 1
                    tp1_hit   = False
                    swept_pl  = last_pl
                    sinyal_say += 1

        if last_ph is not None and last_ph != swept_ph and not in_trade:
            # SHORT SFP
            if (high_ > last_ph and
                close_ < last_ph and
                wick_up >= body * WICK_MULT):

                sl_v  = high_ + atr_v * ATR_MULT
                sld   = max(abs(sl_v - close_), atr_v * 0.1)
                tp1_v = close_ - sld

                # TP2: bull_liq icinden en yakin ve MIN_RR'yi karsilayan seviye
                tp2_v = None
                for lvl in sorted(bull_liq, reverse=True):
                    if lvl < close_ and (close_ - lvl) / sld >= MIN_RR:
                        tp2_v = lvl
                        break
                if tp2_v is None:
                    tp2_v = close_ - sld * ATR_TP_MULT

                rr_chk = abs(close_ - tp2_v) / sld
                if rr_chk >= MIN_RR:
                    in_trade  = True
                    ep        = close_
                    sl        = sl_v
                    tp1       = tp1_v
                    tp2       = tp2_v
                    sl_dist   = sld
                    direction = -1
                    tp1_hit   = False
                    swept_ph  = last_ph
                    sinyal_say += 1

    total = r_full_win + r_risksiz_be + r_stop
    log.info(f"  {symbol} {tf_label(tf)} | Sinyal:{sinyal_say} | Kapanan:{total} "
             f"FW:{r_full_win} BE:{r_risksiz_be} ST:{r_stop} | NetR:{round(r_net,2)}")

    if total < MIN_TRADES:
        return None

    kaybeden   = max(r_stop * 1.0, 0.001)
    kar_fakt   = r_win_total / kaybeden
    basari     = (r_full_win + r_risksiz_be) * 100.0 / total

    return {
        "total":      total,
        "full_win":   r_full_win,
        "risksiz_be": r_risksiz_be,
        "stop":       r_stop,
        "net_r":      round(r_net, 2),
        "kar_fakt":   round(kar_fakt, 2),
        "basari":     round(basari, 1),
    }

# =========================================================================
# TARAMA
# =========================================================================
def tarama_yap(pairs, baslik):
    log.info(f"Tarama basladi: {baslik} — {len(pairs)} parite x {len(TIMEFRAMES)} TF")
    results = []

    for symbol in pairs:
        for tf in TIMEFRAMES:
            days = TF_DAYS[tf]
            df   = get_klines(symbol, tf, days)
            if df is None:
                continue

            stat = backtest_sfp(df, symbol, tf)
            if stat is None:
                continue

            if (stat["net_r"]    >= MIN_NET_R and
                stat["kar_fakt"] >= MIN_KAR_FAKT and
                stat["total"]    >= MIN_TRADES):
                results.append({"symbol": symbol, "tf": tf, **stat})

    results.sort(key=lambda x: x["net_r"], reverse=True)
    top = results[:TOP_N]

    log.info(f"Tarama bitti. {len(results)} parite filtreyi gecti.")

    tarih = datetime.now(timezone.utc).strftime("%d/%m/%Y")
    if not top:
        send_tg(
            f"{baslik}\n{tarih}\n\n"
            f"Filtreyi gecen parite yok.\n"
            f"Filtre: NetR>={MIN_NET_R}R | KF>={MIN_KAR_FAKT} | Islem>={MIN_TRADES}"
        )
        return

    msg  = f"{baslik}\n{tarih}\n"
    msg += f"Filtre: NetR>={MIN_NET_R}R | KF>={MIN_KAR_FAKT} | Islem>={MIN_TRADES}\n"
    msg += f"Gecen: {len(results)} | Gosterilen: {len(top)}\n"
    msg += "=" * 28 + "\n\n"

    for idx, r in enumerate(top, 1):
        r_net = f"+{r['net_r']}R" if r['net_r'] >= 0 else f"{r['net_r']}R"
        msg  += f"{idx}. {r['symbol']} — {tf_label(r['tf'])}\n"
        msg  += f"   Islem: {r['total']}\n"
        msg  += f"   Full Win: {r['full_win']}\n"
        msg  += f"   Risksiz BE: {r['risksiz_be']}\n"
        msg  += f"   Stop: {r['stop']}\n"
        msg  += f"   Net R: {r_net}\n"
        msg  += f"   Kar Faktoru: {r['kar_fakt']}\n"
        msg  += f"   Basari: %{r['basari']}\n\n"

    if len(msg) <= 4096:
        send_tg(msg)
    else:
        chunks = []
        lines  = msg.split("\n")
        chunk  = ""
        for line in lines:
            if len(chunk) + len(line) + 1 > 4000:
                chunks.append(chunk)
                chunk = line + "\n"
            else:
                chunk += line + "\n"
        if chunk:
            chunks.append(chunk)
        for c in chunks:
            send_tg(c)
            time.sleep(0.5)

# =========================================================================
# ANA PROGRAM
# =========================================================================
def main():
    log.info("PHANTOM SCANNER v3.0 baslatildi.")
    log.info(f"Filtreler: NetR>={MIN_NET_R}R | KF>={MIN_KAR_FAKT} | Islem>={MIN_TRADES}")

    send_tg(
        "PHANTOM SCANNER v3.0 aktiv oldu.\n"
        "Pine Script ile birebir ayni mantik.\n"
        "Simdi BTCUSDT test taramasi yapiliyor..."
    )

    # Test: sadece BTCUSDT
    tarama_yap(["BTCUSDT"], "BTCUSDT TEST (v3.0)")

    send_tg("Test bitti! Tam tarama basliyor (1-3 saat)...")

    # Tam tarama
    pairs = get_all_usdt_pairs()
    if pairs:
        tarama_yap(pairs, "TAM TARAMA RAPORU")
    else:
        send_tg("HATA: Parite listesi alinamadi!")

    # Haftalik zamanlayici
    schedule.every().monday.at("04:00").do(
        lambda: tarama_yap(get_all_usdt_pairs() or [], "HAFTALIK TARAMA")
    )
    log.info("Zamanlayici aktif.")

    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    main()
