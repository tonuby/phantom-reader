"""
PHANTOM SCANNER v2.0
====================
Binance USDT.P paritelerini SFP stratejisiyle tarar.
- Baslarken test mesaji gonderir
- 5 parite test taramasi yapar, sonuc dogru ise tam tarama baslar
- Her zaman dilimi icin dogru gun sayisi kullanir
- Her Pazartesi 08:00 Baku (04:00 UTC) haftalik guncelleme

Kurulum:
    pip install requests pandas schedule
"""

import requests
import pandas as pd
import schedule
import time
import logging
from datetime import datetime, timedelta, timezone

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger()

# =========================================================================
# AYARLAR
# =========================================================================
TG_BOT_URL  = "https://api.telegram.org/bot8730809758:AAH5pxgy3PWA4cd0_m6N1Jb5-QOTQcPJJ6Q/sendMessage"
TG_CHAT_ID  = "811792517"

# Her zaman dilimi icin dogru gun sayisi (TV bar limiti baz alinarak)
TF_DAYS = {
    "5m":  35,   # ~35 gun
    "15m": 90,   # ~90 gun
    "1h":  90,   # ~90 gun
}

TIMEFRAMES   = list(TF_DAYS.keys())
TOP_N        = 15

# Filtreler
MIN_TRADES   = 10
MIN_NET_R    = 10.0
MIN_KAR_FAKT = 1.5

# SFP parametreleri — PHANTOM_V3 ile ayni
WICK_MULT   = 1.5
P_LEFT      = 15
P_RIGHT     = 5
ATR_LEN     = 14
ATR_MULT    = 1.0
MIN_RR      = 1.95

# Test icin kullanilacak pariteler
TEST_PAIRS  = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "BNBUSDT"]

# =========================================================================
# TELEGRAM
# =========================================================================
def send_tg(text):
    try:
        r = requests.post(TG_BOT_URL, json={
            "chat_id": TG_CHAT_ID,
            "text":    text
        }, timeout=10)
        if r.status_code == 200:
            log.info("Telegram mesaji gonderildi")
            return True
        else:
            log.error(f"Telegram hatasi: {r.text}")
            return False
    except Exception as e:
        log.error(f"Telegram baglanti hatasi: {e}")
        return False

def tf_label(tf):
    return {"5m":"5M","15m":"15M","1h":"1H","4h":"4H"}.get(tf, tf.upper())

# =========================================================================
# BINANCE API
# =========================================================================
BINANCE_URL = "https://fapi.binance.com"

def get_all_usdt_pairs():
    """Tum aktif USDT perpetual paritelerini al"""
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
    """OHLCV verisi al"""
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
            time.sleep(0.1)

        if not all_klines:
            return None

        df = pd.DataFrame(all_klines, columns=[
            "time","open","high","low","close","volume",
            "close_time","quote_vol","trades","taker_base","taker_quote","ignore"
        ])
        df["time"]   = pd.to_datetime(df["time"], unit="ms")
        df["open"]   = df["open"].astype(float)
        df["high"]   = df["high"].astype(float)
        df["low"]    = df["low"].astype(float)
        df["close"]  = df["close"].astype(float)
        df["volume"] = df["volume"].astype(float)
        df = df.drop_duplicates(subset=["time"]).reset_index(drop=True)
        return df

    except Exception as e:
        log.error(f"{symbol} {interval} veri hatasi: {e}")
        return None

# =========================================================================
# INDIKATÖRLER
# =========================================================================
def calc_atr(df, period=14):
    high  = df["high"]
    low   = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs()
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/period, adjust=False).mean()
    return atr

def find_pivot_high(series, left, right):
    """Pivot high bul - hizli versiyon"""
    vals = series.values
    n = len(vals)
    result = [float('nan')] * n
    for i in range(left, n - right):
        v = vals[i]
        is_ph = True
        for j in range(i - left, i + right + 1):
            if j != i and vals[j] >= v:
                is_ph = False
                break
        if is_ph:
            result[i] = v
    return pd.Series(result, index=series.index)

def find_pivot_low(series, left, right):
    """Pivot low bul - hizli versiyon"""
    vals = series.values
    n = len(vals)
    result = [float('nan')] * n
    for i in range(left, n - right):
        v = vals[i]
        is_pl = True
        for j in range(i - left, i + right + 1):
            if j != i and vals[j] <= v:
                is_pl = False
                break
        if is_pl:
            result[i] = v
    return pd.Series(result, index=series.index)

# =========================================================================
# SFP BACKTEST
# =========================================================================
def backtest_sfp(df):
    """
    SFP stratejisi backtest.
    Her islem 3 kategoriden birine girer:
    - full_win: TP2 hedefine ulasti
    - risksiz_be: TP1 sonrasi breakeven kapandi
    - stop: TP1 gelmeden stop vuruldu
    TP1 ayrıca sayılmaz.
    """
    if df is None or len(df) < P_LEFT + P_RIGHT + ATR_LEN + 10:
        return None

    atr  = calc_atr(df, ATR_LEN)
    p_hi = find_pivot_high(df["high"], P_LEFT, P_RIGHT)
    p_lo = find_pivot_low(df["low"],  P_LEFT, P_RIGHT)

    r_full_win   = 0
    r_risksiz_be = 0
    r_stop       = 0
    r_net        = 0.0
    r_win_r      = 0.0
    sinyal_sayisi = 0  # debug

    last_ph  = None
    last_pl  = None
    swept_ph = None
    swept_pl = None

    in_trade  = False
    ep = sl = tp1 = tp2 = sl_dist = 0.0
    direction = 0
    tp1_hit   = False
    start_qty = 1.0

    start_i = P_LEFT + P_RIGHT + ATR_LEN

    for i in range(start_i, len(df)):
        row   = df.iloc[i]
        atr_v = atr.iloc[i]

        if pd.notna(p_hi.iloc[i]):
            last_ph = p_hi.iloc[i]
        if pd.notna(p_lo.iloc[i]):
            last_pl = p_lo.iloc[i]

        if pd.isna(atr_v) or atr_v <= 0:
            continue

        open_  = row["open"]
        high_  = row["high"]
        low_   = row["low"]
        close_ = row["close"]

        body    = max(abs(close_ - open_), 1e-10)
        wick_up = high_ - max(close_, open_)
        wick_dn = min(close_, open_) - low_

        # Aktif işlem yönetimi
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
                        r_net += 1.0
                    else:
                        r_stop += 1
                        r_net  -= 1.0
                    in_trade = False
                    tp1_hit  = False
                elif high_ >= tp2:
                    rr = abs(tp2 - ep) / sl_dist
                    r_full_win += 1
                    r_net      += rr
                    r_win_r    += rr
                    in_trade   = False
                    tp1_hit    = False
            else:
                if high_ >= sl:
                    if tp1_hit:
                        r_risksiz_be += 1
                        r_net += 1.0
                    else:
                        r_stop += 1
                        r_net  -= 1.0
                    in_trade = False
                    tp1_hit  = False
                elif low_ <= tp2:
                    rr = abs(ep - tp2) / sl_dist
                    r_full_win += 1
                    r_net      += rr
                    r_win_r    += rr
                    in_trade   = False
                    tp1_hit    = False
            continue

        # Yeni sinyal ara
        # LONG SFP
        if (last_pl is not None and
            low_ < last_pl and
            close_ > last_pl and
            wick_dn >= body * WICK_MULT and
            last_pl != swept_pl):

            sl_v    = low_ - atr_v * ATR_MULT
            sld     = max(abs(close_ - sl_v), 1e-10)
            tp1_v   = close_ + sld
            tp2_v   = close_ + sld * MIN_RR
            rr_chk  = abs(tp2_v - close_) / sld

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
                sinyal_sayisi += 1

        # SHORT SFP
        elif (last_ph is not None and
              high_ > last_ph and
              close_ < last_ph and
              wick_up >= body * WICK_MULT and
              last_ph != swept_ph):

            sl_v    = high_ + atr_v * ATR_MULT
            sld     = max(abs(sl_v - close_), 1e-10)
            tp1_v   = close_ - sld
            tp2_v   = close_ - sld * MIN_RR
            rr_chk  = abs(close_ - tp2_v) / sld

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
                sinyal_sayisi += 1

    total = r_full_win + r_risksiz_be + r_stop
    log.debug(f"    Sinyal: {sinyal_sayisi} | Kapanan: {total} (FW:{r_full_win} BE:{r_risksiz_be} ST:{r_stop})")
    if total < MIN_TRADES:
        return None

    kazanan_r  = r_win_r + r_risksiz_be * 1.0
    kaybeden_r = max(r_stop * 1.0, 0.001)
    kar_fakt   = kazanan_r / kaybeden_r
    basari     = (r_full_win + r_risksiz_be) * 100.0 / total

    return {
        "total":      total,
        "full_win":   r_full_win,
        "risksiz_be": r_risksiz_be,
        "stop":       r_stop,
        "net_r":      round(r_net, 2),
        "win_r":      round(r_win_r, 2),
        "kar_fakt":   round(kar_fakt, 2),
        "basari":     round(basari, 1),
    }

# =========================================================================
# TARAMA
# =========================================================================
def tarama_yap(pairs, baslik):
    """Verilen parite listesini tara, rapor gonder"""
    log.info(f"Tarama basladi: {baslik} — {len(pairs)} parite")

    results = []
    toplam  = len(pairs) * len(TIMEFRAMES)
    islenen = 0

    for symbol in pairs:
        for tf in TIMEFRAMES:
            islenen += 1
            days = TF_DAYS[tf]

            df = get_klines(symbol, tf, days)
            if df is None or len(df) < 100:
                time.sleep(0.05)
                continue

            stat = backtest_sfp(df)
            if stat is None:
                time.sleep(0.05)
                continue

            if (stat["net_r"]    >= MIN_NET_R and
                stat["kar_fakt"] >= MIN_KAR_FAKT and
                stat["total"]    >= MIN_TRADES):
                results.append({
                    "symbol": symbol,
                    "tf":     tf,
                    **stat
                })
                log.info(f"  GECTI: {symbol} {tf_label(tf)} | R:{stat['net_r']} | KF:{stat['kar_fakt']}")

            if islenen % 100 == 0:
                log.info(f"  {islenen}/{toplam} islendi, {len(results)} parite filtreyi gecti")

            time.sleep(0.05)

    results.sort(key=lambda x: x["net_r"], reverse=True)
    top = results[:TOP_N]

    log.info(f"Tarama bitti. {len(results)} parite filtreyi gecti.")

    if not top:
        send_tg(
            f"{baslik}\n\n"
            f"Filtreyi gecen parite bulunamadi.\n"
            f"Filtre: NetR>={MIN_NET_R}R | KF>={MIN_KAR_FAKT} | Islem>={MIN_TRADES}\n"
            f"Taranan: {len(pairs)} parite x {len(TIMEFRAMES)} TF"
        )
        return False

    tarih = datetime.now(timezone.utc).strftime("%d/%m/%Y")
    msg   = f"{baslik}\n"
    msg  += f"{tarih}\n"
    msg  += f"Filtre: NetR>={MIN_NET_R}R | KF>={MIN_KAR_FAKT} | Islem>={MIN_TRADES}\n"
    msg  += f"Gececen: {len(results)} | Gosterilen: {len(top)}\n"
    msg  += "=" * 30 + "\n\n"

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

    # 4096 karakter siniri
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

    return True

# =========================================================================
# ANA PROGRAM
# =========================================================================
def haftalik_tarama():
    pairs = get_all_usdt_pairs()
    if pairs:
        tarama_yap(pairs, "HAFTALIK TARAMA RAPORU")

def main():
    log.info("PHANTOM SCANNER v2.0 baslatildi.")
    log.info(f"Filtreler: NetR>={MIN_NET_R}R | KF>={MIN_KAR_FAKT} | Islem>={MIN_TRADES}")
    log.info(f"Zaman dilimleri: {TIMEFRAMES}")

    # 1. Baslangic test mesaji
    send_tg(
        "PHANTOM SCANNER v2.0 aktiv oldu.\n"
        f"Filtreler: NetR>={MIN_NET_R}R | KF>={MIN_KAR_FAKT} | Islem>={MIN_TRADES}\n"
        "Simdi 5 parite ile test taramasi yapiliyor..."
    )

    # 2. Test taramasi (5 parite)
    log.info("Test taramasi baslıyor: 5 parite...")
    test_ok = tarama_yap(TEST_PAIRS, "TEST TARAMASI (5 Parite)")

    if test_ok:
        send_tg("Test taramasi basarili! Tam tarama basliyor (1-3 saat surebilir)...")
    else:
        send_tg("Test taramasi bitti (filtre gecen yok). Tam tarama basliyor...")

    # 3. Tam tarama
    pairs = get_all_usdt_pairs()
    if pairs:
        tarama_yap(pairs, "ILK TAM TARAMA — Son 35-90 Gun")
    else:
        send_tg("HATA: Parite listesi alinamadi!")
        return

    # 4. Haftalik zamanlayici
    schedule.every().monday.at("04:00").do(haftalik_tarama)
    log.info("Zamanlayici aktif. Her Pazartesi 04:00 UTC tarama yapilacak.")

    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    main()
