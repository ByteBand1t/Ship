# AIDAnova Ship Tracker

Zigbee-Button → Home Assistant → Alexa sagt, wo sich die AIDAnova gerade befindet.

> **Beispiel-Ansage:** *„Lara befindet sich gerade in der Nähe von Bergen, Norwegen. Das Schiff läuft in etwa 3 Stunden in Bergen ein."*

---

## Repo-Struktur

```
ship-tracker/
├── app.py                          # Python Flask Service
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── homeassistant/
    ├── configuration.yaml          # REST-Sensor für HA
    └── automation_zigbee2mqtt.yaml # Automation (Zigbee2MQTT)
```

---

## Setup

### 1. aisstream.io API-Key holen

Kostenlos registrieren auf [aisstream.io](https://aisstream.io) (GitHub-Login möglich)
→ API Keys → neuen Key erstellen

### 2. docker-compose.yml anpassen

```yaml
environment:
  - AISSTREAM_API_KEY=DEIN_KEY_HIER
  - PERSON_NAME=Lara
```

### 3. Docker-Container auf Portainer deployen

In Portainer → **Stacks** → **Add Stack** → den Inhalt von `docker-compose.yml`
einfügen → **Deploy the stack**

Danach testen:
```
http://DEINE_SERVER_IP:5000/api/status
```

### 4. Alexa Media Player in Home Assistant installieren

In HACS nach **„Alexa Media Player"** suchen, installieren und mit dem
Amazon-Account verbinden.

### 5. Home Assistant konfigurieren

In `configuration.yaml` den Block aus `homeassistant/configuration.yaml`
einfügen – `DEINE_SERVER_IP` ersetzen – HA neu starten.

### 6. Automation anlegen

Die Datei `homeassistant/automation_zigbee2mqtt.yaml` in HA importieren
oder manuell anlegen.

Folgende Platzhalter ersetzen:

| Platzhalter | Wo finden |
|---|---|
| `DEINE_BUTTON_IEEE_ADRESSE` | HA → Einstellungen → Geräte & Dienste → ZHA → Button |
| `DEIN_BUTTON_NAME` | Zigbee2MQTT Dashboard |
| `notify.alexa_media_DEIN_GERAET` | HA → Entwicklerwerkzeuge → Dienste → nach `alexa_media` suchen |

---

## Schiff-Daten

| Eigenschaft | Wert |
|---|---|
| Schiff | AIDAnova |
| MMSI | 247389200 |
| IMO | 9781865 |
| Flagge | Italien |

---

## Funktionsweise

```
aisstream.io ──(WebSocket)──► ship-tracker ──(HTTP)──► Home Assistant
                                    │                         │
                              Nominatim                  Alexa TTS
                           (Reverse Geocoding)
```

Der Docker-Service hält eine permanente WebSocket-Verbindung zu aisstream.io
und cached die letzte bekannte Position. Home Assistant ruft alle 5 Minuten
`/api/status` ab und speichert die fertige deutsche Ansage als Sensor-Wert.
Beim Button-Druck liest die Automation diesen Wert und übergibt ihn an Alexa.
