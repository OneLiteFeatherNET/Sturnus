# Sturnus — Design

Discord-Voice-Transkription mit Outline-Ablage für OneLiteFeather.

Status: Entwurf zur Review · Datum: 2026-08-19

## 1. Ziel

Ein dedizierter Discord-Voice-Channel wird automatisch aufgezeichnet und als
chronologisches Transkript in einem Dokumentsystem abgelegt — in dieser Phase
Outline, über einen austauschbaren Adapter (Abschnitt 8). Sprecher erscheinen
darin als echte Nutzer des Zielsystems, sofern sie ihren Discord-Account
einmalig damit verknüpft haben.

Der Name folgt der Vogel-Konvention der Organisation (Falco, Otis, Ducula, Pica,
Guira, Aves): *Sturnus vulgaris*, der Star, ist für seine Stimmen-Imitation
bekannt.

## 2. Nicht-Ziele

Bewusst außerhalb dieser Phase:

- **Keine LLM-Zusammenfassung.** Das Dokument enthält ausschließlich das
  Roh-Transkript. Eine Zusammenfassungsstufe über die bestehende Ollama-Instanz
  ist eine mögliche spätere Phase, kein Teil dieses Entwurfs.
- **Keine Speaker-Diarization.** Discord liefert getrennte Audio-Streams pro
  Nutzer; die Sprechertrennung ist damit ein gelöstes Problem und benötigt weder
  pyannote noch WhisperX.
- **Keine Per-Nutzer-OAuth-Token.** Der Bot schreibt mit einem einzigen
  Service-Token nach Outline. Der OAuth-Flow dient allein der einmaligen
  Identitätsfeststellung.
- **Kein Live-Transkript.** Die Transkription beginnt erst, wenn die Session
  beendet ist, und läuft über die vollständige Aufnahme.
- **Nur der Outline-Adapter.** Der Port für die Dokumentablage wird gezogen,
  aber allein Outline dahinter implementiert. Confluence, Notion und Markdown-
  Dateien sind vorgesehene Erweiterungen, kein Lieferumfang dieser Phase.
- **Nur ein Aufnahme-Channel pro Guild.** Das Datenmodell ist auf mehrere
  vorbereitet, die Konfiguration in dieser Phase nicht.

## 3. Rechtlicher Rahmen

Die Aufzeichnung des nicht-öffentlich gesprochenen Wortes ohne Einwilligung ist
nach § 201 StGB strafbar. Das Design behandelt Einwilligung deshalb nicht als
Feature, sondern als Vorbedingung jeder Audio-Verarbeitung.

### 3.1 Zweistufiger Schutz

**Primär — Discord-Permissions.** Im Aufnahme-Channel erhält `@everyone` die
Berechtigung `Speak: deny`, die Consent-Rolle `Speak: allow`. Wer nicht
eingewilligt hat, kann technisch kein Audio senden.

**Sekundär — Bot-seitiger SSRC-Filter.** Nutzer mit `Administrator`-Berechtigung
umgehen Channel-Overrides und können unabhängig von der Rolle sprechen. Der Bot
prüft deshalb bei jedem eingehenden Stream, ob der zugeordnete Nutzer die
Consent-Rolle trägt, und verwirft die Pakete andernfalls, bevor sie einen Puffer
erreichen. Dieser Filter ist nicht redundant, sondern deckt einen real
existierenden Bypass ab.

Die Prüfung erfolgt fortlaufend, nicht einmalig beim Session-Start. Widerruft
jemand seine Einwilligung während einer laufenden Session, wird sein Stream ab
diesem Moment verworfen; bereits aufgezeichnetes Audio bleibt unberührt, da der
Widerruf nach Art. 7 Abs. 3 DSGVO nur in die Zukunft wirkt.

### 3.2 Deployment-Voraussetzungen

Diese Punkte sind nicht optional, sondern Bedingung für den Betrieb:

- Es müssen **nicht-aufgezeichnete Voice-Channels** als Alternative existieren.
  Ist die Einwilligung die einzige Möglichkeit zur Teilnahme am Sprachbetrieb,
  ist ihre Freiwilligkeit nach Art. 7 Abs. 4 DSGVO (Kopplungsverbot) angreifbar.
- **Channel-Name und Channel-Topic benennen die Aufzeichnung.** Da der Bot ohne
  expliziten Startbefehl automatisch joint, existiert kein Moment, in dem jemand
  die Aufnahme bewusst auslöst — die Kennzeichnung muss den Channel selbst
  tragen.
- Der Bot postet bei jedem Join eine sichtbare Ankündigung in den Text-Teil des
  Channels.
- **Die Datenschutzerklärung benennt die Aufbewahrungsdauer der Audioaufnahmen
  und deren Zweck.** Da Aufnahmen die Transkription überdauern (Abschnitt 12),
  ist das kein Detail der Umsetzung, sondern Bestandteil dessen, worin
  eingewilligt wird. Ändert sich die Dauer, ändert sich `policy_version` und die
  Einwilligung ist neu einzuholen.

### 3.3 Widerruf

`/consent revoke` entzieht die Rolle und setzt `revoked_at` (Art. 7 Abs. 3,
jederzeitiger Widerruf). Bereits erstellte Transkripte bleiben bestehen; der
Widerruf wirkt ab dem Zeitpunkt seiner Erteilung.

## 4. Architektur

Drei Deployments, eine gemeinsame PostgreSQL-Datenbank, ein S3-Bucket.

### 4.1 `bot`

Python 3.12, `discord.py` 2.x mit `discord-ext-voice-recv`. 1 CPU,
`replicas: 1` fix, keine HPA — eine einzelne Gateway-Verbindung ist nicht
horizontal skalierbar. Kein Ingress.

Zur Bibliothekswahl: `discord.py` unterstützt kein Voice-Receive. Die
Alternative wäre `py-cord`, dessen `WaveSink` die Pakete pro Nutzer jedoch nur
aneinanderhängt, ohne Stille aufzufüllen — die Streams driften auseinander und
die Chronologie des Protokolls wird unbrauchbar. `discord-ext-voice-recv`
gewährt Zugriff auf die RTP-Timestamps, aus denen sich die absolute Position
jedes Segments rekonstruieren lässt. Das ist der Grund für die Wahl.

Aufgaben: Slash-Commands, Session-State-Machine, Voice-Receive, Consent-Filter,
Aufzeichnung, S3-Upload bei Session-Ende, Job-Enqueue, Veröffentlichung des
Dokument-Links. Health- und Metrics-Endpunkte auf einem
internen Port (`/healthz`, `/readyz`, `/metrics`, `/version`).

### 4.2 `link-service`

Python 3.12, kleiner HTTP-Service. Einziges Deployment mit Ingress (über
Cloudflare Tunnel, analog zur Outline-Installation).

Aufgaben: OAuth-Callback für die Account-Verknüpfung. Getrennt vom Bot, weil der
Bot-Pod sowohl das Discord- als auch das Outline-Service-Token hält und nicht
öffentlich erreichbar sein soll — und weil ein Deploy am Link-Flow sonst einen
Bot-Neustart erzwingen würde, der jede laufende Aufnahme verliert.

### 4.3 `worker`

Python 3.12, `faster-whisper`. 4 CPU. Kein Ingress.

Aufgaben: Transkriptions-Jobs aus der Queue ziehen, den vollständigen
Sprecher-Stream transkribieren, das Audio löschen; sind alle Sprecher einer
Session fertig, die Transkripte zusammenführen und das Outline-Dokument anlegen.

### 4.4 Code-Struktur

Alle drei Deployments teilen sich ein Paket mit einer nach innen gerichteten
Abhängigkeitsregel:

```
src/sturnus/
  domain/          reine Logik, keine I/O
    session.py       Session-State-Machine
    timeline.py      RTP-Zeitrekonstruktion, Segment-Merge
    transcript.py    Markdown- und Mention-Rendering
    consent.py       Consent-Auflösung
  application/     Use-Cases, orchestrieren Ports
    ports.py         Protocol-Definitionen
    record_session.py
    transcribe_speaker.py
    publish_document.py
    link_account.py
  infrastructure/  Adapter auf konkrete Technik
    discord/         Voice-Receive-Adapter, Cogs
    db/              ORM-Modelle, Repositories, Migrationen
    objectstore/     S3, Verschlüsselung
    whisper/         faster-whisper
    documents/       DocumentSink-Adapter, aktuell Outline
    templates/       Jinja2-Umgebung und mitgelieferte Standard-Templates
```

**Die Abhängigkeitsregel:** `domain` importiert weder `application` noch
`infrastructure` und keine Fremdbibliothek mit I/O — kein `discord`, kein
`sqlalchemy`, kein `boto3`. `application` kennt nur `domain` und die eigenen
Ports, niemals einen konkreten Adapter.

Das ist kein Selbstzweck. Die gesamte Logik, die dieses Projekt schwierig macht
— Zeitrekonstruktion, Session-Übergänge, Segment-Merge — liegt damit in Code, der
ohne Discord-Verbindung, ohne Datenbank und ohne Audiodatei testbar ist. Und der
Adapter, der laut Abschnitt 15 das größte Fremdrisiko trägt, ist austauschbar,
ohne die Kernlogik zu berühren.

Damit die Regel nicht mit der Zeit verfällt, wird sie **als Test durchgesetzt**
(Abschnitt 14), nicht als Konvention dokumentiert.

### 4.5 Ports und ihre Grenze

Als Protocol abstrahiert werden die Systeme, die in Tests durch Fakes ersetzt
werden müssen oder deren Implementierung wechseln kann:

| Port | Begründung |
|---|---|
| `TranscriptionEngine` | In Unit-Tests gefaked; Wechsel zwischen `large-v3-turbo` und `small` |
| `AudioStore` | S3 in Unit-Tests durch In-Memory-Fake ersetzt |
| `DocumentSink` | Der eigentliche Erweiterungspunkt (Abschnitt 8): Outline heute, Confluence, Notion oder Dateiablage später |
| `VoiceReceiver` | Kapselt `discord-ext-voice-recv`, hält den Bibliothekswechsel lokal |

**Für Repositories werden bewusst keine Interfaces definiert.** Die
Datenzugriffsschicht wird gegen eine echte PostgreSQL-Instanz über Testcontainers
getestet (Abschnitt 14); ein Interface mit genau einer Implementierung und einem
echten Datenbanktest dahinter wäre Zeremonie ohne Nutzen. SOLID verlangt
Abstraktion dort, wo Implementierungen variieren — nicht überall. Diese Grenze
ist Teil des Entwurfs und keine Nachlässigkeit.

## 5. Session-Lebenszyklus

Der Bot beobachtet `on_voice_state_update` für den konfigurierten Channel.

### 5.1 Zustandsübergänge

| Auslöser | Übergang |
|---|---|
| Erster Nutzer **mit Consent-Rolle** betritt den Channel | `IDLE` → `RECORDING`, Bot joint, Ankündigung wird gepostet |
| Nutzer ohne Consent-Rolle betritt den leeren Channel | kein Übergang — niemand kann sprechen |
| Letzter berechtigter Nutzer verlässt den Channel | `RECORDING` → `GRACE` |
| Berechtigter Nutzer kehrt während `GRACE` zurück | `GRACE` → `RECORDING`, dieselbe Session läuft weiter |
| `empty_grace_seconds` läuft ab | `GRACE` → `CLOSING` |
| `idle_timeout_minutes` ohne jedes Audio | `RECORDING` → `CLOSING` |
| `max_session_hours` erreicht | `RECORDING` → `CLOSING` |

`CLOSING` schließt die Aufnahmedateien, lädt sie nach S3, reiht je Sprecher einen
Transkriptions-Job ein, setzt die Session auf `closed` und verlässt den Channel.

Eine Session entspricht genau einem Outline-Dokument.

### 5.2 Testbarkeit

Die State-Machine ist als reine Klasse mit **injizierter Uhr** implementiert und
kennt weder Discord noch Datenbank. Genau der Teil, der sich sonst nur mit
echten Personen in einem Voice-Channel prüfen ließe, wird damit deterministisch
unit-testbar.

### 5.3 Abschluss und Dokumenterstellung

Je Sprecher entsteht ein Transkriptions-Job. Der Worker prüft nach jedem
erfolgreich abgeschlossenen Job, ob für dessen Session noch ein weiterer Job
offen ist; ist keiner mehr offen, führt derselbe Durchlauf den Merge aus und legt
das Dokument an. Die Prüfung läuft in derselben Transaktion wie der Statuswechsel
des Jobs, damit bei gleichzeitig endenden Jobs nicht zwei Dokumente entstehen.

Ein Job je Sprecher statt einem je Session hat zwei Gründe: ein Fehlversuch
wiederholt nur den betroffenen Sprecher statt der gesamten Session, und der
Fortschritt einer mehrstündigen Aufnahme ist beobachtbar. Die Jobs werden
nacheinander abgearbeitet — `faster-whisper` nutzt bereits alle Kerne des
Workers, parallele Jobs würden sich gegenseitig ausbremsen.

## 6. Audio-Pipeline

### 6.1 Erfassung

Eingehende Opus-Pakete werden pro Nutzer **sofort beim Empfang auf 16 kHz Mono
PCM** dekodiert — das Zielformat von Whisper. Ein späteres Resampling entfällt.

Stille wird anhand der RTP-Timestamps **aufgefüllt**. Alle Sprecher-Puffer eines
Sprecher-Streams sind dadurch gleich lang und exakt synchron, was das Zusammenführen zu
einer trivialen Operation auf gemeinsamen Zeitachsen macht statt zu einer
fehleranfälligen Heuristik. Das Padding kostet Speicher, aber keine Rechenzeit:
`vad_filter=True` lässt Whisper die Stille überspringen.

### 6.2 Zeitrekonstruktion

RTP-Timestamps laufen bei Opus/48 kHz mit 48000 Ticks pro Sekunde. Der
Startwert ist pro SSRC zufällig, weshalb absolute Zeit über einen Referenzpunkt
bestimmt wird:

Beim **ersten Paket eines SSRC** wird das Paar `(wall_clock_now, rtp_ts_first)`
festgehalten. Für jedes weitere Paket gilt:

```
absolute_time = wall_clock_first + (rtp_ts - rtp_ts_first) / 48000
```

Das ergibt sample-genaues Timing innerhalb eines Nutzer-Streams und
Wanduhr-Genauigkeit für die Ausrichtung zwischen Nutzern; die Abweichung
entspricht dem Netzwerk-Jitter des jeweils ersten Pakets und liegt typischerweise
unter 100 ms — für ein lesbares Protokoll ausreichend.

**Fallstrick:** Bei einem Reconnect wechselt die SSRC eines Nutzers. Die
Zuordnung SSRC → Discord-User wird deshalb laufend über die Speaking-Events
gepflegt, und jede neue SSRC erhält einen eigenen Referenzpunkt.

### 6.3 Aufzeichnung und Übergabe

Die Aufnahme läuft über die gesamte Session als **ein durchgehender Strom je
Sprecher**, ohne Unterteilung. Erst wenn die Session in `CLOSING` wechselt, werden
die Dateien geschlossen, nach S3 geladen und je Sprecher ein Transkriptions-Job
eingereiht.

Der Grund ist Qualität: jede Schnittstelle im Audio ist eine Stelle, an der
Whisper seinen Kontext verliert, Sätze über die Grenze hinweg schlechter erkannt
werden und die Sprachdetektion auf weniger Material arbeitet. Ein einziger
Durchlauf über die vollständige Aufnahme liefert die bestmögliche Transkription,
die dieses Modell hergibt.

Der Preis ist Latenz. Die Transkription beginnt erst nach Session-Ende und
braucht bei etwa 1× Realtime ungefähr so lange wie die reine Redezeit: eine
vierstündige Session mit zwei Stunden tatsächlicher Sprache ist rund zwei Stunden
nach ihrem Ende als Dokument verfügbar. Deshalb wird der Dokument-Link
anschließend in den Channel gestellt (Abschnitt 8.5) — wer die Sitzung verlassen
hat, erfährt sonst nie, dass das Protokoll fertig ist.

**Der Bot puffert nicht im Arbeitsspeicher, sondern schreibt fortlaufend auf ein
Volume.** Die Aufnahmelänge ist dadurch vom RAM entkoppelt. Bei 16 kHz Mono
belegt eine Stunde je Sprecher rund 115 MB; die Obergrenze `max_session_hours`
begrenzt den ungünstigsten Fall, und die Volume-Größe wird auf
`max_session_hours × erwartete Sprecherzahl` ausgelegt.

**Das Volume ist ein PVC, kein `emptyDir`.** Ohne Unterteilung hängt eine ganze
Session an dieser einen Datei — ein `emptyDir` verliert sie bei jedem
Reschedule. Ein SIGTERM-Handler schließt die Dateien geordnet und reiht die Jobs
ein; kommt der Bot einem harten Kill nicht zuvor, findet er die verwaisten
Aufnahmen beim nächsten Start auf dem PVC, lädt sie hoch und reiht sie nach. Bei
`replicas: 1` ist ein RWO-PVC dafür ausreichend.

Ohne diese beiden Vorkehrungen würde ein einzelner Deploy eine mehrstündige
Aufnahme vernichten — bei Unterteilung in Abschnitte wäre der Verlust auf einen
Abschnitt begrenzt gewesen, hier ist er es nicht.

## 7. Transkription

`faster-whisper` mit `large-v3-turbo` in int8 als Default. Turbo ist ein
destillierter Decoder, läuft auf 4 Kernen bei etwa 1× Realtime bei rund 1,6 GB
RAM und liefert für Deutsch deutlich bessere Ergebnisse als `small`. `small`
bleibt als konfigurierbarer Fallback, falls das Sizing im Betrieb nicht aufgeht.

**Die Sprache wird automatisch erkannt, aber nur einmal je Sprecher und
Session.** Whisper bestimmt die Sprache anhand der ersten 30 Sekunden eines
Durchlaufs — bei Silence-Padding wäre das unter Umständen Stille. Die Detektion
läuft deshalb auf dem **ersten VAD-Segment mit substantieller Sprache**, nicht
auf dem Anfang der Datei. Das Ergebnis wird in `session_participant` festgehalten
und für den gesamten Durchlauf dieses Sprechers als `language` gesetzt.

Erkennt die Detektion nichts Belastbares — etwa bei einem Sprecher mit nur
wenigen Wortmeldungen —, greift eine konfigurierbare Standardsprache.

`vad_filter=True` überspringt die aufgefüllte Stille.

**Halluzinationsrisiko bei langen Durchläufen.** Whisper trägt über
`condition_on_previous_text` Kontext zwischen seinen 30-Sekunden-Fenstern weiter,
was die Qualität hebt — aber bei langem Audio zu Kaskaden führen kann, in denen
sich ein einmal abgedrifteter Text selbst verstärkt. Bei einem Durchlauf über
eine vollständige Session ist diese Gefahr am größten, weil keine Schnittstelle
den Kontext zurücksetzt. Die Schwellen für `compression_ratio_threshold` und
`no_speech_threshold` bleiben deshalb aktiv, und das Abschalten von
`condition_on_previous_text` ist der Rückfallweg, falls sich Wiederholungsartefakte
im Betrieb zeigen.

Jeder Nutzer-Stream wird einzeln transkribiert. Die resultierenden Segmente
tragen Offsets relativ zum Aufnahmebeginn des Sprechers, die über dessen
Referenzzeitpunkt (Abschnitt 6.2) in absolute Zeit umgerechnet werden.

## 8. Dokumentablage

Das Ziel der Transkripte ist austauschbar. Outline ist der erste und einzige in
dieser Phase gebaute Adapter; Confluence, Notion oder das Ablegen als
Markdown-Dateien sollen später ohne Eingriff in die Kernlogik ergänzt werden
können.

Umgesetzt wird deshalb **die Naht, nicht der Vorrat**: der Port wird sauber
gezogen und Outline dahinter implementiert. Weitere Adapter jetzt schon zu bauen,
ohne dass ein Ziel feststeht, würde Annahmen zementieren, die sich am zweiten
realen Adapter als falsch erweisen.

### 8.1 Der Port

`domain` erzeugt ein **zielneutrales Transkript-Modell**: eine Liste von Blöcken
aus Zeitpunkt, Sprecheridentität und Text, dazu Session-Metadaten und die
Teilnehmerliste. Es enthält keinerlei Markup.

Die Sprecheridentität trägt alle bekannten Bestandteile — Discord-ID,
eingefrorener Discord-Anzeigename und, falls verknüpft, die Kennung beim
Zielsystem. Welche davon im Ergebnis auftauchen und in welcher Form, entscheidet
allein der Adapter.

Der `DocumentSink`-Port umfasst das Anlegen eines Dokuments aus diesem Modell und
die Rückgabe einer aufrufbaren URL. Er kennt weder Collections noch Spaces noch
Dateipfade — diese Begriffe leben in der Konfiguration des jeweiligen Adapters.

### 8.2 Rendering über Jinja2

Jeder Adapter rendert das Transkript-Modell über ein **Jinja2-Template**. Der
Grund ist nicht Konfigurierbarkeit um ihrer selbst willen, sondern dass die
Zielformate keine gemeinsame Darstellung haben: Outline nutzt
`@[Name](mention://user/<id>)`, Confluence `<ac:link><ri:user/></ac:link>`,
Notion strukturiertes JSON, und einfaches Markdown kennt überhaupt keine
Erwähnungen. Ein einziger fest verdrahteter Renderer könnte nur den kleinsten
gemeinsamen Nenner bedienen.

Jeder Adapter bringt ein Standard-Template mit. Ist in `guild_config` ein eigenes
hinterlegt, hat dieses Vorrang.

Dieselbe Engine rendert die Discord-Nachrichten — Aufnahme-Ankündigung,
Consent-Hinweis, Fertigstellungsmeldung mit Dokument-Link —, sodass Wortlaut und
Sprache dieser Texte ohne Codeänderung anpassbar sind.

**Templates laufen in einer `SandboxedEnvironment`.** Jinja2 ist im
Auslieferungszustand keine Sandbox: ein Ausdruck wie `{{ ''.__class__.__mro__ }}`
öffnet den Weg zu beliebiger Codeausführung. Da Templates über einen
Admin-Command gesetzt werden können, wäre ein ungeschütztes Environment
gleichbedeutend mit einer Shell im Bot-Pod für jeden Guild-Administrator. Die
Umgebung erhält ausschließlich das Transkript-Modell und eine feste Menge an
Filtern; nichts, was I/O ausführt.

**Eingesetzte Werte werden zielformat-spezifisch escaped.** Discord-Anzeigenamen
und Transkripttext sind nicht vertrauenswürdig — wer sich `[hier klicken](https://…)`
nennt, injiziert andernfalls einen Link in jedes Protokoll, in dem er auftaucht,
oder bricht mit `](` aus dem umgebenden Markup aus. Jeder Adapter stellt einen
Escape-Filter für sein Format bereit; die Templates verwenden ihn für jeden Wert
aus dem Modell. HTML-Autoescape ist dafür kein Ersatz, weil das Zielformat in
den meisten Fällen kein HTML ist.

### 8.3 Der Outline-Adapter

Ein Dokument pro Session in der konfigurierten Collection, Titel mit Datum und
Uhrzeit, kein H1 am Anfang — der Titel ist in Outline ein eigenes Feld.
Aufeinanderfolgende Segmente desselben Sprechers werden zu einem Block
zusammengefasst.

Verknüpfte Nutzer werden als echte Outline-Erwähnung gerendert und dadurch
benachrichtigt; dahinter steht in Klammern der Discord-Anzeigename, verlinkt auf
das Discord-Profil über die Discord-ID:

```markdown
**14:32:05** · @[Max Mustermann](mention://user/9c8b…) ([maxm](https://discord.com/users/1234…))

Der gesprochene Text dieses Blocks.
```

Ist kein Outline-Account verknüpft, entfällt allein die Erwähnung; die verlinkte
Discord-Identität bleibt:

```markdown
**14:33:11** · [gastnutzer](https://discord.com/users/9876…)
```

Die Discord-ID ist der stabile Anker: Anzeigenamen ändern sich, die ID nicht.
Ein Protokoll bleibt dadurch auch dann zuordenbar, wenn jemand sich umbenannt
oder den Server verlassen hat. Die Namen stammen aus `session_participant` und
sind zum Session-Zeitpunkt eingefroren; aus derselben Quelle entsteht die
Teilnehmerliste am Dokumentkopf.

> **Bei der Implementierung zu verifizieren:** Ob Outline eine Benachrichtigung
> je Erwähnung oder je Dokument und Nutzer erzeugt. Bei einem langen Protokoll
> nennt dieselbe Person unter Umständen hunderte Blöcke — wird pro Erwähnung
> benachrichtigt, ist das unbrauchbar. Fallback in diesem Fall: nur die erste
> Nennung eines Sprechers als Erwähnung rendern, alle weiteren als Klartext mit
> Discord-Link.

### 8.4 Account-Verknüpfung

`/link` erzeugt einen kurzlebigen, signierten State und antwortet ephemeral mit
einer Autorisierungs-URL. Nach Zustimmung ruft der `link-service` den Callback
ab, tauscht den Code gegen ein Token, fragt damit **einmalig** die Identität des
Nutzers ab, speichert die Kennung des Zielsystems samt Anzeigename — und
verwirft das Token.

Es wird kein Zugriffstoken persistiert. Damit entfallen Token-Verschlüsselung,
Refresh-Handling und Revocation vollständig.

Die Verknüpfung ist **je Zielsystem** abgelegt (`provider` in `account_link`),
nicht global. Ein späterer Confluence-Adapter braucht eine eigene Zuordnung; die
Outline-Verknüpfung taugt dafür nicht.

`/link remove` löscht die Zuordnung.

> **Bei der Implementierung zu verifizieren:** Outline läuft in Version 1.9.1 und
> unterstützt OAuth-Applications als Provider. Die exakten Endpunkt-Pfade, die
> Scope-Bezeichnungen und der Endpunkt zur Abfrage der eigenen Identität sind
> gegen die laufende Instanz zu prüfen, statt aus der Dokumentation angenommen zu
> werden.

### 8.5 Veröffentlichung des Links

Weil die Transkription erst nach Session-Ende beginnt und je nach Redezeit
Stunden dauern kann, hat der Channel sich längst geleert, wenn das Dokument
entsteht. Der Bot postet deshalb den Link auf das fertige Dokument in den
Text-Teil des Aufnahme-Channels, sobald es vorliegt — Wortlaut über ein
Jinja2-Template.

Der Worker selbst postet nicht. Er hält weder eine Gateway-Verbindung noch soll
er das Discord-Token besitzen; er setzt die Session auf `documented` und
hinterlegt `document_url`. Der Bot fragt diesen Zustand alle
`publish_poll_seconds` (Default 30) ab, postet die Nachricht und setzt
`announced_at` — das Feld verhindert doppelte Ankündigungen nach einem Neustart.

Abfrage statt `LISTEN`/`NOTIFY`: Die Datenbank wird über PgBouncer erreicht, und
im Transaction-Pooling-Modus werden Benachrichtigungen nicht durchgereicht. Bei
wenigen Sessions pro Tag ist eine Abfrage im 30-Sekunden-Takt die robustere Wahl
gegenüber einer Direktverbindung am Pooler vorbei.

## 9. Datenmodell

PostgreSQL über CloudNativePG, eigene Datenbank nach dem bestehenden
`database/`-Muster des Clusters.

Zugriff ausschließlich über **SQLAlchemy 2.0 im async-Modus** (`DeclarativeBase`,
`Mapped[...]`, `async_sessionmaker`) mit `asyncpg` als Treiber. Roher SQL-Zugriff
neben dem ORM ist ausgeschlossen: im RAG-Bot existieren ORM-Modelle und direkte
`asyncpg`-Zugriffe nebeneinander, was zu zwei parallelen Datenzugriffswegen für
dieselbe Datenbank führt. Sturnus hat genau einen.

Schema-Änderungen laufen über **Alembic**-Migrationen, die beim Start des
`worker` angewandt werden — nicht durch `create_all()` und nicht durch manuelles
DDL. Der RAG-Bot hat keine Migrationen; das ist eine Lücke, kein zu
übernehmendes Muster.

| Tabelle | Inhalt |
|---|---|
| `guild_config` | Laufzeit-Konfiguration je Guild (Abschnitt 10) |
| `account_link` | `discord_user_id` + `provider` (zusammengesetzter PK), `external_user_id`, `display_name`, `linked_at` |
| `consent` | `discord_user_id`, `guild_id`, `granted_at`, `revoked_at`, `policy_version`, `source` |
| `oauth_state` | `state` (PK), `discord_user_id`, `created_at`, `expires_at` |
| `session` | `id`, `guild_id`, `channel_id`, `started_at`, `ended_at`, `end_reason`, `status`, `document_provider`, `document_id`, `document_url`, `announced_at` |
| `session_participant` | `session_id`, `discord_user_id`, `discord_display_name` (zum Session-Zeitpunkt eingefroren), `detected_language`, `first_seen_at` |
| `transcription_job` | `id`, `session_id`, `discord_user_id`, `s3_key`, `encryption_key_id`, `retention_until`, `audio_deleted_at`, `status`, `attempts`, `error`, `transcript` |

Die Queue ist `transcription_job`, konsumiert über
`select(TranscriptionJob).with_for_update(skip_locked=True)`.
Ein Message-Broker wird bewusst nicht eingesetzt: PostgreSQL wird für das
Mapping ohnehin benötigt, und das erwartete Volumen von wenigen Sessions pro Tag
rechtfertigt keine zusätzliche Betriebskomponente.

## 10. Slash-Commands

| Command | Berechtigung | Wirkung |
|---|---|---|
| `/consent` | alle | Ephemeral-Embed mit Datenschutzhinweis und Buttons *Zustimmen* / *Ablehnen*. Bei Zustimmung: Rolle vergeben, Eintrag mit aktueller `policy_version` |
| `/consent revoke` | alle | Rolle entziehen, `revoked_at` setzen |
| `/consent status` | alle | Eigener Einwilligungs- und Verknüpfungsstand |
| `/link` | alle | Ephemeral-Antwort mit Autorisierungs-URL |
| `/link remove` | alle | Verknüpfung löschen |
| `/audio delete` | alle | Eigene Audioaufnahmen sofort löschen, unabhängig von der Aufbewahrungsfrist |
| `/audio purge` | Admin | Aufnahmen eines benannten Nutzers löschen (Art. 17 DSGVO) |
| `/config …` | Admin | Laufzeit-Konfiguration lesen und setzen, einschließlich Templates |

Alle Antworten sind ephemeral. Für Admin-Commands wird das bestehende
`require_admin()`-Muster aus dem RAG-Bot übernommen.

## 11. Konfiguration

Laufzeit-konfigurierbar über die `/config`-Gruppe, gespeichert in
`guild_config`. Bot und Worker teilen sich den Store.

| Schlüssel | Default | Konsument |
|---|---|---|
| `voice_channel_id` | — | Bot |
| `consent_role_id` | — | Bot |
| `empty_grace_seconds` | 60 | Bot |
| `idle_timeout_minutes` | 15 | Bot |
| `max_session_hours` | 4 | Bot |
| `publish_poll_seconds` | 30 | Bot |
| `document_provider` | `outline` | Worker |
| `document_target` | — | Worker |
| `document_template` | Adapter-Standard | Worker |
| `message_templates` | mitgeliefert | Bot |
| `audio_retention_days` | 30 | Worker |
| `policy_version` | — | beide |
| `policy_url` | — | beide |

Nicht laufzeit-konfigurierbar, sondern über Umgebungsvariablen: Whisper-Modell,
Standardsprache als Rückfall der Autodetektion, maximale Fehlversuche je
Transkriptions-Job (Default 3), Datenbank- und S3-Verbindung, Tokens sowie der
Hauptschlüssel für die Audio-Verschlüsselung.

`document_target` ist bewusst allgemein benannt: beim Outline-Adapter ist es eine
Collection-ID, bei einem künftigen Confluence-Adapter ein Space-Key, bei einer
Dateiablage ein Pfad. Die Auslegung obliegt dem Adapter.

## 12. Aufbewahrung und Schutz der Aufnahmen

Audioaufnahmen werden **nicht sofort nach der Transkription gelöscht**, sondern
für `audio_retention_days` aufbewahrt (Default 30). Der Zweck ist die erneute
Verarbeitung: eine fehlgeschlagene oder erkennbar schlechte Transkription lässt
sich sonst nicht wiederholen, und ein verbessertes Modell kann nicht auf
Bestandsmaterial angewandt werden.

Diese Verlängerung ist der sicherheitskritischste Teil des Systems. Rohes Audio
aus privaten Gesprächen ist erheblich sensibler als das daraus erzeugte
Transkript, und es liegt nun über Wochen statt Minuten.

### 12.1 Schutzmaßnahmen

- **Verschlüsselung vor dem Upload.** Der Bot verschlüsselt jede Aufnahme lokal
  mit AES-256-GCM, bevor sie den Pod verlässt. Wer Zugriff auf den Objektspeicher
  erlangt, erhält damit keine hörbaren Daten. Serverseitige Verschlüsselung
  allein wäre dafür unzureichend, weil sie gegenüber dem Speicherbetreiber und
  einem gestohlenen Zugangsschlüssel nicht schützt.
- **Umschlagverfahren.** Je Session wird ein eigener Datenschlüssel erzeugt und
  mit dem Hauptschlüssel aus SOPS verschlüsselt neben dem Job abgelegt;
  `encryption_key_id` verweist auf den verwendeten Hauptschlüssel. Ein Wechsel
  des Hauptschlüssels erfordert dadurch keine Neuverschlüsselung des
  Bestandsmaterials.
- **Eigener Bucket mit eigenen Zugangsdaten**, getrennt von allen übrigen
  Anwendungen, ohne öffentlichen Zugriff. Der Bot schreibt, der Worker liest und
  löscht; niemand sonst besitzt die Zugangsdaten.
- **Kein Object Lock.** Eine Unveränderlichkeitssperre stünde im Widerspruch zur
  Löschpflicht aus Art. 17 DSGVO — sie würde genau die Löschung verhindern, die
  auf Anfrage möglich sein muss.

### 12.2 Durchsetzung der Aufbewahrungsfrist

Beim Anlegen eines Jobs wird `retention_until` gesetzt. Ein periodischer Lauf im
Worker löscht abgelaufene Objekte und vermerkt `audio_deleted_at`. Eine
Lebenszyklusregel des Buckets greift zusätzlich als zweite Ebene, damit ein
dauerhaft ausgefallener Worker nicht zu unbegrenzter Aufbewahrung führt.

Die Regel allein genügt nicht: Aufbewahrung muss auch in der Datenbank
nachvollziehbar sein, und `audio_deleted_at` ist der Nachweis, dass gelöscht
wurde.

### 12.3 Löschung auf Anfrage

`/audio delete` löscht die eigenen Aufnahmen aller Sessions unmittelbar,
unabhängig von der Frist. Für Administratoren erledigt `/audio purge` dasselbe
für einen benannten Nutzer, um Auskunfts- und Löschersuchen nach Art. 17 DSGVO
bedienen zu können.

Bereits erstellte Transkripte bleiben davon unberührt — sie sind ein eigenes
Verarbeitungsergebnis und liegen im Dokumentsystem, nicht bei Sturnus.

### 12.4 Übrige Daten

- Die lokale Aufnahmedatei auf dem PVC wird nach erfolgreichem Upload entfernt.
- **`consent`-Einträge bleiben dauerhaft erhalten**, auch nach Widerruf — das ist
  die Nachweispflicht aus Art. 7 Abs. 1 DSGVO; `revoked_at` dokumentiert den
  Widerruf, statt den Nachweis zu löschen.
- `account_link` ist auf Nutzerwunsch löschbar.
- Weder Audiodaten noch Transkriptinhalte erscheinen in Logs.
- Fertige Transkripte unterliegen dem Lebenszyklus des Zielsystems und werden von
  Sturnus nicht weiter verwaltet.

## 13. Deployment

Neues Repository nach OLF-Standard: Helm-Chart unter `charts/`, Images über die
reusable workflows in Version `@v2.3.0` nach GHCR, Release-Please für Versionen
und Changelog, zentrales Renovate-Preset, Trunk-Based Development mit
Conventional Commits.

Cluster-seitig im Kubernetes-FLUX-Repository:

- `apps/base/sturnus/` und `apps/clusters/feathre-core/base-apps/sturnus/`
- CloudNativePG-Datenbank nach dem bestehenden `database/`-Muster
- `ObjectBucketClaim` für den Audio-Bucket nach dem Muster von `outline.yaml`
- Secrets über SOPS: Discord-Token, Outline-Service-Token, OAuth-Client-Secret
- Ingress ausschließlich für `link-service`, über Cloudflare Tunnel

Für `bot` wird ein PodDisruptionBudget gesetzt, das ungewollte Evictions während
laufender Sessions begrenzt, sowie ein **RWO-PVC für die laufende Aufnahme**
(Abschnitt 6.3). Dessen Größe folgt aus `max_session_hours` und der erwarteten
Sprecherzahl — bei vier Stunden und zehn Sprechern rund 5 GB. Da das PVC den Pod
an eine Zone bindet, wird `bot` per Node-Affinität an die Region gepinnt, wie es
die übrigen zustandsbehafteten Anwendungen im Cluster ebenfalls tun.

Ressourcen: `bot` 1 CPU, `link-service` minimal, `worker` 4 CPU mit
Speicheranforderung passend zum gewählten Modell (rund 2 GB für
`large-v3-turbo` in int8, zuzüglich Puffer).

## 14. Teststrategie

`pytest` mit `pytest-asyncio`, analog zum RAG-Bot.

Ohne Discord-Abhängigkeit unit-testbar und entsprechend als reine Funktionen
beziehungsweise Klassen geschnitten:

- Session-State-Machine mit injizierter Uhr — sämtliche Übergänge aus
  Abschnitt 5.1
- Zeitrekonstruktion aus RTP-Timestamps, einschließlich SSRC-Wechsel
- Sprachdetektion: Festschreibung je Sprecher, Rückfall auf die Standardsprache
- Wiederaufnahme verwaister Aufnahmen vom PVC nach einem Absturz
- Einmaligkeit der Link-Ankündigung über `announced_at`
- Zusammenführung der Segmente über alle Sprecher hinweg
- Rendering des Transkript-Modells über Jinja2, inklusive Sprecherzeile mit und
  ohne verknüpften Account
- **Sandbox-Ausbruch:** ein Template mit `{{ ''.__class__… }}` und verwandten
  Ausdrücken muss fehlschlagen, nicht ausgeführt werden
- **Escaping:** ein Anzeigename wie `[klick](https://boese.example)` darf im
  erzeugten Dokument keinen Link ergeben
- Verschlüsselung und Entschlüsselung einer Aufnahme im Umschlagverfahren
- Aufbewahrungslauf: löscht abgelaufene Objekte, lässt laufende unberührt, setzt
  `audio_deleted_at`
- Consent-Auflösung einschließlich des Administrator-Bypass-Falls

PostgreSQL über Testcontainers — die Repositories werden gegen eine echte
Datenbank geprüft, nicht gegen Fakes. Whisper in Unit-Tests über den
`TranscriptionEngine`-Port gefaked, ergänzt um einen Integrationstest mit einer
kurzen echten Audiodatei, damit die Modellanbindung nicht nur theoretisch
funktioniert.

**Ein Architektur-Test setzt die Abhängigkeitsregel aus Abschnitt 4.4 durch:** er
prüft die Import-Graphen und schlägt fehl, sobald `domain` etwas aus
`application`, `infrastructure` oder einer I/O-Bibliothek importiert. Eine
Schichtungsregel, die nur in der Dokumentation steht, wird nach wenigen Monaten
verletzt sein; als Test ist sie eine Zusicherung.

Voice-Receive selbst bleibt ein dünner Adapter ohne eigene Tests — die Logik
liegt bewusst außerhalb.

## 15. Offene Risiken

- **`discord-ext-voice-recv` ist eine Community-Extension** ohne offizielle
  Unterstützung durch discord.py. Bricht Discord das Voice-Protokoll, hängt die
  Behebung an einem Drittprojekt. Der Adapter wird deshalb bewusst dünn gehalten,
  damit ein Wechsel der Bibliothek die Kernlogik nicht berührt.
- **Das Sizing des Workers ist geschätzt.** Die Angabe von rund 1× Realtime für
  `large-v3-turbo` auf 4 Kernen ist vor dem Rollout an echtem Material zu messen;
  der Fallback auf `small` ist eingeplant.
- **Der Bot ist ein Singleton ohne Übernahme, und eine Session ist unteilbar.**
  Bei geordnetem Neustart schließt der SIGTERM-Handler die Aufnahme; bei einem
  harten Kill übernimmt die Wiederaufnahme vom PVC beim nächsten Start
  (Abschnitt 6.3). Fällt beides aus — etwa bei Verlust des Volumes — ist die
  gesamte Session verloren, nicht nur ein Ausschnitt. Das ist der bewusst
  akzeptierte Preis dafür, die Aufnahme nicht zu unterteilen. Ein
  PodDisruptionBudget begrenzt ungewollte Evictions, verhindert aber keinen Deploy.
- **Die Latenz ist erheblich und wächst mit der Redezeit.** Ein langer Abend im
  Voice-Channel liefert sein Protokoll erst Stunden später. Sollte sich das im
  Betrieb als untragbar erweisen, ist die Unterteilung der Aufnahme in Abschnitte
  der Weg zurück — sie kostet Transkriptionsqualität an jeder Schnittstelle.
- **Die Outline-OAuth-Details sind unverifiziert** (siehe Abschnitt 8.4) und vor
  der Implementierung des Link-Flows gegen die laufende Instanz zu prüfen.
- **Die Jinja2-Sandbox ist eine Hürde, keine Garantie.** Für
  `SandboxedEnvironment` sind in der Vergangenheit Ausbrüche bekannt geworden.
  Die Möglichkeit, Templates zu setzen, bleibt deshalb auf Administratoren
  beschränkt, Änderungen werden protokolliert, und die Jinja2-Version gehört zu
  den sicherheitsrelevanten Abhängigkeiten, die Renovate zeitnah aktualisiert.
- **Die verlängerte Aufbewahrung vergrößert den Schaden einer Kompromittierung.**
  Wochenlang vorgehaltenes Rohaudio privater Gespräche wiegt schwerer als jedes
  andere Datum in diesem System. Die Verschlüsselung vor dem Upload begrenzt das
  auf die Fälle, in denen zugleich der Hauptschlüssel abfließt — womit dessen
  Handhabung zum eigentlichen Schutzgut wird. Eine kürzere Frist bleibt jederzeit
  die wirksamste Gegenmaßnahme.
- **Ein zweiter Adapter wird die Portgrenze prüfen.** Der Zuschnitt von
  `DocumentSink` beruht bislang auf einer einzigen Implementierung; Confluence
  oder Notion können Anforderungen mitbringen — Blockstrukturen statt Text,
  Pflichtfelder, andere Erwähnungsmodelle —, die eine Anpassung des
  Transkript-Modells nötig machen. Das ist erwartbar und der Grund, jetzt nicht
  mehr als einen Adapter zu bauen.
