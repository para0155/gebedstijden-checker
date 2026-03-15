#!/usr/bin/env python3
"""
Gebedstijden Checker — Web App
"""

import json
import math
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from astral import LocationInfo
from astral.sun import sun, elevation, noon
from flask import Flask, render_template, request, jsonify

app = Flask(__name__, static_folder='static', static_url_path='/static')

CONFIG_BESTAND = Path(__file__).parent / "config.json"

HIJRI_MAANDEN = [
    'Muharram', 'Safar', 'Rabi al-Awwal', 'Rabi al-Thani',
    'Jumada al-Ula', 'Jumada al-Thani', 'Rajab', "Sha'ban",
    'Ramadan', 'Shawwal', 'Dhul Qi\'dah', 'Dhul Hijjah',
]


def gregorian_naar_hijri(jaar, maand, dag):
    """Omm al-Qura-achtige benadering: Gregoriaans naar Hijri conversie."""
    # Bereken Julian Day Number
    if maand <= 2:
        jaar -= 1
        maand += 12
    A = jaar // 100
    B = 2 - A + A // 4
    jd = int(365.25 * (jaar + 4716)) + int(30.6001 * (maand + 1)) + dag + B - 1524.5

    # Hijri conversie vanuit Julian Day
    jd = jd - 1948439.5 + 0.5
    l = int(jd) + 10632
    n = int((l - 1) / 10631)
    l = l - 10631 * n + 354
    j = int((10985 - l) / 5316) * int((50 * l) / 17719) + int(l / 5670) * int((43 * l) / 15238)
    l = l - int((30 - j) / 15) * int((17719 * j) / 50) - int(j / 16) * int((15238 * j) / 43) + 29
    h_maand = int((24 * l) / 709)
    h_dag = l - int((709 * h_maand) / 24)
    h_jaar = 30 * n + j - 30

    maand_naam = HIJRI_MAANDEN[h_maand - 1] if 1 <= h_maand <= 12 else '?'
    return h_dag, h_maand, h_jaar, maand_naam


def gregorian_naar_hijri_str(jaar, maand, dag):
    """Geeft een geformateerde Hijri datum string terug."""
    h_dag, h_maand, h_jaar, maand_naam = gregorian_naar_hijri(jaar, maand, dag)
    return f"{h_dag} {maand_naam} {h_jaar}"


MAX_AFWIJKING_MINUTEN = 5
MAWAQIT_API = "https://mawaqit.net/api/2.0"

BEREKENINGS_METHODES = {
    "MWL":     {"fajr_hoek": 18.0, "isha_hoek": 17.0},
    "ISNA":    {"fajr_hoek": 15.0, "isha_hoek": 15.0},
    "Egypt":   {"fajr_hoek": 19.5, "isha_hoek": 17.5},
    "Makkah":  {"fajr_hoek": 18.5, "isha_hoek": None},
    "Diyanet": {"fajr_hoek": 18.0, "isha_hoek": 17.0},
}

# Mecca coördinaten voor Qibla berekening
MECCA_LAT = 21.4225
MECCA_LON = 39.8262


# ─── Qibla berekening ──────────────────────────────────────────

def bereken_qibla(lat, lon):
    """Bereken de Qibla richting (bearing) van een locatie naar Mecca."""
    lat1 = math.radians(lat)
    lat2 = math.radians(MECCA_LAT)
    delta_lon = math.radians(MECCA_LON - lon)

    x = math.sin(delta_lon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta_lon)

    bearing = math.degrees(math.atan2(x, y))
    return (bearing + 360) % 360


def qibla_richting_tekst(graden):
    """Geeft windrichting tekst voor een bearing."""
    richtingen = [
        (0, "N"), (22.5, "NNO"), (45, "NO"), (67.5, "ONO"),
        (90, "O"), (112.5, "OZO"), (135, "ZO"), (157.5, "ZZO"),
        (180, "Z"), (202.5, "ZZW"), (225, "ZW"), (247.5, "WZW"),
        (270, "W"), (292.5, "WNW"), (315, "NW"), (337.5, "NNW"),
    ]
    for i, (hoek, naam) in enumerate(richtingen):
        volgende = richtingen[(i + 1) % len(richtingen)][0]
        if volgende < hoek:
            volgende += 360
        if hoek <= graden < volgende or (i == len(richtingen) - 1 and graden >= hoek):
            return naam
    return "N"


# ─── Mawaqit API ────────────────────────────────────────────────

def mawaqit_request(url):
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def mawaqit_zoek(zoekterm):
    url = f"{MAWAQIT_API}/mosque/search?word={urllib.parse.quote(zoekterm)}"
    return mawaqit_request(url)


def mawaqit_zoek_locatie(lat, lon):
    """Zoek moskeeën in de buurt op basis van coördinaten."""
    url = f"{MAWAQIT_API}/mosque/search?lat={lat}&lon={lon}"
    return mawaqit_request(url)


# ─── Zonnestand berekeningen ─────────────────────────────────────

def bereken_tijd_voor_hoek(locatie_info, datum, hoek, na_middag=False, tz=None):
    tz = tz or ZoneInfo(locatie_info.timezone)
    observer = locatie_info.observer
    from astral.sun import noon as solar_noon
    middag = solar_noon(observer, date=datum, tzinfo=tz)

    if na_middag:
        start = middag
        eind = datetime.combine(datum + timedelta(days=1), datetime.min.time()).replace(tzinfo=tz)
    else:
        start = datetime.combine(datum, datetime.min.time()).replace(tzinfo=tz)
        eind = middag

    for _ in range(50):
        midden = start + (eind - start) / 2
        elev = elevation(observer, midden)
        if na_middag:
            if elev > -hoek:
                start = midden
            else:
                eind = midden
        else:
            if elev < -hoek:
                start = midden
            else:
                eind = midden
    return midden


def bereken_gebedstijden(lat, lon, datum, tijdzone_str, methode_naam="MWL"):
    methode = BEREKENINGS_METHODES[methode_naam]
    tz = ZoneInfo(tijdzone_str)
    locatie = LocationInfo("Locatie", "NL", tijdzone_str, lat, lon)
    zon = sun(locatie.observer, date=datum, tzinfo=tz)
    solar_noon_time = noon(locatie.observer, date=datum, tzinfo=tz)

    fajr = bereken_tijd_voor_hoek(locatie, datum, methode["fajr_hoek"], na_middag=False, tz=tz)
    shurooq = zon["sunrise"]
    dhuhr = solar_noon_time + timedelta(minutes=2)

    # Asr (Shafi'i)
    dhuhr_elevatie = elevation(locatie.observer, solar_noon_time)
    zenith_rad = math.radians(90 - dhuhr_elevatie)
    asr_elevatie = math.degrees(math.atan(1.0 / (1 + math.tan(zenith_rad))))
    observer = locatie.observer
    start = solar_noon_time
    eind = zon["sunset"]
    for _ in range(50):
        midden = start + (eind - start) / 2
        elev = elevation(observer, midden)
        if elev > asr_elevatie:
            start = midden
        else:
            eind = midden
    asr = midden

    maghrib = zon["sunset"]

    if methode["isha_hoek"] is None:
        isha = maghrib + timedelta(minutes=90)
    else:
        isha = bereken_tijd_voor_hoek(locatie, datum, methode["isha_hoek"], na_middag=True, tz=tz)

    return {
        "fajr": fajr.strftime("%H:%M"),
        "shurooq": shurooq.strftime("%H:%M"),
        "dhuhr": dhuhr.strftime("%H:%M"),
        "asr": asr.strftime("%H:%M"),
        "maghrib": maghrib.strftime("%H:%M"),
        "isha": isha.strftime("%H:%M"),
    }


def zonnestand_info(lat, lon, datum, tijdzone_str, methode_naam="MWL"):
    tz = ZoneInfo(tijdzone_str)
    locatie = LocationInfo("Locatie", "NL", tijdzone_str, lat, lon)
    observer = locatie.observer
    zon = sun(observer, date=datum, tzinfo=tz)
    daglicht = zon["sunset"] - zon["sunrise"]
    methode = BEREKENINGS_METHODES[methode_naam]

    # Zonshoogte op elk moment van de dag (voor de grafiek)
    zonnepad = []
    start_dt = datetime.combine(datum, datetime.min.time()).replace(tzinfo=tz)
    for minuut in range(0, 24 * 60, 10):  # elke 10 minuten
        t = start_dt + timedelta(minutes=minuut)
        elev = elevation(observer, t)
        zonnepad.append({
            "tijd": t.strftime("%H:%M"),
            "hoogte": round(elev, 2),
        })

    # Hoogte bij dhuhr (hoogste punt)
    noon_time = noon(observer, date=datum, tzinfo=tz)
    max_hoogte = round(elevation(observer, noon_time), 1)

    # Zonshoogte bij elk gebedstijd-moment
    gebedstijd_hoeken = []

    # Fajr
    fajr_hoek = methode["fajr_hoek"]
    gebedstijd_hoeken.append({
        "naam": "Fajr", "icoon": "🌙",
        "hoek": round(-fajr_hoek, 1),
        "uitleg": f"Zon op -{fajr_hoek}° onder de horizon (dageraad)",
        "tijd": zon["dawn"].strftime("%H:%M") if "dawn" in zon else "—",
    })

    # Shurooq
    gebedstijd_hoeken.append({
        "naam": "Shurooq", "icoon": "🌅",
        "hoek": 0,
        "uitleg": "Zon op 0° — verschijnt boven de horizon",
        "tijd": zon["sunrise"].strftime("%H:%M"),
    })

    # Dhuhr
    gebedstijd_hoeken.append({
        "naam": "Dhuhr", "icoon": "☀️",
        "hoek": max_hoogte,
        "uitleg": f"Zon op hoogste punt: {max_hoogte}°",
        "tijd": noon_time.strftime("%H:%M"),
    })

    # Asr
    dhuhr_elevatie = elevation(observer, noon_time)
    zenith_rad = math.radians(90 - dhuhr_elevatie)
    asr_elevatie = round(math.degrees(math.atan(1.0 / (1 + math.tan(zenith_rad)))), 1)
    gebedstijd_hoeken.append({
        "naam": "Asr", "icoon": "🌤️",
        "hoek": asr_elevatie,
        "uitleg": f"Zon op {asr_elevatie}° — schaduw = voorwerp + middagschaduw",
        "tijd": "",
    })

    # Maghrib
    gebedstijd_hoeken.append({
        "naam": "Maghrib", "icoon": "🌇",
        "hoek": 0,
        "uitleg": "Zon op 0° — verdwijnt onder de horizon",
        "tijd": zon["sunset"].strftime("%H:%M"),
    })

    # Isha
    isha_hoek = methode["isha_hoek"]
    if isha_hoek:
        gebedstijd_hoeken.append({
            "naam": "Isha", "icoon": "🌃",
            "hoek": round(-isha_hoek, 1),
            "uitleg": f"Zon op -{isha_hoek}° onder de horizon (duisternis)",
            "tijd": "",
        })
    else:
        gebedstijd_hoeken.append({
            "naam": "Isha", "icoon": "🌃",
            "hoek": None,
            "uitleg": "90 minuten na Maghrib (Makkah methode)",
            "tijd": "",
        })

    # Astronomische zonnestanden berekenen
    # Civil twilight: -6°, Nautical: -12°, Astronomical: -18°
    zonnestanden = []

    twilight_hoeken = [
        (-18, "Astronomische schemering", "Zon -18° — volledige duisternis eindigt / begint"),
        (-12, "Nautische schemering", "Zon -12° — horizon wordt zichtbaar op zee"),
        (-6, "Burgerlijke schemering", "Zon -6° — je kunt buiten lezen"),
        (0, "Zonsopkomst", "Zon 0° — bovenrand raakt de horizon"),
    ]

    # Ochtend schemeringen
    for hoek, naam, uitleg in twilight_hoeken:
        if hoek < 0:
            t = bereken_tijd_voor_hoek(locatie, datum, -hoek, na_middag=False, tz=tz)
        else:
            t = zon["sunrise"]
        zonnestanden.append({
            "naam": f"{naam} (ochtend)",
            "tijd": t.strftime("%H:%M"),
            "hoek": hoek,
            "uitleg": uitleg,
            "fase": "ochtend",
        })

    # Zonne-middag
    zonnestanden.append({
        "naam": "Zonne-middag (hoogste punt)",
        "tijd": noon_time.strftime("%H:%M"),
        "hoek": max_hoogte,
        "uitleg": f"Zon op hoogste punt: {max_hoogte}°",
        "fase": "middag",
    })

    # Avond schemeringen (omgekeerde volgorde)
    twilight_avond = list(reversed(twilight_hoeken))
    for hoek, naam, uitleg in twilight_avond:
        if hoek < 0:
            t = bereken_tijd_voor_hoek(locatie, datum, -hoek, na_middag=True, tz=tz)
        else:
            t = zon["sunset"]
        zonnestanden.append({
            "naam": f"{naam} (avond)",
            "tijd": t.strftime("%H:%M"),
            "hoek": hoek,
            "uitleg": uitleg,
            "fase": "avond",
        })

    return {
        "zonsopkomst": zon["sunrise"].strftime("%H:%M"),
        "zonne_middag": noon_time.strftime("%H:%M"),
        "zonsondergang": zon["sunset"].strftime("%H:%M"),
        "daglicht": str(daglicht).split(".")[0],
        "max_hoogte": max_hoogte,
        "zonnepad": zonnepad,
        "gebedstijd_hoeken": gebedstijd_hoeken,
        "zonnestanden": zonnestanden,
    }


# ─── Config ─────────────────────────────────────────────────────

def laad_config():
    if CONFIG_BESTAND.exists():
        with open(CONFIG_BESTAND) as f:
            return json.load(f)
    return None


def sla_config_op(data):
    with open(CONFIG_BESTAND, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ─── Hulpfuncties ───────────────────────────────────────────────

def verschil_min(t1_str, t2_str):
    t1 = datetime.strptime(t1_str, "%H:%M")
    t2 = datetime.strptime(t2_str, "%H:%M")
    return round((t1 - t2).total_seconds() / 60)


def vergelijk_tijden(moskee_tijden, berekende_tijden, iqama_data=None):
    gebeden = ["fajr", "shurooq", "dhuhr", "asr", "maghrib", "isha"]
    namen = {"fajr": "Fajr", "shurooq": "Shurooq", "dhuhr": "Dhuhr",
             "asr": "Asr", "maghrib": "Maghrib", "isha": "Isha"}
    iconen = {"fajr": "🌙", "shurooq": "🌅", "dhuhr": "☀️",
              "asr": "🌤️", "maghrib": "🌇", "isha": "🌃"}
    if iqama_data is None:
        iqama_data = {}

    resultaten = []
    for key in gebeden:
        moskee = moskee_tijden.get(key, "—")
        berekend = berekende_tijden.get(key, "—")
        iqama = iqama_data.get(key, "—")
        if moskee != "—" and berekend != "—":
            verschil = verschil_min(moskee, berekend)
            if abs(verschil) <= MAX_AFWIJKING_MINUTEN:
                status = "ok"
            elif abs(verschil) <= MAX_AFWIJKING_MINUTEN * 2:
                status = "warning"
            else:
                status = "error"
        else:
            verschil = 0
            status = "ok"

        resultaten.append({
            "naam": namen[key],
            "icoon": iconen[key],
            "key": key,
            "moskee": moskee,
            "iqama": iqama,
            "berekend": berekend,
            "verschil": verschil,
            "status": status,
        })
    return resultaten


# ─── Routes ──────────────────────────────────────────────────────

@app.route("/")
def index():
    config = laad_config()
    return render_template("index.html", config=config)


@app.route("/api/zoek")
def api_zoek():
    zoekterm = request.args.get("q", "")
    if not zoekterm:
        return jsonify([])
    try:
        resultaten = mawaqit_zoek(zoekterm)
        moskees = []
        for m in resultaten:
            moskees.append({
                "naam": m.get("name", "Onbekend"),
                "adres": m.get("localisation", ""),
                "uuid": m.get("uuid", ""),
                "slug": m.get("slug", ""),
                "lat": m.get("latitude", 0),
                "lon": m.get("longitude", 0),
            })
        return jsonify(moskees)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/dichtbij")
def api_dichtbij():
    """Zoek moskeeën in de buurt op basis van GPS coördinaten."""
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    if lat is None or lon is None:
        return jsonify({"error": "lat en lon zijn verplicht"}), 400
    try:
        resultaten = mawaqit_zoek_locatie(lat, lon)
        moskees = []
        for m in resultaten:
            moskees.append({
                "naam": m.get("name", "Onbekend"),
                "adres": m.get("localisation", ""),
                "uuid": m.get("uuid", ""),
                "slug": m.get("slug", ""),
                "lat": m.get("latitude", 0),
                "lon": m.get("longitude", 0),
            })
        return jsonify(moskees)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/berekenen", methods=["POST"])
def api_berekenen():
    """Bereken astronomische tijden en vergelijk met meegegeven moskee-tijden."""
    config = laad_config()
    if not config:
        return jsonify({"error": "Geen moskee geselecteerd"}), 400

    data = request.json
    moskee_tijden = data.get("moskee_tijden", {})

    tz_str = config.get("tijdzone", "Europe/Amsterdam")
    tz = ZoneInfo(tz_str)
    vandaag = datetime.now(tz).date()
    methode = config.get("methode", "MWL")

    berekend = bereken_gebedstijden(
        config["latitude"], config["longitude"], vandaag, tz_str, methode
    )
    vergelijking = vergelijk_tijden(moskee_tijden, berekend)
    zon = zonnestand_info(config["latitude"], config["longitude"], vandaag, tz_str, methode)

    return jsonify({
        "moskee": config["moskee_naam"],
        "datum": vandaag.strftime("%A %d %B %Y"),
        "methode": methode,
        "vergelijking": vergelijking,
        "zonnestand": zon,
    })


@app.route("/api/kies", methods=["POST"])
def api_kies():
    data = request.json
    config = {
        "moskee_naam": data["naam"],
        "uuid": data["uuid"],
        "slug": data["slug"],
        "latitude": data["lat"],
        "longitude": data["lon"],
        "tijdzone": "Europe/Amsterdam",
        "methode": "MWL",
    }
    sla_config_op(config)
    return jsonify({"ok": True})


@app.route("/api/moskee_tijden")
def api_moskee_tijden():
    """Haal Mawaqit tijden op voor een specifieke moskee (voor vergelijking)."""
    slug = request.args.get("slug", "")
    if not slug:
        return jsonify({"error": "slug is verplicht"}), 400
    try:
        resultaten = mawaqit_zoek(slug)
        if not resultaten:
            return jsonify({"error": "Moskee niet gevonden"}), 404
        m = resultaten[0]
        times = m.get("times", [])
        naam = m.get("name", "Onbekend")
        tijden = {
            "fajr": times[0] if len(times) > 0 else "—",
            "shurooq": times[1] if len(times) > 1 else "—",
            "dhuhr": times[2] if len(times) > 2 else "—",
            "asr": times[3] if len(times) > 3 else "—",
            "maghrib": times[4] if len(times) > 4 else "—",
            "isha": times[5] if len(times) > 5 else "—",
        }
        return jsonify({"naam": naam, "tijden": tijden})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tijden")
def api_tijden():
    config = laad_config()
    if not config:
        return jsonify({"error": "Geen moskee geselecteerd"}), 400

    tz_str = config.get("tijdzone", "Europe/Amsterdam")
    tz = ZoneInfo(tz_str)
    vandaag = datetime.now(tz).date()
    methode = config.get("methode", "MWL")

    # Haal Mawaqit tijden op
    try:
        resultaten = mawaqit_zoek(config["slug"])
        if not resultaten:
            return jsonify({"error": "Moskee niet gevonden"}), 404
        m = resultaten[0]
        times = m.get("times", [])
        iqama = m.get("iqama", [])
        moskee_tijden = {
            "fajr": times[0], "shurooq": times[1], "dhuhr": times[2],
            "asr": times[3], "maghrib": times[4], "isha": times[5],
        }
    except Exception as e:
        return jsonify({"error": f"Mawaqit fout: {e}"}), 500

    # Bereken astronomische tijden
    berekend = bereken_gebedstijden(
        config["latitude"], config["longitude"], vandaag, tz_str, methode
    )

    # Iqama info
    iqama_data = {}
    iqama_keys = ["fajr", "shurooq", "dhuhr", "asr", "maghrib", "isha"]
    for i, key in enumerate(iqama_keys):
        if i < len(iqama):
            val = iqama[i]
            # Mawaqit kan iqama als offset (int minuten) of als tijdstring geven
            if isinstance(val, int) and val > 0:
                # Offset in minuten na adhan
                adhan_str = moskee_tijden.get(key, "")
                if adhan_str:
                    adhan_dt = datetime.strptime(adhan_str, "%H:%M")
                    iqama_dt = adhan_dt + timedelta(minutes=val)
                    iqama_data[key] = iqama_dt.strftime("%H:%M")
            elif isinstance(val, str) and val:
                # String offset zoals "+10" → bereken echte tijd
                stripped = val.strip().lstrip("+")
                if stripped.isdigit():
                    offset_min = int(stripped)
                    adhan_str = moskee_tijden.get(key, "")
                    if adhan_str:
                        adhan_dt = datetime.strptime(adhan_str, "%H:%M")
                        iqama_dt = adhan_dt + timedelta(minutes=offset_min)
                        iqama_data[key] = iqama_dt.strftime("%H:%M")
                elif ":" in val:
                    # Al een echte tijd zoals "13:02"
                    iqama_data[key] = val

    # Vergelijk (met iqama)
    vergelijking = vergelijk_tijden(moskee_tijden, berekend, iqama_data)

    # Zonnestand
    zon = zonnestand_info(config["latitude"], config["longitude"], vandaag, tz_str, methode)

    # Hijri datum
    datum_hijri = gregorian_naar_hijri_str(vandaag.year, vandaag.month, vandaag.day)

    # Qibla hoek (Feature 1)
    qibla = bereken_qibla(config["latitude"], config["longitude"])
    qibla_tekst = qibla_richting_tekst(qibla)

    # Ramadan detectie (Feature 5)
    h_dag, h_maand, h_jaar, _ = gregorian_naar_hijri(vandaag.year, vandaag.month, vandaag.day)
    is_ramadan = (h_maand == 9)
    ramadan_dag = h_dag if is_ramadan else None

    return jsonify({
        "moskee": config["moskee_naam"],
        "datum": vandaag.strftime("%A %d %B %Y"),
        "datum_hijri": datum_hijri,
        "methode": methode,
        "vergelijking": vergelijking,
        "zonnestand": zon,
        "iqama": iqama_data,
        "qibla_hoek": round(qibla, 1),
        "qibla_richting": qibla_tekst,
        "is_ramadan": is_ramadan,
        "ramadan_dag": ramadan_dag,
    })


@app.route("/api/week")
def api_week():
    """Geeft gebedstijden voor de komende 7 dagen (berekend)."""
    config = laad_config()
    if not config:
        return jsonify({"error": "Geen moskee geselecteerd"}), 400

    tz_str = config.get("tijdzone", "Europe/Amsterdam")
    tz = ZoneInfo(tz_str)
    vandaag = datetime.now(tz).date()
    methode = config.get("methode", "MWL")

    dag_namen = {
        "Monday": "Maandag", "Tuesday": "Dinsdag", "Wednesday": "Woensdag",
        "Thursday": "Donderdag", "Friday": "Vrijdag", "Saturday": "Zaterdag",
        "Sunday": "Zondag",
    }

    dagen = []
    for i in range(7):
        datum = vandaag + timedelta(days=i)
        tijden = bereken_gebedstijden(
            config["latitude"], config["longitude"], datum, tz_str, methode
        )
        eng_dag = datum.strftime("%A")
        nl_dag = dag_namen.get(eng_dag, eng_dag)
        dagen.append({
            "datum": datum.strftime("%d-%m"),
            "dag": nl_dag,
            "dag_kort": nl_dag[:2],
            "is_vandaag": (i == 0),
            "tijden": tijden,
        })

    return jsonify({"dagen": dagen, "methode": methode})


if __name__ == "__main__":
    app.run(debug=True, port=5050, host="0.0.0.0")
