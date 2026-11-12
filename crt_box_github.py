"""
CRT Candle Box - GitHub Actions version (GUI vagar, fakt Telegram)
Aa script dar thodi minute e GitHub Actions chalave chhe:
  1. Yahoo mathi closed candle lave (1H / 15m / 5m)
  2. CRT box shodhe (app jevo j logic)
  3. navo box hoy to Telegram par automatic moke
  4. state.json ma yaad rakhe (jethi ek j box be vaar na jay)

Telegram Token / Chat ID GitHub Secrets ma mukvo: TG_TOKEN, TG_CHAT
"""

import html as htmlmod
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

TZ = ZoneInfo("Asia/Kolkata")
TF_BOX = {"1H": ("1h", timedelta(hours=1)),
          "15m": ("15m", timedelta(minutes=15)),
          "5m": ("5m", timedelta(minutes=5))}
STATE_FILE = Path(__file__).with_name("state.json")


# ---------- env helpers ----------
def env_str(name, default):
    v = os.environ.get(name)
    return default if v is None or v.strip() == "" else v.strip()


def env_bool(name, default):
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def env_int(name, default):
    try:
        return int(env_str(name, str(default)))
    except ValueError:
        return default


def load_cfg() -> dict:
    tf = env_str("BOX_TF", "1H")
    if tf not in TF_BOX:
        tf = "1H"
    return {
        "symbol": env_str("SYMBOL", "GC=F"), "tf": tf,
        "show_bull": env_bool("SHOW_BULL", True), "show_bear": env_bool("SHOW_BEAR", True),
        "strict": env_bool("STRICT", False), "ignore_outside": env_bool("IGNORE_OUTSIDE", True),
        "allow_outside_close": env_bool("ALLOW_OUTSIDE_CLOSE", True),
        "use_window": env_bool("USE_WINDOW", False),
        "start_h": env_int("START_H", 18), "start_m": env_int("START_M", 0),
        "end_h": env_int("END_H", 8), "end_m": env_int("END_M", 0),
        "box_source": env_str("BOX_SOURCE", "Range Candle (previous)"),
        "extend": env_int("EXTEND", 1),
    }


# ---------- DATA + CRT LOGIC (app jevo j) ----------
def fetch_closed_candles(symbol: str, tf: str) -> pd.DataFrame:
    interval, delta = TF_BOX[tf]
    df = None
    for attempt in range(3):
        try:
            df = yf.download(symbol, period="7d", interval=interval, progress=False, auto_adjust=False)
            if df is not None and not df.empty:
                break
        except Exception as e:
            print(f"Yahoo ભૂલ (try {attempt + 1}): {e}")
        time.sleep(5)
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close"]].dropna()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(TZ)
    now = datetime.now(TZ)
    return df[df.index + delta <= now]


def in_window(ts, cfg) -> bool:
    if not cfg["use_window"]:
        return True
    cur = ts.hour * 60 + ts.minute
    s = cfg["start_h"] * 60 + cfg["start_m"]
    e = cfg["end_h"] * 60 + cfg["end_m"]
    if s <= e:
        return s <= cur < e
    return cur >= s or cur < e


def detect_crt(c1, c2, c1_time, cfg):
    o1, h1, l1, cl1 = c1["Open"], c1["High"], c1["Low"], c1["Close"]
    h2, l2 = c2["High"], c2["Low"]

    bull_sweep = l1 < l2 and cl1 > l2 and (not cfg["strict"] or cl1 < h2)
    bear_sweep = h1 > h2 and cl1 < h2 and (not cfg["strict"] or cl1 > l2)
    outside = (
        cfg["ignore_outside"] and h1 > h2 and l1 < l2
        and not (cfg["allow_outside_close"] and (cl1 > h2 or cl1 < l2))
    )
    bull = bull_sweep and (not bear_sweep or cl1 >= o1) and not outside
    bear = bear_sweep and (not bull_sweep or cl1 < o1) and not outside

    if not in_window(c1_time, cfg):
        return None
    if bull and cfg["show_bull"]:
        return "bull"
    if bear and cfg["show_bear"]:
        return "bear"
    return None


def find_boxes(df: pd.DataFrame, cfg: dict) -> list:
    delta = TF_BOX[cfg["tf"]][1]
    boxes = []
    for i in range(1, len(df)):
        c1, c2 = df.iloc[i], df.iloc[i - 1]
        t1, t2 = df.index[i], df.index[i - 1]
        kind = detect_crt(c1, c2, t1, cfg)
        if kind is None:
            continue
        if cfg["box_source"] == "CRT Candle":
            bh, bl, bstart = c1["High"], c1["Low"], t1
        else:
            bh, bl, bstart = c2["High"], c2["Low"], t2
        boxes.append({
            "key": f"{cfg['tf']}|{t1.isoformat()}",
            "kind": kind, "time": t1,
            "high": float(bh), "low": float(bl), "mid": float((bh + bl) / 2),
            "bstart": bstart, "bend": bstart + delta * (1 + cfg["extend"]),
        })
    return boxes


# ---------- Telegram ----------
def tg_box_text(b, boxes, cfg) -> str:
    total = sum(1 for x in boxes if x["time"].date() == b["time"].date())
    is_bull = b["kind"] == "bull"
    name = "CRT Bull 🟢" if is_bull else "CRT Bear 🔴"
    if is_bull:
        meaning = ("CRT કેન્ડલે પાછલી કેન્ડલનો <b>Low sweep</b> કર્યો અને પછી તેની ઉપર close થઈ. "
                   "એટલે નીચેની liquidity લેવાઈ ગઈ અને ભાવ ઉપર તરફ (Bullish) પાછો ફર્યો.")
    else:
        meaning = ("CRT કેન્ડલે પાછલી કેન્ડલનો <b>High sweep</b> કર્યો અને પછી તેની નીચે close થઈ. "
                   "એટલે ઉપરની liquidity લેવાઈ ગઈ અને ભાવ નીચે તરફ (Bearish) પાછો ફર્યો.")
    return (
        f"<b>{name}</b> | {cfg['symbol']} | {cfg['tf']}\n"
        f"CRT કેન્ડલ: {b['time']:%d-%b %H:%M} IST\n"
        f"<i>{meaning}</i>\n\n"
        f"📈 Box High: <b>{b['high']:.2f}</b>\n"
        f"   ↳ Box નો સૌથી ઊંચો ભાવ (ઉપરની સીમા / resistance)\n\n"
        f"📉 Box Low: <b>{b['low']:.2f}</b>\n"
        f"   ↳ Box નો સૌથી નીચો ભાવ (નીચેની સીમા / support)\n\n"
        f"➗ Mid (50%): <b>{b['mid']:.2f}</b>\n"
        f"   ↳ High અને Low ની બરાબર વચ્ચેનો ભાવ (equilibrium)\n\n"
        f"🕒 Box સમય: {b['bstart']:%H:%M} → {b['bend']:%H:%M}\n"
        f"   ↳ આટલા સમય સુધી Box ચાર્ટ પર માન્ય ગણાય\n\n"
        f"આજનો box નંબર: <b>{total}</b>\n"
        f"ℹ️ આ rule-આધારિત signal છે, નાણાકીય સલાહ નથી."
    )


def tg_send(token: str, chat: str, text: str) -> bool:
    """ek message moke. HTML ma bhul hoy to plain text ma moke. saru thay to True"""
    def post(t, use_html=True):
        fields = {"chat_id": chat, "text": t}
        if use_html:
            fields["parse_mode"] = "HTML"
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=20):
            pass

    hints = {400: "Chat ID ખોટો (ke bot ne pehla /start nathi karyu)", 401: "Bot Token ખોટો",
             403: "Bot block chhe ke group ma add nathi", 404: "Bot Token ખોટો"}
    try:
        post(text)
        return True
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "ignore")
        except Exception:
            body = ""
        if e.code == 400 and "parse" in body.lower():
            try:
                post(htmlmod.unescape(re.sub(r"<[^>]+>", "", text)), use_html=False)
                return True
            except Exception as e2:
                print(f"Telegram plain-text pan na gayo: {e2}")
                return False
        print(f"❌ Telegram ભૂલ {e.code}: {hints.get(e.code, '')} {body[:150]}")
        return False
    except Exception as e:
        print(f"❌ Telegram ભૂલ: {e}")
        return False


# ---------- state ----------
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(seen: set, tf: str):
    keep = sorted(seen)[-300:]
    STATE_FILE.write_text(json.dumps({"tf": tf, "seen": keep}, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- main ----------
def main() -> int:
    token = os.environ.get("TG_TOKEN", "").strip()
    chat = os.environ.get("TG_CHAT", "").strip()
    if not token or not chat:
        print("❌ GitHub Secrets ma TG_TOKEN ane TG_CHAT mukvo")
        return 1

    if os.environ.get("TEST", "").strip().lower() == "true":
        ok = tg_send(token, chat, "✅ <b>CRT Box Scanner (GitHub)</b>\nTelegram connection બરાબર ચાલે છે.")
        print("Test message:", "ગયો" if ok else "ના ગયો")
        return 0 if ok else 1

    cfg = load_cfg()
    tf = cfg["tf"]
    df = fetch_closed_candles(cfg["symbol"], tf)
    if df.empty:
        print("ડેટા ખાલી મળ્યો (market band hoy shake). Pachi try karshe.")
        return 0

    boxes = find_boxes(df, cfg)
    state = load_state()
    first = (not state) or state.get("tf") != tf
    seen = set(state.get("seen", [])) if not first else set()
    print(f"{cfg['symbol']} {tf}: {len(df)} કેન્ડલ, {len(boxes)} box, છેલ્લી કેન્ડલ {df.index[-1]:%d-%b %H:%M}")

    if first:
        seen |= {b["key"] for b in boxes}
        save_state(seen, tf)
        tg_send(token, chat, f"✅ <b>CRT Box Scanner (GitHub)</b>\n{cfg['symbol']} · {tf} શરૂ થયું.\n"
                             f"હવે નવો box બને ત્યારે અહીં આપોઆપ આવશે.")
        print("પહેલી વાર: junu box seen mark karya, Telegram par nathi mokalya.")
        return 0

    max_age = timedelta(hours=env_int("MAX_AGE_HOURS", 6))
    now = datetime.now(TZ)
    failed = False
    sent = 0
    for b in boxes:
        if b["key"] in seen:
            continue
        if now - b["time"] > max_age:
            seen.add(b["key"])          # bahu junu: chupchap seen
            continue
        if sent:
            time.sleep(1.2)             # Telegram speed limit
        if tg_send(token, chat, tg_box_text(b, boxes, cfg)):
            seen.add(b["key"])
            sent += 1
            print(f"✅ Telegram par gayo: {b['kind']} {b['time']:%d-%b %H:%M}")
        else:
            failed = True               # seen ma nahi nakhi, pachi try thase
    save_state(seen, tf)
    if not sent and not failed:
        print("કોઈ નવો box નથી.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
