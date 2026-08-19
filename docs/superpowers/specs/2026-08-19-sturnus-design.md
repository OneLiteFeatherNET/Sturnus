# Sturnus — Design

Discord-Voice-Transkription mit Outline-Ablage für OneLiteFeather.

Status: Entwurf zur Review · Datum: 2026-08-19

## 1. Ziel

Ein dedizierter Discord-Voice-Channel wird automatisch aufgezeichnet und als
chronologisches Transkript in Outline abgelegt. Sprecher erscheinen im Dokument
als echte Outline-Nutzer, sofern sie ihren Discord-Account einmalig mit ihrem
Outline-Account verknüpft haben.

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
- **Kein Live-Transkript.** Die Transkription läuft nachgelagert in Chunks,
  nicht in Echtzeit.
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
diesem Moment verworfen; bereits geflushte Chunks bleiben unberührt, da der
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
Chunk-Erzeugung, S3-Upload, Job-Enqueue. Health- und Metrics-Endpunkte auf einem
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

Aufgaben: Chunk-Jobs aus der Queue ziehen, transkribieren, Chunk-Audio löschen;
bei Session-Abschluss die Chunk-Transkripte zusammenführen und das
Outline-Dokument anlegen.

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
    transcribe_chunk.py
    link_account.py
  infrastructure/  Adapter auf konkrete Technik
    discord/         Voice-Receive-Adapter, Cogs
    db/              ORM-Modelle, Repositories, Migrationen
    objectstore/     S3
    whisper/         faster-whisper
    outline/         Outline-API-Client
```

**Die Abhängigkeitsregel:** `domain` importiert weder `application` noch
`infrastructure` und keine Fremdbibliothek mit I/O — kein `discord`, kein
`sqlalchemy`, kein `boto3`. `application` kennt nur `domain` und die eigenen
Ports, niemals einen konkreten Adapter.

Das ist kein Selbstzweck. Die gesamte Logik, die dieses Projekt schwierig macht
— Zeitrekonstruktion, Session-Übergänge, Chunk-Merge — liegt damit in Code, der
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
| `DocumentSink` | Outline-API in Tests gefaked |
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

`CLOSING` flusht den letzten Chunk, setzt die Session auf `closed` und verlässt
den Channel.

Eine Session entspricht genau einem Outline-Dokument.

### 5.3 Abschluss und Dokumenterstellung

Der Bot erzeugt keinen gesonderten Abschluss-Job. Stattdessen prüft der Worker
nach jedem erfolgreich transkribierten Chunk, ob dessen Session bereits `closed`
ist und kein weiterer Chunk-Job dieser Session mehr offen ist. Trifft beides zu,
führt derselbe Durchlauf den Merge aus und legt das Dokument an.

Dieser Weg ist gegenüber einem separaten Abschluss-Job vorzuziehen, weil er ohne
Reihenfolgegarantie zwischen Bot und Worker auskommt: es ist gleichgültig, ob
der letzte Chunk vor oder nach dem Schließen der Session fertig wird. Die Prüfung
läuft in derselben Transaktion wie der Statuswechsel des Chunks, damit bei
gleichzeitig endenden Chunks nicht zwei Dokumente entstehen.

### 5.2 Testbarkeit

Die State-Machine ist als reine Klasse mit **injizierter Uhr** implementiert und
kennt weder Discord noch Datenbank. Genau der Teil, der sich sonst nur mit
echten Personen in einem Voice-Channel prüfen ließe, wird damit deterministisch
unit-testbar.

## 6. Audio-Pipeline

### 6.1 Erfassung

Eingehende Opus-Pakete werden pro Nutzer **sofort beim Empfang auf 16 kHz Mono
PCM** dekodiert — das Zielformat von Whisper. Ein späteres Resampling entfällt.

Stille wird anhand der RTP-Timestamps **aufgefüllt**. Alle Sprecher-Puffer eines
Chunks sind dadurch gleich lang und exakt synchron, was das Zusammenführen zu
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

### 6.3 Chunking

Alle `chunk_interval_minutes` (Default 10) wird pro Nutzer der Puffer als WAV
nach S3 geschrieben und ein Chunk-Job eingereiht.

Der Grund ist die Auto-Join-Charakteristik: ein Channel, in dem abends jemand
sechs Stunden verbringt, ergäbe bei durchgehendem Padding mehrere Gigabyte im
Pod. Chunking hält den Pod-Speicher konstant, begrenzt das Padding pro Chunk auf
wenige Dutzend Megabyte und lässt die Transkription **parallel zur laufenden
Session** arbeiten — das Dokument ist Minuten nach Session-Ende fertig statt
Stunden später.

**Chunk-Grenzen respektieren Sprechpausen.** Eine harte Grenze mitten im Wort
zerschneidet die Transkription. Der Flush wird deshalb bis zur nächsten
Sprechpause des jeweiligen Nutzers verzögert, maximal jedoch um 60 Sekunden;
danach wird hart geschnitten.

## 7. Transkription

`faster-whisper` mit `large-v3-turbo` in int8 als Default. Turbo ist ein
destillierter Decoder, läuft auf 4 Kernen bei etwa 1× Realtime bei rund 1,6 GB
RAM und liefert für Deutsch deutlich bessere Ergebnisse als `small`. `small`
bleibt als konfigurierbarer Fallback, falls das Sizing im Betrieb nicht aufgeht.

Sprache wird auf Deutsch gepinnt (`language="de"`), nicht automatisch erkannt —
Autodetektion auf kurzen Segmenten ist unzuverlässig und kann innerhalb einer
Session zwischen Sprachen springen.

`vad_filter=True` überspringt die aufgefüllte Stille.

Jeder Nutzer-Stream wird einzeln transkribiert. Die resultierenden Segmente
tragen Offsets relativ zum Chunk-Start, die über die Chunk-Startzeit in absolute
Zeit umgerechnet werden.

## 8. Outline-Integration

### 8.1 Account-Verknüpfung

`/link` erzeugt einen kurzlebigen, signierten State und antwortet ephemeral mit
einer Autorisierungs-URL. Nach Zustimmung in Outline ruft der `link-service` den
Callback ab, tauscht den Code gegen ein Token, fragt damit **einmalig** die
Identität des Nutzers ab, speichert `outline_user_id` und Anzeigename — und
verwirft das Token.

Es wird kein Zugriffstoken persistiert. Damit entfallen Token-Verschlüsselung,
Refresh-Handling und Revocation vollständig.

`/link remove` löscht die Zuordnung.

> **Bei der Implementierung zu verifizieren:** Outline läuft in Version 1.9.1 und
> unterstützt OAuth-Applications als Provider. Die exakten Endpunkt-Pfade, die
> Scope-Bezeichnungen und der Endpunkt zur Abfrage der eigenen Identität sind
> gegen die laufende Instanz zu prüfen, statt aus der Dokumentation angenommen zu
> werden.

### 8.2 Dokumenterstellung

Bei Session-Abschluss führt der Worker alle Chunk-Transkripte aller Sprecher
nach absoluter Zeit zusammen und rendert Markdown:

- Ein Dokument pro Session in der konfigurierten Collection
  (`outline_collection_id`), Titel mit Datum und Uhrzeit.
- Aufeinanderfolgende Segmente desselben Sprechers werden zu einem Block
  zusammengefasst, statt jedes Segment einzeln zu listen.
- Kein H1 am Dokumentanfang — der Titel ist in Outline ein eigenes Feld.

**Sprecherzeile.** Jeder Block wird mit Zeitstempel und Sprecher eingeleitet.
Verknüpfte Nutzer werden als echte Outline-Mention gerendert und dadurch
benachrichtigt; dahinter steht in Klammern der Discord-Anzeigename, verlinkt auf
das Discord-Profil über die Discord-ID:

```markdown
**14:32:05** · @[Max Mustermann](mention://user/9c8b…) ([maxm](https://discord.com/users/1234…))

Der gesprochene Text dieses Blocks.
```

Ist kein Outline-Account verknüpft, entfällt allein die Mention; die verlinkte
Discord-Identität bleibt:

```markdown
**14:33:11** · [gastnutzer](https://discord.com/users/9876…)
```

Die Discord-ID ist der stabile Anker: Anzeigenamen ändern sich, die ID nicht.
Ein Protokoll bleibt dadurch auch dann zuordenbar, wenn jemand sich umbenannt
oder den Server verlassen hat.

**Anzeigenamen werden zum Session-Zeitpunkt eingefroren.** Der Worker schlägt sie
nicht beim Rendern nach, sondern liest sie aus `session_participant` — sonst
zeigte ein altes Protokoll die heutigen Namen. Aus derselben Tabelle wird eine
Teilnehmerliste an den Dokumentkopf gerendert.

> **Bei der Implementierung zu verifizieren:** Ob Outline eine Benachrichtigung
> je Mention oder je Dokument und Nutzer erzeugt. Bei einem langen Protokoll
> nennt dieselbe Person unter Umständen hunderte Blöcke — wird pro Mention
> benachrichtigt, ist das unbrauchbar. Fallback in diesem Fall: nur die erste
> Nennung eines Sprechers als Mention rendern, alle weiteren als Klartext mit
> Discord-Link.

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
| `account_link` | `discord_user_id` (PK), `outline_user_id`, `display_name`, `linked_at` |
| `consent` | `discord_user_id`, `guild_id`, `granted_at`, `revoked_at`, `policy_version`, `source` |
| `oauth_state` | `state` (PK), `discord_user_id`, `created_at`, `expires_at` |
| `session` | `id`, `guild_id`, `channel_id`, `started_at`, `ended_at`, `end_reason`, `status`, `outline_document_id` |
| `session_participant` | `session_id`, `discord_user_id`, `discord_display_name` (zum Session-Zeitpunkt eingefroren), `first_seen_at` |
| `chunk_job` | `id`, `session_id`, `discord_user_id`, `seq`, `starts_at`, `s3_key`, `status`, `attempts`, `error`, `transcript` |

Die Queue ist `chunk_job`, konsumiert über
`select(ChunkJob).with_for_update(skip_locked=True)`.
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
| `/config …` | Admin | Laufzeit-Konfiguration lesen und setzen |

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
| `chunk_interval_minutes` | 10 | Bot |
| `outline_collection_id` | — | Worker |
| `policy_version` | — | beide |
| `policy_url` | — | beide |

Nicht laufzeit-konfigurierbar, sondern über Umgebungsvariablen: Whisper-Modell,
Sprache, maximale Fehlversuche je Chunk-Job (Default 3), Datenbank- und
S3-Verbindung, Tokens.

## 12. Löschkonzept

- **Chunk-Audio** wird unmittelbar nach erfolgreicher Transkription des jeweiligen
  Chunks aus S3 gelöscht. Im Normalbetrieb liegt nie mehr als ein
  Chunk-Zeitfenster gespeichert.
- Eine **S3-Lifecycle-Regel mit 48 Stunden** greift als Rückfallebene für
  verwaiste Objekte aus fehlgeschlagenen Jobs.
- Ein Job wechselt nach einer konfigurierten Zahl von Fehlversuchen in einen
  Dead-Letter-Status und löst einen Alert aus.
- **`consent`-Einträge bleiben dauerhaft erhalten**, auch nach Widerruf — das ist
  die Nachweispflicht aus Art. 7 Abs. 1 DSGVO; `revoked_at` dokumentiert den
  Widerruf, statt den Nachweis zu löschen.
- `account_link` ist auf Nutzerwunsch löschbar.
- Weder Audiodaten noch Transkriptinhalte erscheinen in Logs.
- Fertige Transkripte unterliegen dem Lebenszyklus von Outline und werden von
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
laufender Sessions begrenzt.

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
- Chunk-Grenzen an Sprechpausen
- Zusammenführung der Segmente über Sprecher und Chunks hinweg
- Markdown- und Mention-Rendering, inklusive Sprecherzeile mit und ohne
  verknüpften Outline-Account
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
- **Der Bot ist ein Singleton ohne Übernahme.** Ein Neustart während einer
  laufenden Session verliert den noch nicht geflushten Puffer, also bis zu ein
  Chunk-Intervall. Bereits hochgeladene Chunks bleiben erhalten. Ein PodDisruptionBudget
  begrenzt ungewollte Evictions, verhindert aber keinen Deploy.
- **Die Outline-OAuth-Details sind unverifiziert** (siehe Abschnitt 8.1) und vor
  der Implementierung des Link-Flows gegen die laufende Instanz zu prüfen.
