# Spec Delta

## Purpose

How a recorded speaker track becomes timed segments: where the audio is sent, how
a track too long for one request is divided, which returned segments are refused
as invented, how a track's language is decided, and what a person is told about
the destination before they consent to be recorded.

## ADDED Requirements

### Requirement: Transcription runs against a configured remote endpoint

The system SHALL transcribe audio by sending it to an OpenAI-compatible
`/audio/transcriptions` endpoint named by configuration, and SHALL NOT transcribe
locally. The endpoint address MUST have no default value, so that audio can never
reach a third party merely because nobody chose otherwise.

#### Scenario: No endpoint configured

- **WHEN** a worker is started without a transcription endpoint configured
- **THEN** it fails to start and reports the missing configuration
- **AND** no transcription job is claimed

#### Scenario: Endpoint configured

- **WHEN** a transcription job runs and an endpoint is configured
- **THEN** the speaker's audio is sent to that endpoint over TLS
- **AND** the transcript is assembled from that endpoint's response

#### Scenario: Endpoint unreachable or failing

- **WHEN** the endpoint times out or answers with a server error
- **THEN** the job fails rather than producing a partial transcript
- **AND** the failure counts as one attempt, so the queue retries it and
  eventually moves it to the dead state

### Requirement: A track too large for one request is divided at silence

A speaker's track can exceed the endpoint's request size limit, so the system
SHALL divide it into windows of at most a configured duration. Every cut MUST fall
inside a stretch that speech detection reported as silence. Silence inside a
window MUST be preserved rather than removed, because removing it would make a
returned segment's times refer to a timeline the recording does not have.

#### Scenario: Track fits in one window

- **WHEN** a track is shorter than the configured window cap
- **THEN** it is sent as a single request

#### Scenario: Track longer than the window cap

- **WHEN** a track exceeds the window cap
- **THEN** it is divided into windows no longer than the cap
- **AND** every cut falls inside a silence between two detected speech clips
- **AND** the silence inside each window remains in the audio that is sent

#### Scenario: No silence available at the cap

- **WHEN** a window reaches the cap with no silence to cut at
- **THEN** the cut is made at the cap regardless, through whatever is being spoken
- **AND** the cut is logged, so a broken sentence in a transcript is explicable
  from the worker's output

### Requirement: Segment times refer to the moment of speech

The system SHALL report each segment's start and end relative to the start of the
speaker's recording, not relative to the request the segment came back from. A
segment whose reported end overruns its window MUST be clamped to that window
rather than allowed to claim time belonging to the next one.

#### Scenario: Offsets across several windows

- **WHEN** a track was divided into three or more windows
- **THEN** each returned segment's times are shifted by its own window's start
- **AND** the resulting times increase monotonically across the whole track

#### Scenario: Segment overruns its window

- **WHEN** a returned segment's end lies beyond the end of its window
- **THEN** the end is clamped to the window's extent
- **AND** the segment is not attributed to the following window

### Requirement: Silence is never sent for transcription

Padding between voice packets is bit-exact zero and is the most reliable way to
make a speech model invent text, so the system SHALL NOT send audio in which no
speech was detected.

#### Scenario: Track with no detected speech

- **WHEN** speech detection finds no clip anywhere in a speaker's track
- **THEN** no request is made to the endpoint
- **AND** an empty result is produced and the track is recorded as skipped

#### Scenario: Silent window inside a track

- **WHEN** one window of a multi-window track contains no detected speech
- **THEN** that window is not sent
- **AND** the windows around it are still sent, with their offsets unaffected

### Requirement: Invented speech is refused after the response

Endpoint responses carry per-segment confidence figures, and the system SHALL use
them to drop segments the model invented. A segment MUST be dropped when its
no-speech probability exceeds 0.6, and independently when its compression ratio
exceeds 2.4. A segment's average log-probability MUST NOT be used as a condition:
an invented line is a fluent, high-probability sequence, so a log-probability
condition switches off exactly the guard aimed at it.

#### Scenario: Invented line with high confidence

- **WHEN** a segment reports a no-speech probability of 0.7 and an average
  log-probability of −0.88
- **THEN** the segment is dropped
- **AND** the drop is logged with the segment's own figures

#### Scenario: Repetitive output

- **WHEN** a segment reports a compression ratio above 2.4
- **THEN** the segment is dropped, whatever its no-speech probability

#### Scenario: Endpoint omits a confidence field

- **WHEN** a response omits the no-speech probability, the compression ratio, or
  both
- **THEN** the rule that depends on the missing field does not apply
- **AND** the segments are kept and the job succeeds

### Requirement: One speaker's track keeps one language

The system SHALL report languages as ISO-639-1 codes, and the language decided for
a track's first window SHALL govern every later window of that track, so that a
quieter stretch cannot change a speaker's language halfway through.

#### Scenario: Language pinned across windows

- **WHEN** the first window of a track resolves to a language
- **THEN** that language is supplied to every later window of the same track

#### Scenario: Endpoint reports a language name

- **WHEN** a response names a language in English rather than as a code
- **THEN** it is normalised to the corresponding ISO-639-1 code

#### Scenario: Unrecognised language

- **WHEN** a response names a language that cannot be mapped to a code
- **THEN** the configured default language is used
- **AND** the job succeeds

### Requirement: Only a known model may run, and it must return segment times

A job records which model produced it, so the system SHALL refuse a model outside
its known set rather than silently substituting one. A model that cannot return
per-segment timestamps MUST NOT be usable, because the protocol interleaves
several speakers chronologically and a transcript without segment times is no
result at all.

#### Scenario: Unknown model requested

- **WHEN** a job requests a model outside the known set
- **THEN** the job is refused before any audio is uploaded

#### Scenario: Job without a requested model

- **WHEN** a job names no model
- **THEN** the configured default model runs

### Requirement: The deployment discloses where audio is sent

Consent covers being recorded by Sturnus; it does not by itself cover the
recording being sent elsewhere. Where a deployment sends audio SHALL therefore be
visible before a person consents and on request afterwards. The endpoint's
credentials MUST NOT appear in any user-facing text, log field, span attribute or
metric label.

#### Scenario: Consent prompt

- **WHEN** a person is shown the consent prompt
- **THEN** the prompt names the host audio is sent to, beside the policy link

#### Scenario: Deployment information requested

- **WHEN** a user asks the bot what this deployment is
- **THEN** the answer names the host audio is sent to, beside name, version,
  licence and source offer

#### Scenario: Credentials

- **WHEN** any of those texts, or any log line, span or metric, is produced
- **THEN** it contains the host but never the API key

### Requirement: Progress and measurements stay reportable

The system SHALL keep reporting audio duration, speech duration and segment count
per job, measured on the recording rather than taken from the endpoint's answer.
Progress SHALL advance once per completed window. This makes the stall detector
coarser — a window is one request with no intermediate signal — and an operator
MUST be told so rather than discovering it.

#### Scenario: Measurements after a job

- **WHEN** a transcription job completes
- **THEN** the reported audio and speech durations are those speech detection
  measured, and the segment count is that of the assembled transcript

#### Scenario: Progress during a multi-window job

- **WHEN** a window's request completes
- **THEN** progress advances by that window
- **AND** no progress is reported while a window's request is in flight
