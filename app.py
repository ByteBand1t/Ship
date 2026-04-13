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
API_KEY     = os.getenv("AISSTREAM_API_KEY", "")

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
            or addr.get("county")
            or addr.get("state")
        )
        country = addr.get("country", "")
        if city:
            return f"{city}, {country}" if country else city
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


async def ais_worker() -> None:
    """Maintain a persistent WebSocket connection to aisstream.io."""
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
                        [[25.0, -20.0], [72.0, 42.0]],   # Europe + Mediterranean + Norway
                        [[10.0, -90.0], [30.0, -55.0]],  # Caribbean
                    ],
                    "FiltersShipMMSI":    [MMSI],
                    "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
                }))
                logging.info("Connected to aisstream.io  |  tracking MMSI %s (%s)", MMSI, PERSON_NAME)

                async for raw in ws:
                    msg   = json.loads(raw)
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
                            # Trigger reverse geocode at most once per 10 minutes
                            if time.time() - ship["last_geocode"] > 600:
                                asyncio.create_task(
                                    _geocode_task(ship["lat"], ship["lon"])
                                )

                        elif mtype == "ShipStaticData":
                            static = msg["Message"]["ShipStaticData"]
                            ship["destination"] = static.get("Destination", "").strip().title()
                            ship["eta_dt"]      = parse_eta(static.get("Eta"))

        except websockets.exceptions.ConnectionClosedError as exc:
            logging.warning("WebSocket closed with error (code=%s reason=%s)  –  reconnecting in 15 s",
                            exc.rcvd.code if exc.rcvd else "?",
                            exc.rcvd.reason if exc.rcvd else "no close frame")
            await asyncio.sleep(15)
        except websockets.exceptions.ConnectionClosed as exc:
            logging.warning("WebSocket closed: %s  –  reconnecting in 15 s", exc)
            await asyncio.sleep(15)
        except Exception as exc:
            logging.error("WebSocket error: %s  –  reconnecting in 30 s", exc)
            await asyncio.sleep(30)


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
    dest     = (d.get("destination") or "").strip()
    eta_dt   = d.get("eta_dt")

    # Build German announcement
    if ns in (1, 5) or speed < 0.5:
        # At anchor (1) or Moored (5)
        msg = f"{name} liegt gerade im Hafen bei {location}"
    else:
        msg = f"{name} befindet sich gerade in der Nähe von {location}"

    if dest and eta_dt:
        t = eta_text(eta_dt)
        if t:
            msg += f". Das Schiff läuft {t} in {dest} ein"

    return jsonify({
        "ok":               True,
        "message":          msg,
        "location":         location,
        "lat":              d["lat"],
        "lon":              d["lon"],
        "speed_knots":      speed,
        "destination":      dest,
        "nav_status":       ns,
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
