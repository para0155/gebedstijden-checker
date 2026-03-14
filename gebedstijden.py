#!/usr/bin/env python3
"""
Gebedstijden Checker
Haalt moskee-tijden op via Mawaqit en vergelijkt ze met
astronomisch berekende tijden op basis van de zonnestand.
"""

import json
import math
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from astral import LocationInfo
from astral.sun import sun, elevation, noon
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt

# ─── Configuratie ────────────────────────────────────────────────

CONFIG_BESTAND = Path(__file__).parent / "config.json"

MAX_AFWIJKING_MINUTEN = 5

BEREKENINGS_METHODES = {
    "MWL":     {"fajr_hoek": 18.0, "isha_hoek": 17.0},
    "ISNA":    {"fajr_hoek": 15.0, "isha_hoek": 15.0},
    "Egypt":   {"fajr_hoek": 19.5, "isha_hoek": 17.5},
    "Makkah":  {"fajr_hoek": 18.5, "isha_hoek": None},
    "Diyanet": {"fajr_hoek": 18.0, "isha_hoek": 17.0},
}

METHODE = "MWL"

console = Console()

MAWAQIT_API = "https://mawaqit.net/api/2.0"


# ─── Mawaqit API ────────────────────────────────────────────────

def mawaqit_request(url):
    """Doe een request naar de Mawaqit API."""
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def mawaqit_zoek(zoekterm):
    """Zoek moskeeën op Mawaqit."""
    url = f"{MAWAQIT_API}/mosque/search?word={urllib.parse.quote(zoekterm)}"
    return mawaqit_request(url)


def mawaqit_haal_tijden(uuid):
    """Haal gebedstijden op voor een specifieke moskee via UUID."""
    url = f"{MAWAQIT_API}/mosque/{uuid}/prayer-times"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def kies_moskee(zoekterm):
    """Zoek en laat de gebruiker een moskee kiezen."""
    console.print(f"\n🔍 Zoeken naar '[bold]{zoekterm}[/bold]' op Mawaqit...")
    resultaten = mawaqit_zoek(zoekterm)

    if not resultaten:
        console.print("[red]Geen moskeeën gevonden![/red]")
        return None

    console.print(f"\n[bold]Gevonden moskeeën ({len(resultaten)}):[/bold]\n")
    for i, moskee in enumerate(resultaten, 1):
        naam = moskee.get("name", "Onbekend")
        locatie = moskee.get("localisation", "")
        console.print(f"  [cyan]{i}.[/cyan] {naam}")
        if locatie:
            console.print(f"     📍 {locatie}")

    keuze = Prompt.ask(
        "\nKies een moskee (nummer)",
        default="1",
    )
    idx = int(keuze) - 1
    if 0 <= idx < len(resultaten):
        return resultaten[idx]

    console.print("[red]Ongeldige keuze![/red]")
    return None


def parse_mawaqit_moskee(moskee_data):
    """Parse Mawaqit moskee-data naar ons formaat."""
    times = moskee_data.get("times", [])
    if len(times) < 6:
        return None

    return {
        "naam": moskee_data.get("name", "Onbekend"),
        "uuid": moskee_data.get("uuid", ""),
        "slug": moskee_data.get("slug", ""),
        "latitude": moskee_data.get("latitude", 0),
        "longitude": moskee_data.get("longitude", 0),
        "tijden": {
            "fajr": times[0],
            "shurooq": times[1],
            "dhuhr": times[2],
            "asr": times[3],
            "maghrib": times[4],
            "isha": times[5],
        },
        "iqama": moskee_data.get("iqama", []),
    }


def sla_config_op(moskee_info, tijdzone="Europe/Amsterdam"):
    """Sla de geselecteerde moskee op in config."""
    config = {
        "moskee_naam": moskee_info["naam"],
        "uuid": moskee_info["uuid"],
        "slug": moskee_info["slug"],
        "latitude": moskee_info["latitude"],
        "longitude": moskee_info["longitude"],
        "tijdzone": tijdzone,
        "methode": METHODE,
    }
    with open(CONFIG_BESTAND, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    console.print(f"\n[green]✓ Moskee opgeslagen in config.json[/green]")
    return config


def laad_config():
    """Laad opgeslagen configuratie."""
    if CONFIG_BESTAND.exists():
        with open(CONFIG_BESTAND) as f:
            return json.load(f)
    return None


# ─── Zonnestand berekeningen ─────────────────────────────────────

def bereken_tijd_voor_hoek(locatie_info, datum, hoek, na_middag=False, tz=None):
    """Bereken het tijdstip waarop de zon een bepaalde depressie-hoek bereikt."""
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
    """Bereken alle gebedstijden op basis van zonnestand."""
    methode = BEREKENINGS_METHODES[methode_naam]
    tz = ZoneInfo(tijdzone_str)

    locatie = LocationInfo("Locatie", "NL", tijdzone_str, lat, lon)
    zon = sun(locatie.observer, date=datum, tzinfo=tz)
    solar_noon_time = noon(locatie.observer, date=datum, tzinfo=tz)

    fajr = bereken_tijd_voor_hoek(locatie, datum, methode["fajr_hoek"], na_middag=False, tz=tz)
    shurooq = zon["sunrise"]
    dhuhr = solar_noon_time + timedelta(minutes=2)

    # Asr (Shafi'i methode)
    dhuhr_elevatie = elevation(locatie.observer, solar_noon_time)
    schaduw_factor = 1
    zenith_rad = math.radians(90 - dhuhr_elevatie)
    asr_elevatie = math.degrees(
        math.atan(1.0 / (schaduw_factor + math.tan(zenith_rad)))
    )
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
        "fajr": fajr,
        "shurooq": shurooq,
        "dhuhr": dhuhr,
        "asr": asr,
        "maghrib": maghrib,
        "isha": isha,
    }


# ─── Vergelijking & Weergave ────────────────────────────────────

def parse_tijd(tijd_str, datum, tz):
    """Parse een tijdstring (HH:MM) naar datetime."""
    uur, minuut = map(int, tijd_str.split(":"))
    return datetime(datum.year, datum.month, datum.day, uur, minuut, tzinfo=tz)


def verschil_minuten(t1, t2):
    """Bereken het verschil in minuten tussen twee tijden."""
    delta = (t1 - t2).total_seconds() / 60
    return round(delta, 1)


def maak_overzicht(moskee_tijden, berekende_tijden, datum, max_afwijking, iqama=None):
    """Maak een vergelijkingsoverzicht met waarschuwingen."""
    gebeden = [
        ("Fajr", "fajr", "🌙", 0),
        ("Shurooq", "shurooq", "🌅", 1),
        ("Dhuhr", "dhuhr", "☀️", 2),
        ("Asr", "asr", "🌤️", 3),
        ("Maghrib", "maghrib", "🌇", 4),
        ("Isha", "isha", "🌃", None),
    ]

    table = Table(
        title=f"Gebedstijden Vergelijking — {datum.strftime('%A %d %B %Y')}",
        show_header=True,
        header_style="bold cyan",
        border_style="blue",
        pad_edge=True,
    )

    table.add_column("Gebed", style="bold", width=12)
    table.add_column("Moskee", justify="center", width=10)
    table.add_column("Iqama", justify="center", width=10)
    table.add_column("Berekend", justify="center", width=10)
    table.add_column("Verschil", justify="center", width=12)
    table.add_column("Status", justify="center", width=8)

    waarschuwingen = []

    for naam, key, emoji, iqama_idx in gebeden:
        moskee_str = moskee_tijden.get(key, "—")
        berekend_dt = berekende_tijden[key]
        berekend_str = berekend_dt.strftime("%H:%M")

        # Iqama tijd berekenen
        iqama_str = "—"
        if iqama and iqama_idx is not None and iqama_idx < len(iqama):
            iq = iqama[iqama_idx]
            if isinstance(iq, str) and iq.startswith("+"):
                try:
                    minuten = int(iq.replace("+", ""))
                    iqama_str = f"{moskee_str} +{minuten}"
                except ValueError:
                    iqama_str = iq
            elif isinstance(iq, str) and ":" in iq:
                iqama_str = iq

        if moskee_str == "—":
            verschil_str = "—"
            status = "—"
        else:
            tz = berekend_dt.tzinfo
            moskee_dt = parse_tijd(moskee_str, datum, tz)
            verschil = verschil_minuten(moskee_dt, berekend_dt)
            verschil_str = f"{verschil:+.0f} min"

            if abs(verschil) <= max_afwijking:
                status = "[green]✓[/green]"
            elif abs(verschil) <= max_afwijking * 2:
                status = "[yellow]⚠[/yellow]"
                waarschuwingen.append(
                    f"[yellow]⚠ {naam}: {abs(verschil):.0f} min afwijking "
                    f"(moskee {moskee_str}, berekend {berekend_str})[/yellow]"
                )
            else:
                status = "[red]✗[/red]"
                waarschuwingen.append(
                    f"[red]✗ {naam}: {abs(verschil):.0f} min afwijking! "
                    f"(moskee {moskee_str}, berekend {berekend_str})[/red]"
                )

        table.add_row(
            f"{emoji} {naam}",
            moskee_str,
            iqama_str,
            berekend_str,
            verschil_str,
            status,
        )

    return table, waarschuwingen


def main():
    console.print()
    console.print(
        Panel(
            "[bold cyan]Gebedstijden Checker[/bold cyan]\n"
            "Haalt tijden op van Mawaqit & vergelijkt met zonnestand",
            border_style="cyan",
        )
    )

    config = laad_config()

    # Eerste keer of --zoek flag: kies een moskee
    if config is None or "--zoek" in sys.argv:
        if len(sys.argv) > 1 and sys.argv[1] != "--zoek":
            zoekterm = sys.argv[1]
        else:
            zoekterm = Prompt.ask("🔍 Zoek een moskee (stad of naam)")

        moskee = kies_moskee(zoekterm)
        if not moskee:
            return

        moskee_info = parse_mawaqit_moskee(moskee)
        if not moskee_info:
            console.print("[red]Kon tijden niet laden![/red]")
            return

        tz = Prompt.ask("⏰ Tijdzone", default="Europe/Amsterdam")
        config = sla_config_op(moskee_info, tz)

        # Toon direct de tijden van de net gekozen moskee
        moskee_tijden = moskee_info["tijden"]
        iqama = moskee_info["iqama"]
    else:
        # Haal verse tijden op van Mawaqit
        console.print(f"\n🕌 [bold]{config['moskee_naam']}[/bold]")
        console.print(f"📡 Tijden ophalen van Mawaqit...")

        try:
            resultaten = mawaqit_zoek(config["slug"])
            if resultaten:
                moskee_data = resultaten[0]
                moskee_info = parse_mawaqit_moskee(moskee_data)
                moskee_tijden = moskee_info["tijden"]
                iqama = moskee_info["iqama"]
            else:
                console.print("[red]Kon moskee niet vinden op Mawaqit![/red]")
                return
        except Exception as e:
            console.print(f"[red]Fout bij ophalen: {e}[/red]")
            return

    lat = config["latitude"]
    lon = config["longitude"]
    tz_str = config["tijdzone"]
    tz = ZoneInfo(tz_str)
    methode = config.get("methode", METHODE)

    console.print(f"📍 Coördinaten: {lat}°N, {lon}°E")
    console.print(f"📊 Berekeningsmethode: [bold]{methode}[/bold]")
    console.print(f"⏱️  Max afwijking: [bold]{MAX_AFWIJKING_MINUTEN} minuten[/bold]")

    vandaag = datetime.now(tz).date()

    # Bereken tijden op basis van zonnestand
    berekend = bereken_gebedstijden(lat, lon, vandaag, tz_str, methode)

    # Maak overzicht
    tabel, waarschuwingen = maak_overzicht(
        moskee_tijden, berekend, vandaag, MAX_AFWIJKING_MINUTEN, iqama
    )

    console.print()
    console.print(tabel)

    # Toon waarschuwingen
    if waarschuwingen:
        console.print()
        console.print(
            Panel(
                "\n".join(waarschuwingen),
                title="[bold red]Waarschuwingen[/bold red]",
                border_style="red",
            )
        )
    else:
        console.print()
        console.print(
            Panel(
                "[bold green]Alle tijden komen overeen met de berekeningen! ✓[/bold green]",
                border_style="green",
            )
        )

    # Zonnestand info
    locatie = LocationInfo("Locatie", "NL", tz_str, lat, lon)
    zon = sun(locatie.observer, date=vandaag, tzinfo=tz)

    console.print()
    console.print(
        Panel(
            f"🌅 Zonsopkomst:    {zon['sunrise'].strftime('%H:%M')}\n"
            f"☀️  Zonne-middag:   {zon['noon'].strftime('%H:%M')}\n"
            f"🌇 Zonsondergang:  {zon['sunset'].strftime('%H:%M')}\n"
            f"🌑 Duur daglicht:  {str(zon['sunset'] - zon['sunrise']).split('.')[0]}",
            title=f"[bold]Zonnestand {vandaag.strftime('%d-%m-%Y')}[/bold]",
            border_style="yellow",
        )
    )

    console.print()
    console.print(
        "[dim]Tip: gebruik [bold]--zoek[/bold] om een andere moskee te kiezen[/dim]"
    )
    console.print()


if __name__ == "__main__":
    main()
