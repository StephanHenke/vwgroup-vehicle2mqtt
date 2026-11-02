# Historische Daten und Persistence

## Deutsch

Ein VW/Audi-Dataset kann mehrere Werte desselben Datenpunkts enthalten, die zu
unterschiedlichen Fahrzeugzeitpunkten aufgenommen wurden. MQTT speichert beim
Empfaenger normalerweise nur den Zeitpunkt des Empfangs. Damit historische Werte
mit ihrem originalen `car_captured_at` in der Persistence landen, veroeffentlicht
`vwgroup-vehicle2mqtt` optional einen Backfill-Batch.

Aktivierung in `config.json`:

```json
{
  "mqtt": {
    "publish_history": true
  }
}
```

Der Dienst sendet dann zusaetzlich:

```text
vw/euda/<VIN>/history/batch/json
```

Dieses Topic ist immer live-only und wird nie retained.

## Batch-Format

Der Batch enthaelt nur Datenpunkte mit einem eigenen gueltigen `timestampUtc`,
`timestampUTC` oder `timestamp`. Ein separates `car_captured_time` datiert keine
Nachbarwerte und wird nicht als Messwert exportiert. Ein kontinuierliches Paket
ohne eigene Messzeiten kann deshalb einen leeren History-Batch liefern, obwohl
seine Werte als letzter bekannter Live-Stand verfuegbar sind. Bereits falsch
datierte Persistence-Eintraege werden nicht automatisch veraendert.

Vereinfachtes Beispiel:

```json
{
  "vin": "<VIN>",
  "dataset": "dataset.zip",
  "event_count": 2,
  "events": [
    {
      "key": "soc",
      "field_name": "battery_state_report.soc",
      "name": "soc",
      "value": 77,
      "unit": "%",
      "car_captured_at": "2026-01-02T03:04:05+00:00",
      "curated_topic": "battery/soc"
    }
  ]
}
```

## openHAB

openHAB ist der bevorzugte Weg fuer diesen Backfill, weil openHAB Item-Zustaende
mit explizitem Timestamp persistieren kann. Die Beispiele liegen hier:

```text
openhab/vwgroup-vehicle2mqtt-history.items
openhab/vwgroup-vehicle2mqtt-history.js
```

Vorgehen:

1. MQTT Binding, JavaScript Scripting und einen Persistence-Service installieren.
2. Bevorzugt `timescaledb` als Persistence-Service verwenden.
3. `mqtt.publish_history=true` setzen.
4. Den Channel `history_batch_json` aus dem Things-Beispiel uebernehmen.
5. Das History-Item anlegen.
6. Die JavaScript-Rule nach `$OPENHAB_CONF/automation/js/` kopieren.
7. In der Rule `PERSISTENCE_SERVICE` auf deinen Service setzen, empfohlen
   `timescaledb`.
8. Das Mapping `ITEM_BY_CURATED_TOPIC` an deine Item-Namen anpassen.

Die Rule persistiert jeden Wert einzeln mit
`item.persistence.persist(timestamp, state, 'timescaledb')`. Der normale aktuelle
Item-Zustand wird dadurch nicht neu kommandiert; es wird nur die Persistence mit
dem originalen `car_captured_at` befuellt.

Empfehlung: Das Live-Item sollte den letzten Fahrzeugwert anzeigen. Haenge diese
Live-Items aber nicht zusaetzlich in automatische Persistence-Strategien wie
`everyChange` oder `everyUpdate` fuer denselben Service. Sonst entstehen
Mischdaten aus Empfangszeitpunkten und originalen Fahrzeugzeitpunkten. Die
historischen Punkte sollten kontrolliert ueber die Rule injiziert werden.

Hinweis: Wenn `publish_unchanged=true` genutzt wird, kann derselbe Dataset-Batch
mehrfach gesendet werden. Fuer Backfill sollte `publish_unchanged=false` bleiben,
damit keine doppelten Persistence-Punkte entstehen.

Auch die Retention des Persistence-Service muss zum Backfill passen. Wenn die
Retention aelter Datenpunkte ausschliesst, werden diese vom Backend verworfen.

## Home Assistant

Home Assistant MQTT-State- und MQTT-Discovery-Topics schreiben den aktuellen
Entity-State mit dem Empfangszeitpunkt in den Recorder. Ein mitgelieferter
`car_captured_at`-Wert kann als Attribut oder eigenes Topic sichtbar sein,
ersetzt aber nicht den Recorder-Zeitstempel.

Fuer echte rueckdatierte historische Werte braucht Home Assistant einen anderen
Weg als MQTT-State-Updates, z. B. Recorder-/Statistics-Import fuer numerische
Langzeitstatistiken oder eine externe Timeseries-Datenbank.

## English

History includes only datapoints with their own valid `timestampUtc`,
`timestampUTC` or `timestamp`. A separate `car_captured_time` does not date its
neighbours. Continuous packages can therefore have an empty history batch
while still supplying live readings. Existing incorrectly dated persistence
records are not automatically rewritten.

A VW/Audi dataset can contain multiple values of the same datapoint captured at
different vehicle-side timestamps. MQTT consumers usually persist the receive
time, not the original vehicle timestamp. To support backfill workflows,
`vwgroup-vehicle2mqtt` can publish an optional history batch.

Enable it in `config.json`:

```json
{
  "mqtt": {
    "publish_history": true
  }
}
```

The service publishes:

```text
vw/euda/<VIN>/history/batch/json
```

This topic is always live-only and never retained.

openHAB is the primary supported backfill path because it can persist Item states
with explicit timestamps. `timescaledb` is the recommended persistence service.
Use:

```text
openhab/vwgroup-vehicle2mqtt-history.items
openhab/vwgroup-vehicle2mqtt-history.js
```

Install the MQTT Binding, JavaScript Scripting, and a persistence service. Then
enable `mqtt.publish_history`, add the History Batch item, place the JavaScript
rule in `$OPENHAB_CONF/automation/js/`, keep `PERSISTENCE_SERVICE='timescaledb'`
or adapt it, and update the `ITEM_BY_CURATED_TOPIC` mapping.

Recommended setup: live Items show the latest vehicle state, but they should not
also be automatically persisted via `everyChange` or `everyUpdate` for the same
service. Historical points should be injected by the rule with their original
`car_captured_at` timestamp.

Home Assistant MQTT state updates cannot backdate Recorder states. For real
historical timestamps, use Recorder/Statistics import for numeric long-term
statistics or an external timeseries database.

References:

- https://www.openhab.org/docs/configuration/persistence
- https://www.openhab.org/addons/automation/jsscripting/
- https://www.home-assistant.io/integrations/recorder
