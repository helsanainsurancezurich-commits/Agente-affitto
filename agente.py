import json, math, os, re, time, requests
from datetime import datetime, timedelta, timezone, date

# ================= CONFIG =================
CENTRO = (47.3769, 8.5417)
RAGGIO_KM = 14
MAX_MINUTI_HB = 35
MAX_PREZZO = 3000
MIN_LOCALI, MAX_LOCALI = 3.0, 4.5
MIN_MQ = 60
MAX_ETA_GIORNI = 60            # consiglio 21 per vedere solo annunci freschi
INGRESSO_DA = date(2027, 1, 1)
INGRESSO_A = date(2027, 3, 31)  # metti date(2027, 4, 15) se potete slittare ad aprile
MESI_MIN_CONTRATTO = 24         # scarta contratti a termine più brevi di 2 anni
SCARTA_SUBITO = False           # True = scarta anche "ab sofort" senza data
ESCLUDI_CITTA = []              # esempio: ["Kloten", "Opfikon"]
MAX_PER_CICLO = 15
DURATA_MIN = 25
INTERVALLO_SEC = 180
MAX_PIN = 1000
SEEN_FILE = "seen.json"

W, E, S, N = 8.35, 8.73, 47.25, 47.50
MLON, MLAT = (W + E) / 2, (S + N) / 2
TILES = [(W, MLON, S, MLAT), (MLON, E, S, MLAT), (W, MLON, MLAT, N), (MLON, E, MLAT, N)]

SCARTA = ["zwischenmiete", "untermiete", "sublet", "wg-zimmer", "wohngemeinschaft",
          "nur für studenten", "nur an studierende", "wohnungstausch", "tauschwohnung"]
AVVISI = ["nachmieter", "lärm", "strassenlärm", "keine haustiere", "haustiere nicht"]
SUBITO = ["ab sofort", "per sofort", "sofort bezugsbereit", "sofort verfügbar", "sofort frei"]
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
    """True se almeno un destinatario ha ricevuto il messaggio."""
    t = os.environ["TG_TOKEN"]
    ids = [c.strip() for c in os.environ["TG_CHAT"].split(",") if c.strip()]
    ok = False
    for c in ids:
        try:
            r = requests.post(f"https://api.telegram.org/bot{t}/sendMessage",
                              data={"chat_id": c, "text": testo}, timeout=20)
            print("Telegram", c[-4:], r.status_code, "" if r.ok else r.text[:120])
            ok = ok or r.ok
        except Exception as e:
            print("Telegram errore", c[-4:], e)
        time.sleep(0.5)
    return ok

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

def valuta_termine(testo, is_temporary, ingresso):
    """Ritorna ('ok'|'scarta'|'incerto', nota) per i contratti a termine."""
    a_termine = bool(is_temporary) or bool(
        re.search(r"(?<!un)befristet|zeitlich begrenzt|fixed[- ]term", testo))
    if not a_termine:
        return "ok", ""
    inizio = ingresso or INGRESSO_DA
    base = r"(?:(?<!un)befristet|zeitlich begrenzt|mietdauer|laufzeit)[^\n]{0,60}?"
    mesi = None
    try:
        m = re.search(base + r"(\d{1,2})\.(\d{1,2})\.(20\d\d)", testo)
        if m:
            fine = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            mesi = (fine.year - inizio.year) * 12 + fine.month - inizio.month
        else:
            m = re.search(base + r"(\d{1,2})\.(20\d\d)", testo)
            if m:
                mesi = (int(m.group(2)) - inizio.year) * 12 + int(m.group(1)) - inizio.month
            else:
                m = re.search(base + r"(\d+)\s*(jahr|monat)", testo)
                if m:
                    n = int(m.group(1))
                    mesi = n * 12 if m.group(2) == "jahr" else n
    except ValueError:
        mesi = None
    if mesi is not None:
        if mesi < MESI_MIN_CONTRATTO:
            return "scarta", f"termine tra {mesi} mesi"
        return "ok", f"contratto a termine (circa {mesi} mesi)"
    return "incerto", "contratto a termine: durata da verificare"

def salva(seen):
    json.dump(sorted(seen)[-8000:], open(SEEN_FILE, "w"))

def ciclo(seen):
    pins = {}
    for west, east, south, north in TILES:
        rp = requests.get("https://flatfox.ch/api/v1/pin/",
                          params={"offer_type": "RENT", "object_category": "APARTMENT",
                                  "max_price": MAX_PREZZO, "min_rooms": MIN_LOCALI,
                                  "max_rooms": MAX_LOCALI, "max_count": MAX_PIN,
                                  "west": west, "east": east, "south": south, "north": north},
                          timeout=40)
        if not rp.ok:
            print("PIN errore", rp.status_code, rp.text[:200])
            continue
        dati = lista(rp.json())
        if len(dati) >= MAX_PIN:
            print("ATTENZIONE: riquadro pieno, possibili annunci tagliati:", len(dati))
        for p in dati:
            if p.get("pk") is not None:
                pins[p["pk"]] = p
    nuovi_pks = [pk for pk in pins if str(pk) not in seen]
    print(f"PIN totali: {len(pins)} | nuovi da controllare: {len(nuovi_pks)}")

    annunci = []
    for i in range(0, len(nuovi_pks), 30):
        chunk = nuovi_pks[i:i + 30]
        rd = requests.get("https://flatfox.ch/api/v1/public-listing/",
                          params=[("pk", pk) for pk in chunk] + [("limit", 100)], timeout=40)
        if rd.ok:
            insieme = set(chunk)
            annunci += [a for a in lista(rd.json()) if a.get("pk") in insieme]
    annunci.sort(key=lambda a: str(a.get("published") or a.get("created") or ""), reverse=True)
    print("Dettagli ottenuti:", len(annunci))

    adesso = datetime.now(timezone.utc)
    nuovi = 0
    for a in annunci:
        if nuovi >= MAX_PER_CICLO:
            break
        pk = str(a.get("pk"))
        seen.add(pk)

        if a.get("reserved") or a.get("is_swap"):
            continue
        if str(a.get("city") or "").lower() in [c.lower() for c in ESCLUDI_CITTA]:
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

        stato, nota = valuta_termine(testo, a.get("is_temporary"), ingresso)
        if stato == "scarta":
            continue
        subito = ingresso is None and any(w in testo for w in SUBITO)
        if subito and SCARTA_SUBITO:
            continue

        minuti = minuti_a_hb(lat, lon)
        if minuti is not None and minuti > MAX_MINUTI_HB:
            continue

        avvisi = [w for w in AVVISI if w in testo]
        if nota:
            avvisi.append(nota)
        if subito:
            avvisi.append("disponibile subito (data non indicata)")
        if a.get("is_furnished"):
            avvisi.append("arredato")
        piano = a.get("floor")
        if piano is not None and str(piano) in ("0", "-1"):
            avvisi.append("piano terra")
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
        if telegram(msg):
            nuovi += 1
        else:
            seen.discard(pk)
            seen.discard(chiave)
            print("Consegna fallita, riprovo al prossimo ciclo")
            break
    print(f"Notificati in questo ciclo: {nuovi}")

seen = set(map(str, json.load(open(SEEN_FILE)))) if os.path.exists(SEEN_FILE) else set()
fine = time.time() + DURATA_MIN * 60
while True:
    print("=== ciclo", datetime.now(timezone.utc).strftime("%H:%M:%S"), "UTC ===")
    try:
        ciclo(seen)
    except Exception as e:
        print("Errore ciclo:", e)
    salva(seen)
    if time.time() + INTERVALLO_SEC > fine:
        break
    time.sleep(INTERVALLO_SEC)
