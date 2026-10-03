"""Gold Alert Bot : alertes Telegram pour l'or (calendrier, news, volatilité)."""
import os, re, json, time, html, hashlib, logging, threading, datetime as dt
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
import feedparser

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("goldbot")

# ---------- Config (variables d'environnement) ----------
SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_KEY"]
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_IDS = [c.strip() for c in os.environ["TELEGRAM_CHAT_ID"].split(",") if c.strip()]
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
GEMINI_DAILY_MAX = int(os.environ.get("GEMINI_DAILY_MAX", "900"))
GOLD_SYMBOL = os.environ.get("GOLD_SYMBOL", "GC=F")
LOOP_SECONDS = 30
NEWS_EVERY = 90
UA = {"User-Agent": "Mozilla/5.0 (compatible; GoldAlertBot/1.0)"}

DEFAULTS = {
    "bot_enabled": True, "threshold": 7, "pre_alert_minutes": [60, 15],
    "stop_before_min": 15, "stop_after_min": 30,
    "vol_threshold_usd": 15, "vol_window_min": 5,
    "src_calendar": True, "src_news": True, "src_volatility": True,
}

# ---------- Supabase (REST) ----------
SBH = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}", "Content-Type": "application/json"}


def sb_get(table, params=None):
    r = requests.get(f"{SB_URL}/rest/v1/{table}", headers=SBH, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def sb_patch(table, match, data):
    requests.patch(f"{SB_URL}/rest/v1/{table}", headers=SBH, params=match, json=data, timeout=15).raise_for_status()


def sb_upsert(table, data):
    h = {**SBH, "Prefer": "resolution=merge-duplicates"}
    requests.post(f"{SB_URL}/rest/v1/{table}", headers=h, json=data, timeout=15).raise_for_status()


def insert_alert(kind, level, title, body, source, key):
    """Insère l'alerte. Retourne True si nouvelle (pas un doublon)."""
    h = {**SBH, "Prefer": "resolution=ignore-duplicates,return=representation"}
    row = {"kind": kind, "level": level, "title": title, "body": body, "source": source, "dedupe_key": key}
    r = requests.post(f"{SB_URL}/rest/v1/alerts", headers=h, params={"on_conflict": "dedupe_key"}, json=row, timeout=15)
    r.raise_for_status()
    return bool(r.json())


# ---------- Telegram ----------
def tg_send(text, chat_ids=None):
    for cid in chat_ids or CHAT_IDS:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": cid, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=15,
            )
        except Exception as e:
            log.warning("Telegram erreur: %s", e)


def notify(kind, level, title, body, source, key, emoji):
    try:
        new = insert_alert(kind, level, title, body, source, key)
    except Exception as e:
        log.warning("insert_alert: %s", e)
        new = True  # en cas de panne base, on alerte quand même
    if new:
        tg_send(f"{emoji} <b>{html.escape(title)}</b>\n{html.escape(body)}")
    return new


def level_emoji(score):
    return "🔴" if score >= 8 else "🟠" if score >= 6 else "🟡"


# ---------- Réglages / statut ----------
SETTINGS = dict(DEFAULTS)
STATE = {"gemini_calls": 0, "gemini_day": dt.date.today().isoformat(), "gold": None, "upcoming": []}


def load_settings():
    try:
        rows = sb_get("settings", {"id": "eq.1"})
        if rows:
            SETTINGS.update({k: v for k, v in rows[0].items() if k in DEFAULTS and v is not None})
    except Exception as e:
        log.warning("settings: %s", e)
    return SETTINGS


def load_state():
    try:
        rows = sb_get("bot_status", {"id": "eq.1"})
        if rows and rows[0].get("gemini_day") == dt.date.today().isoformat():
            STATE["gemini_calls"] = rows[0].get("gemini_calls") or 0
    except Exception as e:
        log.warning("state: %s", e)


def write_status():
    try:
        sb_upsert("bot_status", {
            "id": 1,
            "last_cycle": dt.datetime.now(dt.timezone.utc).isoformat(),
            "gemini_calls": STATE["gemini_calls"],
            "gemini_day": STATE["gemini_day"],
            "info": {"gold": STATE["gold"], "upcoming": STATE["upcoming"], "enabled": SETTINGS["bot_enabled"]},
        })
    except Exception as e:
        log.warning("write_status: %s", e)


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


# ---------- Couche 1 : calendrier ----------
CAL = {"ts": 0.0, "events": []}
CAL_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
]


def get_events():
    if time.time() - CAL["ts"] < 1800:
        return CAL["events"]
    events, ok = [], False
    for url in CAL_URLS:
        try:
            r = requests.get(url, headers=UA, timeout=20)
            r.raise_for_status()
            ok = True
            for e in r.json():
                if e.get("country") != "USD" or e.get("impact") != "High":
                    continue
                t = dt.datetime.fromisoformat(e["date"]).astimezone(dt.timezone.utc)
                events.append({"title": e.get("title", "?"), "time": t,
                               "forecast": e.get("forecast"), "previous": e.get("previous"),
                               "actual": e.get("actual")})
        except Exception as ex:
            log.warning("calendrier %s: %s", url.split("/")[-1], ex)
    if ok:
        CAL.update(ts=time.time(), events=events)
    else:
        CAL["ts"] = time.time() - 1500  # nouvel essai dans 5 min
    return CAL["events"]


def check_calendar(s):
    now = utcnow()
    groups = {}
    for e in get_events():
        groups.setdefault(e["time"], []).append(e)
    STATE["upcoming"] = [
        {"time": t.isoformat(), "title": " + ".join(x["title"] for x in evs)}
        for t, evs in sorted(groups.items()) if t > now
    ][:8]

    stop_before, stop_after = int(s["stop_before_min"]), int(s["stop_after_min"])
    times = sorted(set(int(m) for m in s["pre_alert_minutes"]) | {stop_before}, reverse=True)
    for t, evs in sorted(groups.items()):
        rem = (t - now).total_seconds() / 60
        names = " + ".join(x["title"] for x in evs)
        hhmm = t.strftime("%H:%M UTC")
        tk = t.strftime("%Y%m%d%H%M")
        if rem > 0:
            for m in times:
                if m - 5 < rem <= m:  # fenêtre de tolérance 5 min
                    stop = m <= stop_before
                    title = f"{'⛔ ARRÊTE LE BOT — ' if stop else ''}{names} dans ~{max(1, round(rem))} min"
                    detail = " | ".join(
                        f"{x['title']}: prévu {x.get('forecast') or '?'} (préc. {x.get('previous') or '?'})" for x in evs)
                    body = f"{hhmm} • Impact ÉLEVÉ sur l'or\n{detail}"
                    notify("calendar", 8 if stop else 5, title, body, "ForexFactory", f"cal:{tk}:pre{m}",
                           "⛔" if stop else "🗓")
        else:
            back = -rem
            if stop_after <= back < stop_after + 5:
                res = " | ".join(f"{x['title']}: {x['actual']}" for x in evs if x.get("actual"))
                body = f"{names} publié à {hhmm}." + (f"\nRésultats : {res}" if res else "")
                notify("calendar", 3, "✅ Tu peux relancer le bot", body, "ForexFactory", f"cal:{tk}:resume", "✅")


# ---------- Couche 2 : news + Gemini ----------
GN = "https://news.google.com/rss/search?hl=en-US&gl=US&ceid=US:en&q="
FEEDS = [
    (GN + "gold+price+OR+XAUUSD+when:1h", False),
    (GN + "Federal+Reserve+OR+Powell+OR+FOMC+when:1h", False),
    (GN + "war+OR+missile+OR+airstrike+OR+sanctions+OR+ceasefire+when:1h", True),
    (GN + "tariffs+OR+Treasury+yields+OR+dollar+index+OR+inflation+when:1h", True),
    (GN + "central+bank+gold+purchases+OR+PBOC+gold+when:1h", False),
    ("https://www.fxstreet.com/rss/news", True),
    ("https://www.federalreserve.gov/feeds/press_all.xml", False),
    ("http://feeds.bbci.co.uk/news/world/rss.xml", True),
]
KEYWORDS = re.compile(
    r"gold|xau|bullion|fed\b|fomc|powell|rate (cut|hike|decision)|inflation|cpi|nfp|payroll|jobs report|"
    r"treasury|yield|dollar|tariff|sanction|war\b|invasion|missile|airstrike|nuclear|ceasefire|attack|"
    r"escalat|central bank|ecb|boj|pboc|recession|default|shutdown|safe.haven|oil|opec|iran|israel|russia|"
    r"ukraine|china|taiwan|trump", re.I)
STRONG = re.compile(r"emergency (rate|meeting)|rate (cut|hike)|nuclear|invasion|declares war|"
                    r"gold (surges|plunges|soars|tumbles)|default|ceasefire collapse", re.I)
SEEN = deque(maxlen=3000)
SEEN_SET = set()
LAST = {"news": 0.0}


def norm_hash(title):
    t = re.sub(r"[^a-z0-9 ]", "", title.lower())
    t = re.sub(r"\s+", " ", t).strip()[:80]
    return hashlib.sha1(t.encode()).hexdigest()[:16]


def collect_news():
    items = []
    for url, need_kw in FEEDS:
        try:
            r = requests.get(url, headers=UA, timeout=15)
            feed = feedparser.parse(r.content)
        except Exception as e:
            log.warning("flux %s: %s", url[:50], e)
            continue
        for en in feed.entries[:25]:
            title = (en.get("title") or "").strip()
            if not title:
                continue
            h = norm_hash(title)
            if h in SEEN_SET:
                continue
            pp = en.get("published_parsed") or en.get("updated_parsed")
            if pp:
                age = (utcnow() - dt.datetime(*pp[:6], tzinfo=dt.timezone.utc)).total_seconds() / 60
                if age > 90:
                    SEEN_SET.add(h)
                    continue
            if need_kw and not KEYWORDS.search(title):
                continue
            SEEN_SET.add(h)
            SEEN.append(h)
            items.append({"title": title, "link": en.get("link", ""), "hash": h,
                          "src": feed.feed.get("title", "RSS")[:40]})
    if len(SEEN_SET) > 4000:
        SEEN_SET.intersection_update(set(SEEN))
    return items


PROMPT = (
    "Tu es analyste de l'or (XAUUSD). Pour chaque titre, estime l'impact probable sur le prix de l'or "
    "dans les 60 prochaines minutes. Réponds UNIQUEMENT par un tableau JSON: "
    '[{"i":int,"score":1-10,"direction":"hausse|baisse|incertain","proba":0-100,"raison":"max 15 mots, en français"}]. '
    "score 8-10 = choc majeur (décision surprise de banque centrale, escalade militaire majeure, donnée US majeure); "
    "5-7 = notable; 1-4 = négligeable. Ignore opinions, analyses techniques et mouvements de prix déjà publiés.\n\nTitres:\n"
)


def gemini_score(items):
    today = dt.date.today().isoformat()
    if STATE["gemini_day"] != today:
        STATE.update(gemini_day=today, gemini_calls=0)
    if not GEMINI_KEY or STATE["gemini_calls"] >= GEMINI_DAILY_MAX:
        return None
    body = PROMPT + "\n".join(f"{i}. {it['title']}" for i, it in enumerate(items))
    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            params={"key": GEMINI_KEY},
            json={"contents": [{"parts": [{"text": body}]}],
                  "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1}},
            timeout=40,
        )
        STATE["gemini_calls"] += 1
        r.raise_for_status()
        txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        data = json.loads(txt)
        return {int(d["i"]): d for d in data}
    except Exception as e:
        log.warning("Gemini: %s", e)
        return None


def check_news(s):
    items = collect_news()
    if not items:
        return
    thr = int(s["threshold"])
    for k in range(0, len(items), 12):
        batch = items[k:k + 12]
        scores = gemini_score(batch)
        for i, it in enumerate(batch):
            if scores and i in scores:
                d = scores[i]
                score = int(d.get("score", 0))
                direction = d.get("direction", "incertain")
                proba = d.get("proba", "?")
                why = d.get("raison", "")
                tag = ""
            elif STRONG.search(it["title"]):  # secours sans IA
                score, direction, proba, why, tag = 7, "incertain", "?", "mot-clé fort (analyse IA indisponible)", " (sans IA)"
            else:
                continue
            if score >= thr:
                arrow = {"hausse": "📈 hausse", "baisse": "📉 baisse"}.get(direction, "↔️ incertain")
                body = f"{arrow} • probabilité {proba}% • impact {score}/10{tag}\n{why}\n{it['src']} — {it['link']}"
                notify("news", score, it["title"], body, it["src"], f"news:{it['hash']}", level_emoji(score))


# ---------- Couche 3 : volatilité ----------
PRICES = deque()
VOL = {"last_alert": 0.0}


def gold_price():
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{GOLD_SYMBOL}?interval=1m&range=1d"
    r = requests.get(url, headers=UA, timeout=10)
    r.raise_for_status()
    return float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])


def check_volatility(s):
    try:
        p = gold_price()
    except Exception as e:
        log.warning("prix or: %s", e)
        return
    now = time.time()
    STATE["gold"] = round(p, 2)
    PRICES.append((now, p))
    while PRICES and now - PRICES[0][0] > 1800:
        PRICES.popleft()
    win = int(s["vol_window_min"]) * 60
    recent = [x for x in PRICES if now - x[0] <= win]
    if len(recent) < 2:
        return
    lo, hi = min(x[1] for x in recent), max(x[1] for x in recent)
    move = hi - lo
    if move >= float(s["vol_threshold_usd"]) and now - VOL["last_alert"] > 600:
        VOL["last_alert"] = now
        first = recent[0][1]
        arrow = "📈" if p > first else "📉"
        notify("volatility", 8, f"⚡ Or : mouvement de {move:.1f}$ en {s['vol_window_min']} min",
               f"{arrow} {first:.2f} → {p:.2f}\n⛔ Envisage d'arrêter le bot.",
               "Yahoo Finance", f"vol:{int(now // 600)}", "⚡")


# ---------- Commandes Telegram ----------
def set_setting(data):
    sb_patch("settings", {"id": "eq.1"}, {**data, "updated_at": utcnow().isoformat()})
    SETTINGS.update(data)


def handle_cmd(cid, text):
    cmd, *args = text.split()
    cmd = cmd.split("@")[0].lower()
    try:
        if cmd == "/pause":
            set_setting({"bot_enabled": False}); tg_send("⏸ Alertes en pause.", [cid])
        elif cmd == "/go":
            set_setting({"bot_enabled": True}); tg_send("▶️ Alertes actives.", [cid])
        elif cmd == "/niveau" and args and args[0].isdigit() and 1 <= int(args[0]) <= 10:
            set_setting({"threshold": int(args[0])}); tg_send(f"Seuil d'alerte : {args[0]}/10", [cid])
        elif cmd == "/statut":
            s = load_settings()
            up = STATE["upcoming"][0] if STATE["upcoming"] else None
            nxt = f"\nProchaine annonce : {up['title']} ({up['time'][11:16]} UTC)" if up else ""
            tg_send(f"🤖 {'Actif' if s['bot_enabled'] else 'En pause'} • seuil {s['threshold']}/10\n"
                    f"Or : {STATE['gold'] or '?'} $ • Gemini : {STATE['gemini_calls']}/{GEMINI_DAILY_MAX}{nxt}", [cid])
        else:
            tg_send("Commandes : /statut /pause /go /niveau N (1-10)", [cid])
    except Exception as e:
        tg_send(f"Erreur : {html.escape(str(e))}", [cid])


def tg_poll():
    offset = 0
    while True:
        try:
            r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                             params={"timeout": 50, "offset": offset}, timeout=60).json()
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                m = u.get("message") or {}
                cid = str((m.get("chat") or {}).get("id"))
                text = (m.get("text") or "").strip()
                if cid in CHAT_IDS and text.startswith("/"):
                    handle_cmd(cid, text)
        except Exception as e:
            log.warning("poll: %s", e)
            time.sleep(5)


# ---------- Keep-alive (Render gratuit) ----------
class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")

    def do_HEAD(self):
        self.send_response(200); self.end_headers()

    def log_message(self, *a):
        pass


def serve():
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "10000"))), Ping).serve_forever()


# ---------- Boucle principale ----------
def main():
    threading.Thread(target=serve, daemon=True).start()
    threading.Thread(target=tg_poll, daemon=True).start()
    load_state()
    tg_send("🤖 Gold Alert démarré. /statut pour l'état.")
    while True:
        t0 = time.time()
        s = load_settings()
        if s["bot_enabled"]:
            if s["src_calendar"]:
                try: check_calendar(s)
                except Exception as e: log.exception("calendar: %s", e)
            if s["src_volatility"]:
                try: check_volatility(s)
                except Exception as e: log.exception("vol: %s", e)
            if s["src_news"] and time.time() - LAST["news"] >= NEWS_EVERY:
                LAST["news"] = time.time()
                try: check_news(s)
                except Exception as e: log.exception("news: %s", e)
        write_status()
        time.sleep(max(1, LOOP_SECONDS - (time.time() - t0)))


if __name__ == "__main__":
    main()
