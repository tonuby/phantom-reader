"""
PHANTOM READER v1.0
===================
Sinyal Telegram kanalından gelen PHANTOM_V3 mesajlarını okur.
Her Pazartesi 08:00'da (Baku saati) geçen haftanın özet raporunu
yeni bir Telegram kanalına gönderir.

Kurulum:
    pip install requests schedule

Çalıştırma:
    python phantom_reader.py

Railway.app için: bu dosyayı olduğu gibi yükle.
"""

import requests
import schedule
import time
import json
import logging
from datetime import datetime, timedelta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger()

# =========================================================================
# AYARLAR — buradan değiştir
# =========================================================================

# Sinyallerin geldiği bot (mevcut botun)
BOT_TOKEN = "8730809758:AAH5pxgy3PWA4cd0_m6N1Jb5-QOTQcPJJ6Q"

# Sinyallerin geldiği chat (mevcut chat)
SINYAL_CHAT_ID = "811792517"

# Raporun gideceği yeni chat
# Yeni grup aç → @userinfobot'a yaz → buraya yaz
RAPOR_CHAT_ID = "811792517"

# Baku UTC+4 — rapor Pazartesi 08:00 Baku = Pazartesi 04:00 UTC
RAPOR_SAATI_UTC = "04:00"

# =========================================================================
# TELEGRAM API
# =========================================================================
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

def send_message(chat_id, text):
    """Telegram'a mesaj gönder"""
    try:
        r = requests.post(f"{API_BASE}/sendMessage", json={
            "chat_id": chat_id,
            "text": text
        }, timeout=10)
        if r.status_code == 200:
            log.info(f"Mesaj gönderildi: {chat_id}")
            return True
        else:
            log.error(f"Mesaj hatası: {r.text}")
            return False
    except Exception as e:
        log.error(f"Bağlantı hatası: {e}")
        return False

def get_updates(offset=None):
    """Bot'a gelen mesajları al"""
    params = {"timeout": 30, "limit": 100}
    if offset:
        params["offset"] = offset
    try:
        r = requests.get(f"{API_BASE}/getUpdates", params=params, timeout=35)
        if r.status_code == 200:
            return r.json().get("result", [])
        return []
    except Exception as e:
        log.error(f"getUpdates hatası: {e}")
        return []

# =========================================================================
# MESAJ PARSER — PHANTOM_V3 mesajlarını çözümle
# =========================================================================

def parse_message(text):
    """
    Telegram mesajını okur, ne tür olduğunu tespit eder.
    Dönüş: {"tip": "giris"|"tp1"|"win"|"be"|"stop", "parite": ..., "rr": ...}
    """
    if not text:
        return None

    text_upper = text.upper()

    # Giriş sinyali
    if "EMELLIYYATA GIR" in text_upper or "ISLEME GIR" in text_upper:
        parite = _extract_after(text, "Cut:", "Cüt:", "Parite:")
        vaxt   = _extract_after(text, "Vaxt:", "Zaman:")
        rr     = _extract_rr(text)
        return {"tip": "giris", "parite": parite, "vaxt": vaxt, "rr": rr}

    # TP1
    if "TP1 ALINDI" in text_upper:
        parite = _extract_parite_from_line2(text)
        return {"tip": "tp1", "parite": parite}

    # Full Win
    if "FULL WIN" in text_upper:
        parite = _extract_parite_from_line2(text)
        rr     = _extract_rr(text)
        return {"tip": "win", "parite": parite, "rr": rr}

    # Risksiz Bağlı (BE)
    if "RISKSIZ" in text_upper:
        parite = _extract_parite_from_line2(text)
        return {"tip": "be", "parite": parite, "rr": 1.0}

    # Stop
    if "STOP" in text_upper and ("STOP VURULDU" in text_upper or text_upper.strip().startswith("STOP") or "PATLAMA" in text_upper):
        parite = _extract_parite_from_line2(text)
        return {"tip": "stop", "parite": parite, "rr": -1.0}

    return None

def _extract_after(text, *keys):
    """Anahtar kelimeden sonraki degeri cikar"""
    lines = text.split("\n")
    for line in lines:
        # Emoji ve bosluk temizle
        clean_line = line.strip()
        for key in keys:
            if key in clean_line:
                val = clean_line.split(key, 1)[-1].strip()
                # Emoji ve ozel karakterleri temizle
                val = val.split()[0] if val else ""
                return val
    return "?"

def _extract_parite_from_line2(text):
    """Parite adini bul"""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for line in lines:
        # Emoji kaldir, USDT iceren satiri bul
        words = line.split()
        for word in words:
            # Emoji ve ozel karakterleri temizle
            clean = ''.join(c for c in word if c.isalnum() or c in '._-')
            if "USDT" in clean.upper() and len(clean) > 4:
                return clean
    return "?"

def _extract_rr(text):
    """RR değerini çıkar"""
    import re
    match = re.search(r'[+\-]?(\d+\.?\d*)R', text)
    if match:
        try:
            # + veya - işaretiyle birlikte al
            full = re.search(r'([+\-]?\d+\.?\d*)R', text)
            return float(full.group(1)) if full else 0.0
        except:
            return 0.0
    return 0.0

# =========================================================================
# HAFTALIK VERİ TOPLAYICI
# =========================================================================

class HaftalikIstatistik:
    def __init__(self):
        self.reset()

    def reset(self):
        self.toplam_giris  = 0
        self.full_win      = 0
        self.risksiz_be    = 0
        self.stop          = 0
        self.net_r         = 0.0
        self.parite_stats  = {}  # {parite: {win, be, stop, r}}
        self.baslangic     = datetime.utcnow()

    def giris_ekle(self, parite, vaxt):
        self.toplam_giris += 1
        if parite not in self.parite_stats:
            self.parite_stats[parite] = {"win": 0, "be": 0, "stop": 0, "r": 0.0, "vaxt": vaxt}

    def win_ekle(self, parite, rr):
        self.full_win += 1
        self.net_r    += rr
        if parite in self.parite_stats:
            self.parite_stats[parite]["win"] += 1
            self.parite_stats[parite]["r"]   += rr

    def be_ekle(self, parite):
        self.risksiz_be += 1
        self.net_r      += 1.0
        if parite in self.parite_stats:
            self.parite_stats[parite]["be"] += 1
            self.parite_stats[parite]["r"]  += 1.0

    def stop_ekle(self, parite):
        self.stop  += 1
        self.net_r -= 1.0
        if parite in self.parite_stats:
            self.parite_stats[parite]["stop"] += 1
            self.parite_stats[parite]["r"]    -= 1.0

    def rapor_olustur(self):
        """Haftalık rapor metni oluştur"""
        bugun = datetime.utcnow()
        hf_bas = self.baslangic.strftime("%d/%m/%Y")
        hf_son = bugun.strftime("%d/%m/%Y")

        kapanan = self.full_win + self.risksiz_be + self.stop
        ugur = (self.full_win + self.risksiz_be) * 100.0 / kapanan if kapanan > 0 else 0.0
        veziyyet = "QAZANCLI" if self.net_r >= 0 else "ZIYANLI"
        r_str = ("+" if self.net_r >= 0 else "") + f"{self.net_r:.2f}"

        lines = []
        lines.append("GUNLUK HESABAT")
        lines.append(f"{hf_bas} - {hf_son}")
        lines.append("")
        lines.append(f"Veziyyet: {veziyyet}")
        lines.append("")
        lines.append(f"Umumı Giriş:    {self.toplam_giris}")
        lines.append(f"Tamamlanan:     {kapanan}")
        lines.append(f"Full Win:       {self.full_win}")
        lines.append(f"Risksiz BE:     {self.risksiz_be}")
        lines.append(f"Stop:           {self.stop}")
        lines.append("")
        lines.append(f"Uğur:    %{ugur:.1f}")
        lines.append(f"Net R:   {r_str}R")
        lines.append("")

        # En iyi 5 parite
        if self.parite_stats:
            sirali = sorted(
                self.parite_stats.items(),
                key=lambda x: x[1]["r"],
                reverse=True
            )[:5]
            lines.append("En Yaxşı 5 Cüt:")
            for par, st in sirali:
                tot = st["win"] + st["be"] + st["stop"]
                ug  = (st["win"] + st["be"]) * 100 / tot if tot > 0 else 0
                r_s = ("+" if st["r"] >= 0 else "") + f"{st['r']:.2f}"
                vaxt = st.get("vaxt", "")
                lines.append(f"  {par} {vaxt} | Ugur:%{ug:.0f} | R:{r_s}")

        return "\n".join(lines)

istat = HaftalikIstatistik()

# =========================================================================
# MESAJ OKUYUCU — periyodik çalışır
# =========================================================================
last_update_id = None

def mesajlari_oku():
    """Son mesajları oku, istatistiklere ekle"""
    global last_update_id

    updates = get_updates(offset=last_update_id)
    if not updates:
        return

    for upd in updates:
        last_update_id = upd["update_id"] + 1

        # Sadece sinyal chat'inden gelen mesajları işle
        msg = upd.get("message") or upd.get("channel_post")
        if not msg:
            continue

        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != str(SINYAL_CHAT_ID):
            continue

        text = msg.get("text", "")
        parsed = parse_message(text)
        if not parsed:
            continue

        tip    = parsed.get("tip")
        parite = parsed.get("parite", "?")
        rr     = parsed.get("rr", 0.0)
        vaxt   = parsed.get("vaxt", "")

        if tip == "giris":
            istat.giris_ekle(parite, vaxt)
            log.info(f"Giriş: {parite} {vaxt}")
        elif tip == "win":
            istat.win_ekle(parite, rr)
            log.info(f"Win: {parite} +{rr}R")
        elif tip == "be":
            istat.be_ekle(parite)
            log.info(f"BE: {parite} +1R")
        elif tip == "stop":
            istat.stop_ekle(parite)
            log.info(f"Stop: {parite} -1R")

# =========================================================================
# HAFTALIK RAPOR GÖNDER
# =========================================================================

def gunluk_rapor_gonder():
    """Pazartesi 08:00 Baku (04:00 UTC) — rapor gönder ve sıfırla"""
    log.info("Haftalık rapor hazırlanıyor...")

    if istat.toplam_giris == 0:
        log.info("Bu hafta hiç işlem yok, rapor gönderilmedi.")
        istat.reset()
        return

    rapor = istat.rapor_olustur()
    basarili = send_message(RAPOR_CHAT_ID, rapor)

    if basarili:
        log.info("Haftalık rapor gönderildi.")
    else:
        log.error("Rapor gönderilemedi!")

    # Sıfırla — yeni haftaya başla
    istat.reset()

# =========================================================================
# ZAMANLAYICI
# =========================================================================

def main():
    log.info("PHANTOM READER baslatildi.")
    log.info(f"Sinyal chat: {SINYAL_CHAT_ID}")
    log.info(f"Rapor chat:  {RAPOR_CHAT_ID}")
    log.info("Gunluk rapor: Her gun 00:00 UTC (Baku 04:00)")
    log.info("")

    if RAPOR_CHAT_ID == "BURAYA_YEN_CHAT_ID_YAZ":
        log.warning("UYARI: RAPOR_CHAT_ID ayarlanmamis! Yeni grubu aç ve chat ID'yi yaz.")

    # Her 30 saniyede mesajları oku
    schedule.every(30).seconds.do(mesajlari_oku)

    # Her gun 00:00 UTC (Baku 04:00) gunluk rapor gonder
    schedule.every().day.at("00:00").do(gunluk_rapor_gonder)

    # İlk çalışmada mevcut mesajları oku
    mesajlari_oku()

    log.info("Hazir. Mesajlar dinleniyor...")
    # Baslangic mesaji gonder
    send_message(RAPOR_CHAT_ID, "PHANTOM READER aktiv oldu. Mesajlar dinleniyor...")

    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    main()
