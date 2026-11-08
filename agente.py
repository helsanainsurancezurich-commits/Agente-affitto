import json, math, os, re, sys, requests
from datetime import datetime, timedelta, timezone, date

# ================= CONFIG =================
CENTRO = (47.3769, 8.5417)
RAGGIO_KM = 14
MAX_MINUTI_HB = 35
MAX_PREZZO = 3000
MIN_LOCALI, MAX_LOCALI = 3.0, 4.5
MIN_MQ = 60
MAX_ETA_GIORNI = 60
INGRESSO_DA = date(2026, 12, 1)
INGRESSO_A = date(2027, 4, 30)
MAX_PER_RUN = 15
BBOX = {"west": 8.35, "east": 8.73, "south": 47.25, "north": 47.50}
SEEN_FILE = "seen.json"

SCARTA = ["zwischenmiete", "untermiete", "sublet", "wg-zimmer", "wohngemeinschaft",
          "nur für studenten", "nur an studierende", "wohnungstausch", "tauschwohnung"]
AVVISI = ["befristet", "zeitlich begrenzt", "nachmieter", "lärm",
          "strassenlärm", "erdgeschoss", "parterre", "keine haustiere", "haustiere nicht"]
CUCINA = ["küche", "einbauküche", "geschirrspüler", "kochinsel", "wohnküche"]
# ==========================================

def km(lat, lon):
    r = 6371
    p1, p2 = math.radians(CENTRO[0]), math.radians(lat)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon - CENTRO[1]) / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))

def prossimo_lunedi():
    d = date.today() + timedelta(days=1)
    while d.weekday() != 0:
        d += timedelta(days=1)
    return d.isoformat()

def minuti_a_hb(lat, lon):
    try:
        r = requests.get("https://transport.opendata.ch/v1/connections",
                         params={"from": f"{lat},{lon}", "to": "Zürich HB", "limit": 4,
                                 "date": prossimo_lunedi(), "time": "08:00"}, timeout=20)
        r.raise_for_status()
        durate = []
        for c in r.json().get("connections", []):
            m = re.match(r"(\d+)d(\d+):(\d+):(\d+)", c["duration"])
            if m:
                g, h, mi, _ = map(int, m.groups())
                durate.append(g * 1440 + h * 60 + mi)
        return min(durate) if durate else None
    except Exception:
        return None

def telegram(testo):
    t = os.environ["TG_TOKEN"]
    for c in os.environ["TG_CHAT"].split(","):
        requests.post(f"https://api.telegram.org/bot{t}/sendMessage",
                      data={"chat_id": c.strip(), "text": testo}, timeout=20)

def parse_dt(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None

def parse_data(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except Exception:
        return None

def lista(dati):
    return dati if isinstance(dati, list) else dati.get("results", [])

seen = set(map(str, json.load(open(SEEN_FILE)))) if os.path.exists(SEEN_FILE) else set()

# 1) ricerca per zona/prezzo/locali (endpoint mappa di Flatfox)
rp = requests.get("https://flatfox.ch/api/v1/pin/",
                  params={"offer_type": "RENT", "object_category": "APARTMENT",
                          "max_price": MAX_PREZZO, "min_rooms": MIN_LOCALI,
                          "max_rooms": MAX_LOCALI, "max_count": 400, **BBOX}, timeout=40)
print("PIN status:", rp.status_code)
if not rp.ok:
    print(rp.text[:500])
    sys.exit(1)
pins = lista(rp.json())
print("PIN totali:", len(pins), "| esempio:", pins[0] if pins else None)
pks = [p.get("pk") for p in pins if p.get("pk") is not None]
nuovi_pks = [pk for pk in pks if str(pk) not in seen]
print("Nuovi da controllare:", len(nuovi_pks))

# 2) dettagli solo dei nuovi
annunci = []
for i in range(0, len(nuovi_pks), 30):
    chunk = nuovi_pks[i:i + 30]
    rd = requests.get("https://flatfox.ch/api/v1/public-listing/",
                      params=[("pk", pk) for pk in chunk] + [("limit", 100)], timeout=40)
    if rd.ok:
        insieme = set(chunk)
        annunci += [a for a in lista(rd.json()) if a.get("pk") in insieme]
print("Dettagli ottenuti:", len(annunci))
if nuovi_pks and not annunci:
    print("Nessun dettaglio ottenuto: il filtro per pk potrebbe non funzionare")
    sys.exit(1)

adesso = datetime.now(timezone.utc)
nuovi = 0

for a in annunci:
    if nuovi >= MAX_PER_RUN:
        break                      # gli altri restano "non visti" e arrivano al prossimo giro
    pk = str(a.get("pk"))
    seen.add(pk)

    if a.get("reserved") or a.get("is_swap"):
        continue
    try:
        prezzo = a.get("rent_gross")
        if not prezzo and a.get("rent_net") is not None:
            prezzo = a["rent_net"] + (a.get("rent_charges") or 0)
        if not prezzo:
            prezzo = a.get("price_display")
        locali = float(a.get("number_of_rooms") or 0)
        lat, lon = a.get("latitude"), a.get("longitude")
        mq = a.get("surface_living")
        if not (prezzo and lat and lon):
            continue
        if prezzo > MAX_PREZZO or not (MIN_LOCALI <= locali <= MAX_LOCALI):
            continue
        if mq and mq < MIN_MQ:
            continue
        d = km(lat, lon)
        if d > RAGGIO_KM:
            continue
    except (TypeError, ValueError):
        continue

    creato = parse_dt(a.get("published") or a.get("created") or "")
    eta = (adesso - creato).days if creato else None
    if eta is not None and eta > MAX_ETA_GIORNI:
        continue

    ingresso = parse_data(a.get("moving_date")) if a.get("moving_date") else None
    if ingresso and not (INGRESSO_DA <= ingresso <= INGRESSO_A):
        continue

    chiave = f"{a.get('street')}|{a.get('zipcode')}|{locali}|{prezzo}"
    if a.get("street") and chiave in seen:
        continue
    seen.add(chiave)

    testo = ((a.get("description") or "") + " " + (a.get("public_title") or "")).lower()
    if any(w in testo for w in SCARTA):
        continue

    minuti = minuti_a_hb(lat, lon)
    if minuti is not None and minuti > MAX_MINUTI_HB:
        continue

    avvisi = [w for w in AVVISI if w in testo]
    if a.get("is_temporary"):
        avvisi.append("contratto a termine")
    if a.get("is_furnished"):
        avvisi.append("arredato")
    cucina = any(w in testo for w in CUCINA)
    fresco = "🔥 " if (eta is not None and eta < 1) else ""
    prezzo_mq = f" · {prezzo / mq:.0f} CHF/m²" if mq else ""

    msg = (f"{fresco}🏠 {a.get('short_title') or a.get('public_title')}\n"
           f"{locali} locali · {mq or '?'} m² · {prezzo} CHF{prezzo_mq}\n"
           f"{a.get('city')} · {d:.1f} km · "
           f"{str(minuti) + ' min a HB' if minuti is not None else 'tempo HB n/d'}\n"
           f"Ingresso: {ingresso or 'non indicato'} · online da: "
           f"{str(eta) + ' gg' if eta is not None else 'n/d'}\n"
           f"Cucina: {'sì' if cucina else 'non menzionata'}\n"
           + (f"⚠️ {', '.join(avvisi)}\n" if avvisi else "")
           + "https://flatfox.ch" + a.get("url", ""))
    telegram(msg)
    nuovi += 1

json.dump(sorted(seen)[-8000:], open(SEEN_FILE, "w"))
print(f"{len(annunci)} letti, {nuovi} notificati")
