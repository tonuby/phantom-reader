from flask import Flask, request, jsonify
import requests
import hmac
import hashlib
import time
import json
import logging
import threading
import schedule
import os
import re
import unicodedata
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger()

app = Flask(__name__)

FVG_BOT_TOKEN = "8573057660:AAFrNwYM3A2IthWDfZFdKcD4T0O2zyIEcTI"
FVG_API       = f"https://api.telegram.org/bot{FVG_BOT_TOKEN}"
TG_CHAT_ID    = os.environ.get("TG_CHAT_ID", "811792517")

BINANCE_API_KEY    = os.environ.get("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY", "")
BINANCE_URL        = "https://fapi.binance.com"

RISK_USDT    = float(os.environ.get("RISK_USDT", "10"))
LEVERAGE     = int(os.environ.get("LEVERAGE", "10"))
TRADE_ACTIVE = os.environ.get("TRADE_ACTIVE", "false").lower() == "true"

def strip_emojis(text):
    cleaned = ""
    for ch in text:
        cat = unicodedata.category(ch)
        if cat not in ("So", "Mn") and ord(ch) < 0x10000:
            cleaned += ch
        elif ch in (" ", "\t"):
            cleaned += ch
    return cleaned.strip()

def normalize_text(text):
    text = text.replace("\\n", "\n")
    return text

def _parse_field(text, *keys):
    text = normalize_text(text)
    for line in text.split("\n"):
        line_clean = strip_emojis(line).strip()
        for key in keys:
            key_clean = key.strip()
            if key_clean in line_clean:
                val = line_clean.split(key_clean, 1)[-1].strip()
                val = val.split()[0] if val else None
                if val:
                    return val
    return None

def _parse_float(text, *keys):
    val = _parse_field(text, *keys)
    if val:
        try:
            return float(val.replace(",", "."))
        except:
            pass
    return None

def _parse_yon(text):
    text = normalize_text(text)
    for line in text.split("\n"):
        up = line.upper()
        if "EMELLIYYATA GIR" in up or "ISLEME GIR" in up:
            if "LONG" in up:
                return "LONG"
            elif "SHORT" in up:
                return "SHORT"
    return None

def _parse_rr(text):
    match = re.search(r'[+]?(\d+\.?\d*)R', text)
    if match:
        try:
            return float(match.group(1))
        except:
            pass
    return 0.0

class GunlukIstat:
    def __init__(self):
        self.reset()

    def reset(self):
        self.toplam   = 0
        self.full_win = 0
        self.be       = 0
        self.stop     = 0
        self.net_r    = 0.0

    def rapor_olustur(self):
        tarih   = datetime.now(timezone.utc).strftime("%d/%m/%Y")
        kapanan = self.full_win + self.be + self.stop
        if kapanan == 0:
            return None
        ugur  = (self.full_win + self.be) * 100.0 / kapanan
        durum = "QAZANCLI" if self.net_r >= 0 else "ZIYANLI"
        r_str = ("+" if self.net_r >= 0 else "") + f"{self.net_r:.2f}"
        usd   = self.net_r * RISK_USDT
        usd_s = ("+" if usd >= 0 else "") + f"{usd:.2f}"
        return (
            f"GUN SONU HESABATI\n"
            f"{tarih}\n\n"
            f"Veziyyet: {durum}\n\n"
            f"Umumi Emeliyyat: {kapanan}\n"
            f"Full Win:        {self.full_win}\n"
            f"Risksiz BE:      {self.be}\n"
            f"Stop:            {self.stop}\n\n"
            f"Ugur: %{ugur:.1f}\n"
            f"Net R: {r_str}R\n"
            f"Net USD: {usd_s}$"
        )

istat = GunlukIstat()
aktif_islem
