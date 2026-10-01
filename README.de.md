# VW Group Vehicle2MQTT

[![Tests](https://github.com/StephanHenke/vwgroup-vehicle2mqtt/actions/workflows/tests.yml/badge.svg)](https://github.com/StephanHenke/vwgroup-vehicle2mqtt/actions/workflows/tests.yml)
[![Docker Image](https://github.com/StephanHenke/vwgroup-vehicle2mqtt/actions/workflows/docker-image.yml/badge.svg)](https://github.com/StephanHenke/vwgroup-vehicle2mqtt/actions/workflows/docker-image.yml)
[![Docker Pulls](https://img.shields.io/docker/pulls/stephanhenke/vwgroup-vehicle2mqtt)](https://hub.docker.com/r/stephanhenke/vwgroup-vehicle2mqtt)

[English](README.md)

Dieses Projekt soll Fahrzeugdaten abrufen, die über das EU Data Act Portal des
Volkswagen Konzerns bereitgestellt werden, und sie strukturiert an einen MQTT
Broker weiterleiten. Ziel ist, diese Werte in Smart-Home-Systemen,
Energiemanagementsystemen und ähnlichen lokalen Automatisierungsplattformen
bereitzustellen.

Der frühere Projektname war `vw-euda-mqtt`. Der neue Name
`vwgroup-vehicle2mqtt` beschreibt den Einsatzzweck breiter, weil es um
Fahrzeugdaten aus dem Volkswagen Konzern und deren Weiterleitung nach MQTT geht.

Der Dienst meldet sich bei `eu-data-act.drivesomethinggreater.com` an, wählt die
konfigurierte Marke aus, liest die neueste Continuous-Data-ZIP-Datei für eine
FIN/VIN, extrahiert nützliche Fahrzeugwerte und publiziert sie nach MQTT. MQTT
Retain ist standardmäßig deaktiviert und muss mit `mqtt.retain=true` bewusst
aktiviert werden.

Der Login- und Dataset-Ansatz basiert auf
<https://github.com/mikrohard/hass-vw-eu-data-act> (MIT).

## Was der Dienst tut

- Login beim EU Data Act Portal mit dem Konto aus `config.json`.
- Markenauswahl über den Portal-Redirect, zum Beispiel `brand=AUDI&method=login`.
- Automatische Behandlung der notwendigen VW/Audi Identity Onboarding-Schritte.
- Abruf der Continuous-Data-Metadaten und ZIP-Datasets.
- Überspringen von `*_no_content_found.zip`, weil diese Dateien keine
  Fahrzeugdaten enthalten.
- MQTT-Publish der wichtigsten Werte und optional der Rohdaten.
- Bereitstellung der Daten für Smart-Home- und Energiemanagement-Anwendungen.

Der Dienst erstellt keine Datenanfrage im Portal. Er kann nur ZIP-Dateien
abrufen, die das Portal bereits erzeugt.

## Aktuelle Einschränkung

Der Dienst ist gegen die Datenübertragung entwickelt, die Volkswagen Group über
das EU Data Act Portal bereitstellt: Er meldet sich an, liest die Metadaten der
aktiven Datenanfrage, listet erzeugte ZIP-Datasets, überspringt leere
Platzhalter-ZIPs, lädt echte ZIP-Inhalte herunter und veröffentlicht deren Werte
nach MQTT.

In den Audi/VW-Portaltests zeigte sich zunächst nur die Erzeugung von
`*_no_content_found.zip`-Platzhalterdateien. Inzwischen wurden echte
Fahrzeugdatensätze beobachtet und erfolgreich nach MQTT publiziert. Der
Datadelivery-List-Endpunkt kann jedoch weiterhin intermittierend `HTTP 500`
liefern. Der Dienst kann die zuletzt erfolgreich publizierten Werte nur dann im
MQTT-Broker retained bereitstellen, wenn `mqtt.retain=true` gesetzt ist;
ansonsten publiziert er Live-Werte und meldet während solcher Polling-Fehler
zusätzlich einen `PendingData`-Status.

## Voraussetzungen im Portal

1. Einmal im Browser auf <https://eu-data-act.drivesomethinggreater.com/>
   anmelden.
2. Die passende Marke auswählen, bei diesem Projekt typischerweise `Audi`.
3. Dasselbe myAudi/VW-Konto verwenden, das auch in `config.json` steht.
4. Offene AGB-, Registrierungs-, Land-/Sprache- und Consent-Dialoge abschließen.
5. Prüfen, dass das Fahrzeug verbunden ist und die FIN/VIN stimmt.
6. Eine `continuous/customised data request` mit 15-Minuten-Frequenz aktivieren.
7. Warten, bis das Portal ZIP-Datensätze für das Fahrzeug anzeigt.

Aktueller erwartbarer Zwischenzustand direkt nach dem Einrichten:

```text
No content datasets available yet
```

Das bedeutet, dass der Login funktioniert, aber Volkswagen bisher nur
`*_no_content_found.zip` erzeugt hat. Der Dienst pollt weiter und verarbeitet
die erste ZIP-Datei mit echtem Inhalt automatisch.

## VW/Audi Identity Onboarding

Auch bei `brand: "AUDI"` können die Login-Seiten `Volkswagen ID` anzeigen.
Audi nutzt hier die VW Group Identity Infrastruktur.

Der Dienst ruft zuerst den offiziellen Brand-Redirect des Portals auf, zum
Beispiel:

```text
/services/redirect/authentication?brand=AUDI&method=login
```

Aus diesem Redirect liest er die korrekte OIDC-Authorize-URL. Das ist wichtig,
weil Audi einen anderen Identity-Client verwenden kann als der generische
GIS-Consent-Client.

Je nach Kontostand können beim ersten nicht-browserbasierten Login diese
Stationen auftreten:

- `terms-and-conditions`: notwendige IdentityKit-Nutzungsbedingungen und
  Datenschutzdokumente werden automatisch bestätigt.
- `consent/users`: notwendige OAuth-Berechtigungen für Basisprofil und
  Fahrzeugzugriff werden automatisch erlaubt.
- `consent/marketing`: optionale Marketing-/Kommunikations-Einwilligung wird
  automatisch übersprungen.
- `verification/email-sent`: Volkswagen hat eine Bestätigungsmail gesendet. Den
  Link in der Mail öffnen und danach den Dienst neu starten.

## Konfiguration

Ausgangspunkt ist `config.example.json`:

```powershell
Copy-Item config.example.json config.json
```

Wichtige Felder:

- `email` / `password`: myAudi/VW-Konto.
- `brand`: für Audi auf `AUDI` setzen.
- `country` / `language`: für Deutschland typischerweise `de`.
- `vin`: FIN/VIN des Fahrzeugs. Wenn leer, versucht der Dienst ein einzelnes
  Fahrzeug automatisch zu finden.
- `identifier`: kann leer bleiben. Der Dienst liest den Continuous-Data-
  Identifier aus den Metadaten.
- `poll_interval_seconds`: Standard `900`, passend zu 15 Minuten.
- `save_original_data`: Standard `false`. Wenn `true`, speichert der Dienst jede
  heruntergeladene originale Portal-ZIP-Datei lokal, bevor nach MQTT publiziert
  wird.
- `original_data_dir`: Standard `data/original`. Relative Pfade werden relativ
  zur `config.json` aufgeloest; im Docker-Betrieb liegt der Ordner damit unter
  dem gemounteten `/config/data`.
- `mqtt.host`, `mqtt.port`, `mqtt.username`, `mqtt.password`: MQTT-Zugang.
- `mqtt.base_topic`: Standard `vw/euda`. Das Topic bleibt aus
  Kompatibilitätsgründen bewusst stabil, auch wenn der Dienst jetzt
  `vwgroup-vehicle2mqtt` heißt.
- `mqtt.retain`: Standard `false`. Nur auf `true` setzen, wenn MQTT-Clients nach
  einem Reconnect die letzten kleinen Status- und Zustandswerte erhalten sollen.
- `mqtt.publish_raw`: veröffentlicht zusätzlich strukturierte Datenpunkte unter
  `structured/...` und ZIP-Dateiinhalte unter `raw/file/<index>/...`.
- `mqtt.publish_history`: Standard `false`. Veröffentlicht einen live-only
  Backfill-Batch unter `history/batch/json`, damit Systeme wie openHAB Werte mit
  ihrem originalen `car_captured_at` persistieren können.
- `mqtt.publish_carcompat`: spiegelt einzelne Werte optional unter
  `car/garage/<vin>/...`.
- `mqtt.publish_homeassistant_discovery`: veröffentlicht Home-Assistant-
  kompatible MQTT-Autodiscovery-Konfigurationen.
- `mqtt.homeassistant_discovery_prefix`: Discovery-Prefix, Standard
  `homeassistant`.
- `mqtt.homeassistant_discovery_retain`: Standard `true`. Retained die Home-
  Assistant-Discovery-Konfiguration unabhängig von den Fahrzeugwerten.

`config.json` enthält Zugangsdaten und ist deshalb per `.gitignore` vom Git-Repo
ausgeschlossen.

Diese Umgebungsvariablen überschreiben `config.json` und sind nützlich für
Docker, Proxmox, NAS-Systeme und Secret-Manager:

```text
VW_EUDA_EMAIL
VW_EUDA_PASSWORD
VW_EUDA_VIN
VW_EUDA_IDENTIFIER
VW_EUDA_MQTT_HOST
VW_EUDA_MQTT_USERNAME
VW_EUDA_MQTT_PASSWORD
```

## Lokal testen

Einrichtungsdiagnose ohne Ausgabe von Zugangsdaten:

```powershell
uv run vwgroup-vehicle2mqtt --config config.json --diagnose
```

```powershell
uv run vwgroup-vehicle2mqtt --config config.json --once --dry-run --debug
```

Ohne MQTT-Publish, aber mit echtem Portal-Login:

```powershell
uv run vwgroup-vehicle2mqtt --config config.json --once --dry-run
```

Einmaliger echter Lauf mit MQTT-Publish:

```powershell
uv run vwgroup-vehicle2mqtt --config config.json --once
```

Dauerbetrieb lokal:

```powershell
uv run vwgroup-vehicle2mqtt --config config.json
```

Prüfen, ob der letzte erfolgreiche Dataset-Publish noch frisch genug für
Container-Monitoring ist:

```powershell
uv run vwgroup-vehicle2mqtt --config config.json --healthcheck
```

## Docker-Betrieb

Fertige Images werden auf Docker Hub und in der GitHub Container Registry
veröffentlicht:

```bash
docker pull stephanhenke/vwgroup-vehicle2mqtt:latest
```

Dasselbe Image ist zusätzlich über die GitHub Container Registry verfügbar:

```bash
docker pull ghcr.io/stephanhenke/vwgroup-vehicle2mqtt:latest
```

Der GitHub-Actions-Workflow veröffentlicht auf Docker Hub, wenn die
Repository-Secrets `DOCKERHUB_USERNAME` und `DOCKERHUB_TOKEN` gesetzt sind.

Nutzung des veröffentlichten Images mit Docker Compose:

```bash
cp config.example.json config.json
cp docker-compose.example.yml docker-compose.yml
mkdir -p data
docker compose up -d
```

Lokaler Entwicklungs-Build im Projektordner:

```bash
cp config.example.json config.json
mkdir -p data
docker compose up -d --build
```

Logs anzeigen:

```bash
docker logs -f vwgroup-vehicle2mqtt
```

Das veröffentlichte Image enthält einen Docker-`HEALTHCHECK`. Er liest die
konfigurierte `state_file` und meldet unhealthy, wenn noch kein erfolgreicher
Dataset-Publish gespeichert wurde oder der letzte erfolgreiche Publish älter
als vier Polling-Intervalle ist. Die Mindestgrenze liegt bei einer Stunde.

Container neu bauen und starten:

```bash
docker compose up -d --build
```

Auf Servern, auf denen Docker nur mit `sudo` verfügbar ist:

```bash
cd /pfad/zum/vwgroup-vehicle2mqtt
sudo docker compose up -d --build
sudo docker logs -f vwgroup-vehicle2mqtt
```

## MQTT-Topics

Standard-Basis:

```text
vw/euda/<vin>/
```

Auswahl der veröffentlichten Topics:

- `status/online`
- `status/connected`
- `status/error`
- `status/error_type`
- `status/last_status_at`
- `status/last_poll_at`
- `status/last_success_at`
- `status/last_error_at`
- `status/last_dataset`
- `status/captured_at`
- `status/car_captured_at`
- `status/data_age_seconds`
- `status/stale`
- `status/service_version`
- `battery/soc`
- `battery/target_soc`
- `battery/charge_bulk_threshold`
- `battery/charge_power_kw`
- `odometer/km`
- `range/km`
- `charging/state`
- `charging/mode`
- `charging/scenario`
- `charging/action_state`
- `charging/mode_selection`
- `charging/max_charge_current_ac`
- `doors/locked`
- `parking_brake`
- `battery/min_temperature_c`
- `battery/max_temperature_c`
- `climate/remaining_time_s`
- `<normalized-topic>/car_captured_at`

`status/car_captured_at` ist aus den `car_captured_time`-Einträgen des Pakets
abgeleitet. Laut VW/Audi-Datenbeschreibung ist dies ein UTC-Zeitpunkt, an dem
der Bericht fahrzeugseitig bzw. auf dem Weg von ICAS1/OCU zum Backend
erzeugt/gesendet wurde. Reale Pakete enthalten mehrere
`car_captured_time`-Einträge; jeder dieser Zeitstempel schließt die davor stehende
Datenpunkt-Gruppe ab. Der Dienst nutzt den neuesten dieser Werte als
Datensatz-Zeitstempel und veröffentlicht die Zeitstempel zusätzlich passend zum
normalisierten Datenpunkt unter `<normalized-topic>/car_captured_at`. Endet ein
Paket mit Datenpunkten nach dem letzten `car_captured_time`, werden diese
Nachläufer dem letzten bekannten Capture-Zeitstempel zugeordnet.
`status/captured_at` bleibt als kompatibler Alias erhalten.
`status/last_poll_at` und `status/last_success_at` sind Zeitstempel des
Dienstes. `status/data_age_seconds` wird aus `car_captured_at` berechnet;
`status/stale` wird `true`, wenn der Datensatz älter als zwei konfigurierte
Polling-Intervalle ist.

Normalisierte Fahrzeug-Topics:

| MQTT-Topic | Quellfeld | Bedeutung |
| --- | --- | --- |
| `battery/soc` | `battery_state_report.soc` | Ladezustand der Batterie in Prozent |
| `battery/target_soc` | `settings.target_soc` | Eingestelltes Ladeziel in Prozent |
| `battery/charge_bulk_threshold` | `battery_state_report.charge_bulk_threshold` | Bulk-Ladeschwelle in Prozent |
| `battery/charge_power_kw` | `battery_state_report.charge_power` | Gemeldete Ladeleistung |
| `odometer/km` | `mileage.value` | Kilometerstand, geschützt gegen `0` und fallende Werte |
| `range/km` | `range` | Gemeldete elektrische Reichweite, sofern im Datensatz enthalten |
| `charging/state` | `charging_state_report.current_charge_state` | Aktueller Ladestatus |
| `charging/mode` | `charging_state_report.charge_mode` | Lademodus |
| `charging/scenario` | `charging_state_report.charging_scenario` | Ladeszenario |
| `charging/action_state` | `charging_state_report.immediate_action_state` | Status einer unmittelbaren Ladeaktion |
| `charging/mode_selection` | `settings.charge_mode_selection` | Gewählte Ladeeinstellung |
| `charging/max_charge_current_ac` | `settings.max_charge_current_ac` | Einstellung für maximalen AC-Ladestrom |
| `doors/locked` | `locked` | Verriegelungsstatus |
| `parking_brake` | `parking_brake` | Status der Parkbremse |
| `battery/min_temperature_c` | `min_temperature` | Niedrigste gemeldete Batterietemperatur |
| `battery/max_temperature_c` | `max_temperature` | Höchste gemeldete Batterietemperatur |
| `climate/remaining_time_s` | `remaining_climate_time` | Restlaufzeit der Klimatisierung in Sekunden |

Der EU-Data-Act-Delivery-Endpunkt liefert ZIP-Dateien. Wenn `mqtt.publish_raw`
aktiviert ist, trennt der Dienst zwei MQTT-Sichten sauber:

- `structured/by_name/<datapoint_name>/...`: katalogbasierte Sicht auf einen
  Datenpunkt. Die direkten Blätter zeigen immer den neuesten Wert zu diesem
  Namen.
- `structured/by_name/<datapoint_name>/keys`: JSON-Liste der passenden
  technischen Keys, neuester zuerst, jeweils mit `key` und `car_captured_at`.
- `structured/by_key/<datapoint_key>/...`: technische Key-Sicht auf denselben
  Datenpunkt, basierend auf dem `key` aus dem ZIP-Inhalt.
- `raw/file/<index>/filename`: ursprünglicher Dateiname im ZIP.
- `raw/file/<index>/content`: originaler Inhalt dieser ZIP-Datei.

Beispiel:

```text
vw/euda/<vin>/structured/by_name/soc/key
vw/euda/<vin>/structured/by_name/soc/value
vw/euda/<vin>/structured/by_name/soc/car_captured_at
vw/euda/<vin>/structured/by_key/<datapoint_key>/name
vw/euda/<vin>/raw/file/0/filename
vw/euda/<vin>/raw/file/0/content
```

MQTT Retain für Fahrzeug-, Status-, `structured`- und Raw-Werte ist Opt-in.
Standardmäßig werden diese Topics nicht retained. Wenn `mqtt.retain=true`
gesetzt ist, können kleine Status- und `structured`-Blätter retained werden.
`.../json` und Raw-Dateiinhalte bleiben immer reine Live-Publishes. Home-
Assistant-Discovery-Configs nutzen den separaten Schalter
`mqtt.homeassistant_discovery_retain`. Der Dienst veröffentlicht keine
Delete-/Tombstone-Nachrichten für alte Broker-Layouts.

Bei Login- oder Polling-Fehlern schreibt der Dienst Fehlerstatus nach:

```text
vw/euda/<vin>/status/...
```

Wenn keine VIN konfiguriert ist, nutzt er:

```text
vw/euda/_service/status/...
```

## evcc-Integration

Eine eigene Anleitung zur Einbindung in evcc liegt in [evcc.md](evcc.md).
Ein Beispiel für eine MQTT-basierte evcc-Fahrzeugkonfiguration liegt unter
[evcc/mqtt-vehicle.example.yaml](evcc/mqtt-vehicle.example.yaml).

## Home Assistant

Home-Assistant-kompatible MQTT-Autodiscovery kann mit
`mqtt.publish_homeassistant_discovery` aktiviert werden. Eine eigene Anleitung
liegt in [homeassistant.md](homeassistant.md). Ein minimales Config-Beispiel
liegt unter
[homeassistant/mqtt-autodiscovery.example.json](homeassistant/mqtt-autodiscovery.example.json).
Historische Rückdatierung ist über MQTT-State-Updates nicht möglich; Details
stehen in [history.md](history.md).

## openHAB

openHAB kann die MQTT-Topics über das MQTT Binding als Generic MQTT Thing
einbinden. Setze `mqtt.retain=true`, wenn openHAB nach einem Reconnect den
letzten Zustand erhalten soll. Eine eigene Anleitung liegt in [openhab.md](openhab.md).
Beispieldateien für Things, Items und den historischen Backfill liegen in
[openhab/](openhab/). Der Backfill-Weg mit originalen `car_captured_at`-
Zeitstempeln ist in [history.md](history.md) beschrieben.

## Fehlersuche

Bei neuer Einrichtung zuerst `--diagnose` ausführen. Der Modus prüft
Konfiguration, MQTT-Verbindung, Portal-Login, Fahrzeugauswahl, Continuous-
Data-Identifier und Dataset-Liste, ohne konfigurierte Zugangsdaten auszugeben.

`Authentication failed: terms-and-conditions`

Der alte Fehler bedeutete, dass VW Identity noch AGB/Registrierung verlangte.
Der Dienst kann diesen Schritt inzwischen automatisch bestätigen. Falls der
Fehler erneut auftaucht, mit `--debug` testen und die Ziel-URL prüfen.

`verification/email-sent`

Volkswagen hat eine Bestätigungsmail geschickt. Den Link in der Mail anklicken
und danach den Dienst neu starten.

`Authentication failed: Login rejected with HTTP 404`

[Issue #4](https://github.com/StephanHenke/vwgroup-vehicle2mqtt/issues/4)
beschreibt eine fehlende lokalisierte Startseite (`/se/en/user.html`) nach dem
Portal-Login-Callback. Diese Redirect-Kette deutet nicht auf fehlenden User
Consent hin. Der Dienst akzeptiert diesen speziellen 404 nur nach einem
Portal-Callback und erfolgreicher Sitzungsprüfung an der Fahrzeug-API.
Andere HTTP-Fehler bleiben Fehler. Scheitert die Sitzungsprüfung, den
Browser-Login mit demselben Markenkonto abschließen, gegebenenfalls nötige
Einwilligungen oder E-Mail-Bestätigung prüfen und `--diagnose` erneut starten.
Keine Cookies, Autorisierungscodes, Tokens oder unredigierten Redirect-URLs teilen.

`No continuous-data Identifier returned`

Im EU Data Act Portal ist für die VIN noch keine Continuous-/Customised-
Datenanfrage aktiv.

`No content datasets available yet`

Login, Marke und Identifier funktionieren. Das Portal hat für diesen Poll noch
keine ZIP-Datei mit Inhalt geliefert. Warten, bis eine ZIP-Datei mit echtem
Inhalt vorhanden ist; sobald das Portal ein nicht-leeres Dataset bereitstellt,
verarbeitet der Dienst es automatisch.

`HTTP 401`

Meist wurde der falsche Brand-/Identity-Client verwendet oder der Portal-Login
ist abgelaufen. Mit `--debug` prüfen, ob der Login bei
`/de/de/user.html` landet.

`HTTP 500` beim List-Endpunkt

Kann beim Portal transient auftreten. Der Dienst versucht den Datadelivery-
List-Endpunkt innerhalb desselben Polls mehrfach mit kurzem exponentiellem
Backoff. Erst wenn diese Versuche fehlschlagen, publiziert er `PendingData` und
wiederholt den gesamten Poll nach `retry_interval_seconds`.

## Git- und Sicherheitsnotizen

Siehe [SECURITY.md](SECURITY.md) für Hinweise zu Responsible Disclosure und zur
Redaktion echter Datenpakete.

Die Nutzung von Konto-E-Mail und Passwort ist eine pragmatische Lösung für den
aktuellen, browserorientierten Portalfluss. Sie ist ausdrücklich nicht die
feinste langfristige Lösung. Aus Sicht dieses Projekts wäre ein eigener API-Key
oder Token sauberer, der über das VW/Audi-Benutzerkonto ausgegeben wird, damit
Drittdienste autorisiert werden können, ohne das eigentliche Kontopasswort zu
speichern.

- `config.json` ist ignoriert, weil es Konto- und MQTT-Zugangsdaten enthält.
- `access.txt` ist ignoriert, weil dort VM-Zugangsdaten liegen können.
- `data/` ist ignoriert und enthält nur Laufzeitstatus.
- Niemals echte Passwörter, VINs mit Personenbezug oder Tokens committen, wenn
  das Repo öffentlich werden soll.
- Echte Datenpakete für Tests oder Issues müssen wie
  `tests/fixtures/audi_dataset_redacted.json` redigiert werden.
