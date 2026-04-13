import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from threading import Thread, Lock

import requests
import websockets
from flask import Flask, jsonify

# ─── Config ───────────────────────────────────────────────────────────────────
MMSI        = "247389200"           # AIDAnova
PERSON_NAME = os.getenv("PERSON_NAME", "Lara")
API_KEY     = os.getenv("AISSTREAM_API_KEY", "").strip()

# ─── App setup ────────────────────────────────────────────────────────────────
app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)

# ─── Shared state (updated by background WebSocket worker) ────────────────────
ship: dict = {
    "lat":          None,
    "lon":          None,
    "speed":        None,
    "nav_status":   15,     # 15 = undefined
    "destination":  None,
    "eta_dt":       None,
    "location_text": None,
    "last_ais":     0,
    "last_geocode": 0,
}
lock    = Lock()
bg_loop = asyncio.new_event_loop()

# ─── Helpers ──────────────────────────────────────────────────────────────────

# ─── Port code & country lookup ───────────────────────────────────────────────

PORT_CODES: dict[str, str] = {
    # Norwegen
    "NOSVG": "Stavanger", "NOBGO": "Bergen", "NOOSL": "Oslo",
    "NOKRS": "Kristiansand", "NOAES": "Ålesund", "NOTRD": "Trondheim",
    "NOTOS": "Tromsø", "NOHAU": "Haugesund", "NOMOL": "Molde",
    "NOFUS": "Flåm", "NOGEI": "Geiranger", "NOLES": "Leknes",
    "NOSVV": "Svolvær", "NOHON": "Honningsvåg", "NOALK": "Alta",
    "NOBOD": "Bodø", "NONVK": "Narvik", "NOHRD": "Harstad",
    "NOOND": "Andenes", "NORRS": "Åndalsnes",
    # Deutschland
    "DEHAM": "Hamburg", "DEKIE": "Kiel", "DEBRV": "Bremerhaven",
    "DEROS": "Rostock", "DEWIS": "Wismar",
    # Dänemark
    "DKAAR": "Aarhus", "DKALS": "Aalborg", "DKCPH": "Kopenhagen",
    "DKFRC": "Fredericia",
    # Schweden
    "SEGOT": "Göteborg", "SESTO": "Stockholm", "SEAHU": "Ahus",
    # Großbritannien
    "GBSOU": "Southampton", "GBLIV": "Liverpool", "GBDVR": "Dover",
    # Niederlande
    "NLRTM": "Rotterdam", "NLAMS": "Amsterdam",
    # Belgien
    "BEANR": "Antwerpen",
    # Spanien
    "ESBCN": "Barcelona", "ESLPA": "Las Palmas", "ESSPC": "Santa Cruz de Tenerife",
    # Portugal
    "PTFNC": "Funchal",
    # Italien
    "ITCIV": "Civitavecchia", "ITGOA": "Genua", "ITNAP": "Neapel",
    # Frankreich
    "FRMRS": "Marseille",
    # Kreuzfahrt-Abkürzungen
    "NOSVG ": "Stavanger",
}

COUNTRY_DE: dict[str, str] = {
    "Norway": "Norwegen", "Norge": "Norwegen", "Germany": "Deutschland",
    "Denmark": "Dänemark", "Sweden": "Schweden", "Finland": "Finnland",
    "United Kingdom": "Großbritannien", "Netherlands": "Niederlande",
    "Belgium": "Belgien", "France": "Frankreich", "Spain": "Spanien",
    "Portugal": "Portugal", "Italy": "Italien", "Iceland": "Island",
    "Faroe Islands": "Färöer-Inseln", "Greenland": "Grönland",
    "Estonia": "Estland", "Latvia": "Lettland", "Lithuania": "Litauen",
    "Poland": "Polen",
}


def decode_port(raw: str) -> str:
    """Convert AIS destination code or raw string to a readable port name."""
    if not raw:
        return ""
    cleaned = raw.strip().upper()
    # Try lookup by full code
    if cleaned in PORT_CODES:
        return PORT_CODES[cleaned]
    # Try 5-char LOCODE (e.g. "NOSVG" from "NOSVG  ")
    for code, name in PORT_CODES.items():
        if cleaned.startswith(code.strip()):
            return name
    # Return title-cased original if not found
    return raw.strip().title()


def country_de(name: str) -> str:
    return COUNTRY_DE.get(name, name)


def reverse_geocode(lat: float, lon: float) -> str | None:
    """Resolve coordinates to a human-readable place name via Nominatim."""
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "json", "zoom": 8},
            headers={"User-Agent": "AIDAnova-HA-Tracker/1.0 (HomeAssistant integration)"},
            timeout=6,
        )
        data = r.json()
        addr = data.get("address", {})
        city = (
            addr.get("city")
            or addr.get("town")
            or addr.get("village")
            or addr.get("municipality")
        )
        country_raw = addr.get("country", "")
        country = country_de(country_raw)
        if city:
            return f"{city}, {country}" if country else city
        # Fallback: state/county + country
        region = addr.get("county") or addr.get("state")
        if region:
            return f"{region}, {country}" if country else region
        display = data.get("display_name", "")
        return display.split(",")[0].strip() if display else None
    except Exception as exc:
        logging.warning("Geocode error: %s", exc)
        return None


def parse_eta(eta_obj: dict | None) -> datetime | None:
    """Parse an AIS ETA object {Month, Day, Hour, Minute} into a UTC datetime."""
    if not eta_obj:
        return None
    m  = eta_obj.get("Month",  0)
    d  = eta_obj.get("Day",    0)
    h  = eta_obj.get("Hour",  24)
    mn = eta_obj.get("Minute", 60)
    if not m or not d or h >= 24 or mn >= 60:
        return None
    now = datetime.now(timezone.utc)
    try:
        dt = datetime(now.year, m, d, h, mn, tzinfo=timezone.utc)
        # If ETA is more than 12 h in the past, assume next year
        if (now - dt).total_seconds() > 43_200:
            dt = datetime(now.year + 1, m, d, h, mn, tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def eta_text(eta_dt: datetime | None) -> str | None:
    """Return a German 'in X Stunden' string for the given ETA."""
    if not eta_dt:
        return None
    secs = (eta_dt - datetime.now(timezone.utc)).total_seconds()
    if secs < 0:
        return None
    if secs < 3_600:
        mins = int(secs / 60)
        return f"in etwa {mins} Minute{'n' if mins != 1 else ''}"
    if secs < 86_400:
        hours = int(secs / 3_600)
        return f"in etwa {hours} Stunde{'n' if hours != 1 else ''}"
    days = int(secs / 86_400)
    return f"in etwa {days} Tag{'en' if days != 1 else ''}"


# ─── Background tasks ─────────────────────────────────────────────────────────

async def _geocode_task(lat: float, lon: float) -> None:
    result = await bg_loop.run_in_executor(None, reverse_geocode, lat, lon)
    with lock:
        if result:
            ship["location_text"] = result
        ship["last_geocode"] = time.time()


def _process_msg(msg: dict) -> None:
    """Update shared ship state from one AIS message dict."""
    mtype = msg.get("MessageType")
    meta  = msg.get("MetaData", {})
    with lock:
        ship["last_ais"] = time.time()
        if mtype == "PositionReport":
            pos = msg["Message"]["PositionReport"]
            ship["lat"]        = meta.get("latitude")
            ship["lon"]        = meta.get("longitude")
            ship["speed"]      = round(pos.get("Sog", 0), 1)
            ship["nav_status"] = pos.get("NavigationalStatus", 15)
            if time.time() - ship["last_geocode"] > 600:
                asyncio.run_coroutine_threadsafe(
                    _geocode_task(ship["lat"], ship["lon"]), bg_loop
                )
        elif mtype == "ShipStaticData":
            static = msg["Message"]["ShipStaticData"]
            ship["destination"] = static.get("Destination", "").strip().title()
            ship["eta_dt"]      = parse_eta(static.get("Eta"))


async def ais_worker() -> None:
    """Maintain a persistent WebSocket connection to aisstream.io."""
    # Log key status once on startup
    if API_KEY:
        logging.info("API key loaded: length=%d, starts=%r, ends=%r",
                     len(API_KEY), API_KEY[:4], API_KEY[-4:])
    else:
        logging.error("API key is empty!")

    fail_count = 0
    while True:
        if not API_KEY:
            logging.error("AISSTREAM_API_KEY is not set – set it in docker-compose.yml")
            await asyncio.sleep(60)
            continue
        try:
            async with websockets.connect(
                "wss://stream.aisstream.io/v0/stream",
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                await ws.send(json.dumps({
                    "APIKey": API_KEY,
                    "BoundingBoxes": [
                        [[72.0, -5.0], [53.5, 32.0]],
                    ],
                    "FiltersShipMMSI":    [MMSI],
                    "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
                }))
                logging.info("Subscription gesendet – warte auf AIS-Daten für MMSI %s (%s)", MMSI, PERSON_NAME)
                fail_count = 0  # reset on successful connect

                async for raw in ws:
                    msg = json.loads(raw)
                    if "error" in msg:
                        logging.error("aisstream.io Fehler: %s", msg["error"])
                        break
                    _process_msg(msg)

        except websockets.exceptions.ConnectionClosedError as exc:
            fail_count += 1
            delay = min(30 * fail_count, 300)  # 30s, 60s, 90s … max 5 min
            logging.warning("WebSocket closed (code=%s reason=%s) – attempt %d, retry in %ds",
                            exc.rcvd.code if exc.rcvd else "?",
                            exc.rcvd.reason if exc.rcvd else "no close frame",
                            fail_count, delay)
            await asyncio.sleep(delay)
        except websockets.exceptions.ConnectionClosed as exc:
            fail_count += 1
            delay = min(30 * fail_count, 300)
            logging.warning("WebSocket closed: %s – attempt %d, retry in %ds", exc, fail_count, delay)
            await asyncio.sleep(delay)
        except asyncio.TimeoutError:
            fail_count += 1
            delay = min(30 * fail_count, 300)
            logging.warning("No response from aisstream.io within 5s – attempt %d, retry in %ds",
                            fail_count, delay)
            await asyncio.sleep(delay)
        except Exception as exc:
            fail_count += 1
            delay = min(30 * fail_count, 300)
            logging.error("WebSocket error: %s – attempt %d, retry in %ds", exc, fail_count, delay)
            await asyncio.sleep(delay)
        else:
            fail_count = 0  # reset on clean exit


# ─── REST endpoints ───────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    with lock:
        d = dict(ship)

    if not d["last_ais"]:
        return jsonify({
            "ok":      False,
            "message": f"Noch keine Schiffsdaten für {PERSON_NAME} verfügbar. "
                       "Der Service läuft – bitte kurz warten.",
        })

    age      = int(time.time() - d["last_ais"])
    name     = PERSON_NAME
    location = d["location_text"] or "unbekanntem Gebiet"
    speed    = d["speed"] or 0
    ns       = d["nav_status"]
    dest_raw = (d.get("destination") or "").strip()
    dest     = decode_port(dest_raw)
    eta_dt   = d.get("eta_dt")

    in_port = ns in (1, 5) or speed < 0.5

    # ── Hauptsatz ──────────────────────────────────────────────────────────
    if in_port:
        msg = f"{name} liegt gerade im Hafen von {location}"
    else:
        knots = f"{speed:.0f}" if speed == int(speed) else f"{speed:.1f}"
        msg = f"{name} ist auf See in der Nähe von {location} und fährt mit {knots} Knoten"

    # ── Nächster Hafen ─────────────────────────────────────────────────────
    if dest and eta_dt:
        t = eta_text(eta_dt)
        if t and in_port:
            msg += f". Nächster Halt: {dest} {t}"
        elif t and not in_port:
            msg += f". Das Schiff läuft {t} in {dest} ein"
    elif dest and not eta_dt:
        if in_port:
            msg += f". Nächster Halt: {dest}"
        else:
            msg += f". Ziel: {dest}"

    return jsonify({
        "ok":               True,
        "message":          msg,
        "location":         location,
        "lat":              d["lat"],
        "lon":              d["lon"],
        "speed_knots":      speed,
        "destination":      dest,
        "nav_status":       ns,
        "in_port":          in_port,
        "data_age_seconds": age,
        "data_fresh":       age < 3_600,
    })


@app.route("/health")
def health():
    with lock:
        last = ship["last_ais"]
    return jsonify({
        "status":          "ok",
        "last_ais_update": int(time.time() - last) if last else None,
    })


# ─── Entry point ──────────────────────────────────────────────────────────────

def _run_background():
    asyncio.set_event_loop(bg_loop)
    bg_loop.run_until_complete(ais_worker())


if __name__ == "__main__":
    Thread(target=_run_background, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
