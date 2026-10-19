/**
 * Where a guild publishes its protocols, as far as the console is allowed
 * to decide it.
 *
 * `document_target` used to be the whole answer: one Outline collection
 * per guild, one document per meeting, configured as a text field on Bot
 * Settings beside twenty other text fields. A guild can now name several
 * destinations in several formats, and none of that was reachable from the
 * interface. This module holds every decision the destinations page makes,
 * so that the page itself is layout and request plumbing — the rule the
 * rest of `app/utils` already follows, and the reason those rules can be
 * tested without mounting anything.
 *
 * Four things here are worth arguing for rather than reading past.
 *
 * **1. This module holds no list of formats any more. It is told.**
 * `GET /api/export-formats` reads `sturnus.application.export_formats`
 * out — every format the deployment knows of, whether this build can run
 * it, and which sink family carries it — and {@link parseFormats} is what
 * arrives. The one thing kept here is *words*: {@link formatLabelKey} maps
 * a name to a translation key, because a sentence in German is not
 * something an API with no message catalogue can serve, and a name with no
 * key falls back to rendering itself.
 *
 * That inversion is what settles the question #150 could only settle one
 * way. It argued that `pdf` and `confluence` should be **absent** from the
 * picker rather than present and disabled, and the decisive half of the
 * argument was that *the console could not see the registry*: a "PDF — not
 * built" row would have been this console asserting a fact about a build it
 * cannot inspect, and it would have gone on asserting it after `pdf` was
 * built. Neither is true now. The row's text is the deployment's own answer
 * and it stops being unavailable the moment the deployment says so, with
 * nothing here to edit.
 *
 * So an unavailable format is **rendered, disabled, and labelled with the
 * reason**. The other half of #150's argument — a dropdown row a save would
 * refuse is "a trap laid under the cursor" — is an argument against a row
 * that looks choosable and is not. This one does not: `UiOption.disabled`
 * is a first-class state that {@link stepEnabled} walks past and that the
 * control renders as unchoosable, and {@link draftProblems} refuses a draft
 * naming an unavailable format so that Save is off and the reason sits
 * under the field. What is left is the fact that PDF exists and this
 * deployment does not build it — which is exactly what somebody who came to
 * this page looking for PDF needs to be told, and what an absent row leaves
 * them to conclude wrongly. The one precedent that cuts the other way,
 * `video_consent_offered`, is a guild's *policy choice* about its own
 * server; this is a property of the binary, which is not something an
 * administrator can go and change.
 *
 * **2. A stored format the API does not report is still rendered, and
 * still choosable while editing that destination.** Exactly the rule
 * `~/utils/directory` applies to a channel id with no row in the mirror:
 * an option that is not rendered is a configuration nobody can remove from
 * this page, and a picker that silently dropped `pdf` would rewrite it to
 * `outline` the next time somebody renamed the destination.
 *
 * The endpoint answering makes this *more* important rather than less,
 * because there is now a way for the list to arrive empty: the request
 * failing. A page that read "this deployment supports nothing" out of a
 * request that timed out and then rewrote every destination on the next
 * save would be the worst version of this screen there is. So the stored
 * value is never filtered against the reported list, anywhere: not in the
 * picker, not in {@link primaryTarget}, not in {@link targetSummary}.
 *
 * **3. The first enabled destination is the one Discord announces.**
 * `sturnus.application.exporting.destinations_for` orders by id, oldest
 * first, and `session.document_url` is stamped from that one alone. That
 * is not a detail of the page's sorting — it is which link lands in the
 * channel — so {@link orderTargets} sorts by id and never by name, and
 * {@link primaryTarget} says which one it is out loud.
 *
 * **4. The credential is write-only, and this module offers no shape that
 * could carry one.** {@link ExportTarget} has `hasSecret` and nothing else
 * about it, because the API's read model has `has_secret` and nothing else
 * about it; {@link TargetDraft} — what the destination form edits and
 * submits — has no secret field at all, which is what makes it impossible
 * for saving a rename to clear a token. See {@link secretState}.
 *
 * Every sentence here is a translation key or a {@link Message}, never
 * prose: a pure function returns data. See `i18n/README.md`.
 */
import type { Message } from './message'
import type { UiOption } from './uiOption'

/* -------------------------------------------------------------------- */
/* What the API sends                                                    */
/* -------------------------------------------------------------------- */

/**
 * One destination, as the console sees it.
 *
 * `id` is a number and `guildId` is a string, and the difference is the
 * API's rather than an inconsistency: a Discord snowflake exceeds
 * JavaScript's safe integer range, where a JSON number silently loses its
 * last digits, and this row's own key is a `SERIAL` that will not reach
 * 2^53.
 *
 * **There is nowhere here to put a credential.** `hasSecret` is the whole
 * of what any response says about one, and this interface is shaped that
 * way on purpose: a field that *could* hold a token is a field somebody
 * eventually binds to an input.
 */
export interface ExportTarget {
  id: number
  guildId: string
  format: string
  name: string
  target: string
  /** Whatever else a format needs. None of the three formats this
   *  deployment publishes needs anything, so it is read, carried and
   *  never edited — dropping it would send `{}` back on the next rename
   *  and erase a key some later format put there. */
  config: Record<string, unknown>
  /** That a credential is stored. Never the credential. */
  hasSecret: boolean
  enabled: boolean
  /** ISO-8601, or `null` when the API sent something unreadable. */
  createdAt: string | null
  updatedAt: string | null
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function asText(value: unknown): string | null {
  if (value === null || value === undefined) return null
  return typeof value === 'string' ? value : String(value)
}

/**
 * One destination from a payload, or `null` when it has no id.
 *
 * Tolerant of everything else, the way `parseDirectory` is: a row with no
 * name is kept and shows as its own id, because a destination that exists
 * and renders as a blank is one nobody can switch off. A row with no id is
 * dropped, because nothing can be done to it — every write addresses it by
 * that id.
 */
export function parseTarget(payload: unknown): ExportTarget | null {
  if (!isRecord(payload)) return null
  // `null` is checked before the conversion, because `Number(null)` is 0
  // and 0 is a safe integer -- a row with a null id would otherwise become
  // a destination every write addressed as `/export-targets/0`.
  if (payload.id === null || payload.id === undefined) return null
  const id = Number(payload.id)
  if (!Number.isSafeInteger(id)) return null
  return {
    id,
    guildId: asText(payload.guild_id) ?? '',
    format: asText(payload.format) ?? '',
    name: asText(payload.name) ?? '',
    target: asText(payload.target) ?? '',
    config: isRecord(payload.config) ? payload.config : {},
    hasSecret: payload.has_secret === true,
    // Absent is false rather than true. A destination this console cannot
    // tell the state of is better drawn as switched off — the reader then
    // switches it on and learns the truth — than drawn as publishing when
    // it is not.
    enabled: payload.enabled === true,
    createdAt: asText(payload.created_at),
    updatedAt: asText(payload.updated_at),
  }
}

/** Every destination in a listing, in the order the API sent them. */
export function parseTargets(payload: unknown): ExportTarget[] {
  if (!isRecord(payload) || !Array.isArray(payload.targets)) return []
  const targets: ExportTarget[] = []
  for (const raw of payload.targets) {
    const target = parseTarget(raw)
    if (target !== null) targets.push(target)
  }
  return targets
}

/* -------------------------------------------------------------------- */
/* The formats, as the deployment reports them                           */
/* -------------------------------------------------------------------- */

export const FORMAT_OUTLINE = 'outline'
export const FORMAT_MARKDOWN = 'markdown'
export const FORMAT_HTML = 'html'

/**
 * What carries a format's bytes away, and therefore what its `target` is.
 *
 * The API's own word (`sturnus.application.export_formats.OUTLINE_SINK`,
 * `OBJECT_STORE_SINK`) rather than a second vocabulary of this console's.
 * There used to be a `TargetKind` of `'collection' | 'prefix'` here that
 * meant exactly these two things under different names, which is one
 * translation table nobody needs: an Outline sink addresses a collection
 * and an object-store sink addresses a key prefix, and saying so once is
 * enough.
 */
export type SinkFamily = 'outline' | 'object_store'

export const SINK_OUTLINE = 'outline'
export const SINK_OBJECT_STORE = 'object_store'

/**
 * One format, exactly as `GET /api/export-formats` reports it.
 *
 * Three fields because the endpoint sends three. `available` is the one
 * that could not be had before: the difference between a format this
 * deployment declines to offer and a format that does not exist.
 *
 * `sink` is `null` for a format this build does not run, and that null is
 * load-bearing rather than a gap — nothing in the API has decided what
 * would carry a PDF, so nothing here may render a field claiming to know.
 */
export interface FormatInfo {
  name: string
  available: boolean
  sink: SinkFamily | null
}

function asSink(value: unknown): SinkFamily | null {
  return value === SINK_OUTLINE || value === SINK_OBJECT_STORE ? value : null
}

/**
 * Every format in a catalogue payload, in the order the API sent them.
 *
 * Order is kept because it is part of the answer: the API lists what a
 * guild has always published to first, and the first row of a picker reads
 * as the ordinary choice.
 *
 * Tolerant in one direction only. A row with no usable `name` is dropped —
 * there is nothing to store or to render for it. A row whose `sink` is a
 * word this console does not know becomes `null`, which is the same state
 * as an unbuilt format's: the address field falls back to a plain box that
 * accepts anything non-empty, and the API decides. An unparseable payload
 * is an empty list, which the page reports as the catalogue being
 * unreadable rather than as a deployment that publishes nothing.
 */
export function parseFormats(payload: unknown): FormatInfo[] {
  if (!isRecord(payload) || !Array.isArray(payload.formats)) return []
  const formats: FormatInfo[] = []
  for (const raw of payload.formats) {
    if (!isRecord(raw)) continue
    const name = (asText(raw.name) ?? '').trim()
    if (name === '') continue
    formats.push({ name, available: raw.available === true, sink: asSink(raw.sink) })
  }
  return formats
}

/**
 * The word this console has for a format, or `null` where it has none.
 *
 * The one thing about a format that stays on this side, and the reason is
 * that the API has no message catalogue: `common.formatOutline` is
 * "Outline document" in English and "Outline-Dokument" in German, and an
 * endpoint that served either would be serving one reader's language to
 * everybody. Under `common.*` rather than this page's namespace because
 * two pages say it — the destinations picker and the recording page's list
 * of published protocols — and two copies of a word are two words that
 * drift.
 *
 * A name with no entry renders as itself. That is not a fallback for a
 * broken state; it is the correct answer for a format this deployment
 * builds and this console has never been taught a word for, and it is what
 * keeps a missing word from being a missing row.
 */
const FORMAT_LABEL_KEYS: Readonly<Record<string, string>> = {
  [FORMAT_OUTLINE]: 'common.formatOutline',
  [FORMAT_MARKDOWN]: 'common.formatMarkdown',
  [FORMAT_HTML]: 'common.formatHtml',
  pdf: 'common.formatPdf',
  confluence: 'common.formatConfluence',
}

export function formatLabelKey(name: string): string | null {
  return FORMAT_LABEL_KEYS[name] ?? null
}

/**
 * What the address field is called, hinted and explained — by sink family
 * and never by format name.
 *
 * Which is the whole point of serving the family. Every format on the
 * object-store family wants a key prefix and calls it the same thing, so
 * `pdf` arriving in the catalogue one day gets the right field, the right
 * label and the right sentence with nothing added here. The previous
 * version of this file had `formatMarkdownNote` and `formatHtmlNote`
 * holding the same sentence twice, which is what a per-name table always
 * decays into.
 */
interface SinkStrings {
  noteKey: string
  targetLabelKey: string
  targetHintKey: string
  /** Whether documents of this family can be read back through the
   *  console. True for the object store, whose bytes this deployment
   *  holds; false for Outline, whose document lives in Outline. Mirrors
   *  `routes_documents._is_readable`. */
  readable: boolean
}

const SINK_STRINGS: Readonly<Record<SinkFamily, SinkStrings>> = {
  [SINK_OUTLINE]: {
    noteKey: 'admin.destinations.sinkOutlineNote',
    targetLabelKey: 'admin.destinations.collectionLabel',
    targetHintKey: 'admin.destinations.collectionHint',
    readable: false,
  },
  [SINK_OBJECT_STORE]: {
    noteKey: 'admin.destinations.sinkObjectStoreNote',
    targetLabelKey: 'admin.destinations.prefixLabel',
    targetHintKey: 'admin.destinations.prefixHint',
    readable: true,
  },
}

/**
 * Everything the form needs about one format, or `null` when the reported
 * catalogue does not contain it.
 *
 * `null` covers three situations that behave identically and deliberately
 * so: a format nobody has heard of, a format stored before the catalogue
 * knew it, and a catalogue that could not be read at all. In each of them
 * the honest position is the same — this console has no opinion, so it
 * asks for a plain address and lets the API decide.
 */
export interface FormatSpec {
  name: string
  available: boolean
  sink: SinkFamily | null
  /** `null` where this console has no word for the format. */
  labelKey: string | null
  /** `null` for an unbuilt format: there is no sink, so there is nothing
   *  truthful to say about what it produces or where it lands. */
  noteKey: string | null
  targetLabelKey: string | null
  targetHintKey: string | null
  readable: boolean
}

export function formatSpec(name: string, formats: readonly FormatInfo[]): FormatSpec | null {
  const info = formats.find((entry) => entry.name === name)
  if (info === undefined) return null
  const strings = info.sink === null ? null : SINK_STRINGS[info.sink]
  return {
    name: info.name,
    available: info.available,
    sink: info.sink,
    labelKey: formatLabelKey(info.name),
    noteKey: strings?.noteKey ?? null,
    targetLabelKey: strings?.targetLabelKey ?? null,
    targetHintKey: strings?.targetHintKey ?? null,
    readable: strings?.readable ?? false,
  }
}

/**
 * An Outline collection id, and an object-store prefix.
 *
 * Both are the `target_pattern` of the matching entry in
 * `sturnus.application.export_formats`, anchored the way Python's
 * `fullmatch` anchors them. **A second copy of a rule the API enforces,
 * and it is a courtesy rather than a control** — exactly what
 * `navigation.ts` says about hiding an admin section. The API refuses a
 * bad target whatever this file thinks; what this buys is that the reason
 * is legible beside the field instead of arriving as a bare 400 that
 * `apiError` has stripped of its explanation.
 *
 * Copied rather than served, and that is on purpose: a `target_pattern` is
 * a compiled Python regular expression, and `/api/export-formats` sends
 * the sink family instead. Handing a caller a pattern to re-compile hands
 * it a dialect — Python and JavaScript disagree about `\d` under Unicode,
 * about what a bare `$` matches, about inline flags — and a pattern that
 * parses differently on the two sides is a courtesy that lies. Two
 * patterns per *family*, which is what the family is for.
 */
const OUTLINE_TARGET = /^[^\s/][^\s]*$/
const OBJECT_PREFIX = /^[A-Za-z0-9_-]+(?:[./][A-Za-z0-9_-]+)*$/

/** Whether a format can address that target at all. A format with no
 *  reported sink — unknown, unbuilt, or from a catalogue that could not be
 *  read — accepts anything non-empty: this console has no pattern for one,
 *  and inventing one would refuse a target the deployment is perfectly
 *  happy with. */
export function acceptsTarget(
  format: string,
  target: string,
  formats: readonly FormatInfo[],
): boolean {
  const sink = formatSpec(format, formats)?.sink ?? null
  if (sink === null) return target.trim() !== ''
  return sink === SINK_OUTLINE ? OUTLINE_TARGET.test(target) : OBJECT_PREFIX.test(target)
}

/**
 * The rows of the format dropdown.
 *
 * Every format the deployment reported, in the order it reported them,
 * plus — when a destination already stores a format that is not among them
 * — that one, labelled with its own raw name. Never dropped: a picker that
 * omitted the stored value would render as though the first row were
 * chosen and rewrite the destination to it on the next save, which is
 * `directory.ts`'s argument for keeping an unresolved id in the list,
 * applied to a word instead of a snowflake.
 *
 * **An unavailable format is a disabled row, not an absent one.** The row
 * says what the format is and, in place of what it would produce, that this
 * deployment does not build it. `UiOption.disabled` is not decoration:
 * `stepEnabled` walks past it, the control renders it unchoosable, and
 * `draftProblems` refuses the draft anyway — so this is a fact stated, not
 * a choice offered and then withdrawn. The argument, and what changed since
 * #150 decided the other way, is at the top of this module.
 *
 * **Except when it is the format this destination already stores.** Then
 * the row is choosable, because it is not an offer — it is where the
 * dropdown's own value has to be able to sit. Disabling it would leave the
 * control pointing at a row it may not select, which is how a picker
 * silently reports the wrong value.
 *
 * Takes a translator rather than returning keys, the way
 * `recordingTabs(label)` does: a `UiOption.label` is text by contract, and
 * the raw name of a format nobody has a word for is text that no key
 * exists for.
 */
export function formatChoices(
  label: (key: string) => string,
  formats: readonly FormatInfo[],
  stored: string | null = null,
): UiOption[] {
  const held = (stored ?? '').trim()
  const options: UiOption[] = formats.map((entry) => {
    const spec = formatSpec(entry.name, formats)
    const labelKey = spec?.labelKey ?? null
    const noteKey = spec?.noteKey ?? null
    return {
      value: entry.name,
      label: labelKey === null ? entry.name : label(labelKey),
      detail: entry.available
        ? (noteKey === null ? undefined : label(noteKey))
        : label('admin.destinations.formatUnavailableDetail'),
      disabled: !entry.available && entry.name !== held,
    }
  })
  if (held !== '' && !formats.some((entry) => entry.name === held)) {
    options.push({
      value: held,
      label: held,
      detail: label('admin.destinations.formatUnknownDetail'),
    })
  }
  return options
}

/* -------------------------------------------------------------------- */
/* Ordering, and which one Discord announces                             */
/* -------------------------------------------------------------------- */

/**
 * The destinations in the order they publish: by id, oldest first.
 *
 * Not by name, and the difference is not cosmetic.
 * `sturnus.application.exporting.destinations_for` sorts by id precisely
 * so that the primary destination does not move when somebody renames one,
 * and `session.document_url` — the link the Discord announcement carries —
 * is stamped from the primary alone. A page that listed these
 * alphabetically would show a reader a first row that is not the first
 * destination, which is the one thing about this list that has a
 * consequence outside the page.
 */
export function orderTargets(targets: readonly ExportTarget[]): ExportTarget[] {
  return [...targets].sort((a, b) => a.id - b.id)
}

/**
 * The destination whose document the Discord announcement links, or `null`
 * when a guild has none.
 *
 * Enabled, and oldest, and nothing else. Deliberately **not** filtered
 * against {@link EXPORT_FORMATS}: whether the deployment can publish a
 * given format is the deployment's answer, and a console that skipped a
 * row because it personally does not know the word `pdf` would name the
 * wrong destination as the announced one on the very deployment where it
 * matters.
 */
export function primaryTarget(targets: readonly ExportTarget[]): ExportTarget | null {
  return orderTargets(targets).find((target) => target.enabled) ?? null
}

/** How many of these are switched on. */
export function enabledTargetCount(targets: readonly ExportTarget[]): number {
  return targets.reduce((total, target) => (target.enabled ? total + 1 : total), 0)
}

/**
 * What the page says about `document_target` on Bot Settings, given what
 * this guild has configured.
 *
 * Said **here**, on the page somebody configures destinations on, rather
 * than only over on Bot Settings — because that is where the two settings
 * look like rivals and where the answer is needed. And it is a function of
 * the guild's state rather than one fixed sentence, because the fallback
 * is genuinely in force for one of these guilds and genuinely dead for the
 * other: `destinations_for` **replaces** the fallback with the configured
 * destinations rather than joining them, so a guild that enables one
 * destination here has silently stopped using `document_target` and should
 * be told so on the screen where it happened.
 */
export function fallbackNote(targets: readonly ExportTarget[]): Message {
  const enabled = enabledTargetCount(targets)
  return enabled === 0
    ? { key: 'admin.destinations.fallbackInUse' }
    : { key: 'admin.destinations.fallbackReplaced', params: { count: enabled } }
}

/* -------------------------------------------------------------------- */
/* One destination, said out loud                                        */
/* -------------------------------------------------------------------- */

/**
 * The subtitle under a destination's name in the list: what it publishes
 * and where.
 *
 * Three sentences, because there are now three things that can be true of
 * a stored format and they are not the same news:
 *
 * - it is reported and this deployment builds it — the ordinary line;
 * - it is reported and **not** built, which means this destination is
 *   configured and nothing will ever be published to it. Said in the list,
 *   beside the destination it is true of, rather than only inside the form
 *   somebody has to open to find out;
 * - it is not reported at all, which is what a stored format from a newer
 *   deployment and an unreadable catalogue both look like. This console
 *   says it does not know and says the deployment's answer is the one that
 *   counts — it does **not** say the destination is broken, because from
 *   here those two look identical.
 *
 * The format is named in words where this console has a word for it and by
 * its raw name where it does not, which is the same rule everywhere else.
 */
export function targetSummary(target: ExportTarget, formats: readonly FormatInfo[]): Message {
  const spec = formatSpec(target.format, formats)
  const labelKey = spec?.labelKey ?? null
  const named = labelKey === null ? target.format : { key: labelKey }
  const key =
    spec === null
      ? 'admin.destinations.rowUnknownFormat'
      : spec.available
        ? 'admin.destinations.rowSummary'
        : 'admin.destinations.rowUnavailableFormat'
  return { key, params: { format: named, target: target.target } }
}

/** Whether this destination is publishing, in words. Switched off is a
 *  configured destination that is not being written to, which is a
 *  different thing from one that does not exist — the API keeps disabled
 *  rows for exactly that reason. */
export function enabledLabelKey(target: ExportTarget): string {
  return target.enabled ? 'admin.destinations.stateEnabled' : 'admin.destinations.stateDisabled'
}

/* -------------------------------------------------------------------- */
/* The credential                                                        */
/* -------------------------------------------------------------------- */

/**
 * What the secret control may do to a destination, and what it may say.
 *
 * The whole of the design is in what is *not* here. There is no `value`,
 * no `masked`, no `reveal`: `PUT .../secret` is the only route that writes
 * a credential and no route anywhere returns one, so a control that
 * offered to show it would be offering something the API cannot serve.
 *
 * `canClear` is separate from `canReplace` because clearing is a separate
 * act with a separate request body (`{"secret": null}`), and because the
 * alternative — a password box rendered empty beside a configured
 * credential, saved along with the rest of the form — silently wipes a
 * token every time somebody corrects a typo in a name. `TargetDraft` has
 * no secret field at all, so that failure is not merely avoided here, it
 * is unrepresentable.
 */
export interface SecretState {
  /** Whether a credential is stored. The only thing known about it. */
  stored: boolean
  statusKey: string
  /** Storing the first one, or replacing the one that is there. */
  actionKey: string
  /** Only where there is something to clear. */
  canClear: boolean
}

export function secretState(target: ExportTarget): SecretState {
  return {
    stored: target.hasSecret,
    statusKey: target.hasSecret
      ? 'admin.destinations.secretStored'
      : 'admin.destinations.secretNone',
    actionKey: target.hasSecret
      ? 'admin.destinations.secretReplace'
      : 'admin.destinations.secretSet',
    canClear: target.hasSecret,
  }
}

/**
 * Whether a typed credential may be submitted.
 *
 * Empty is refused here as well as by the API, which answers 400 to `""` —
 * and refusing it in the control is what stops "save an empty box" from
 * looking like a way to clear one. Clearing has its own button, and it is
 * the only way.
 */
export function canSubmitSecret(typed: string): boolean {
  return typed !== ''
}

/* -------------------------------------------------------------------- */
/* The form                                                              */
/* -------------------------------------------------------------------- */

/**
 * What the destination form edits.
 *
 * Four fields, and the absence of a fifth is the point — see
 * {@link SecretState}. `config` is not here either: none of the three
 * formats needs anything in it, and a JSON box on a page nobody has a
 * reason to type JSON into is a way to break a destination rather than a
 * way to configure one. The page carries the stored `config` through
 * unedited.
 */
export interface TargetDraft {
  format: string
  name: string
  target: string
  enabled: boolean
}

/**
 * A blank destination, ready to be added.
 *
 * The format is **the first one the deployment reports as available**,
 * which for every deployment shipped so far is `outline` — what every
 * guild configured before this page existed — but is that because the API
 * says so rather than because this file used to name it. On a build where
 * `outline` is not compiled in, a form opening on it would be a form that
 * opens invalid.
 *
 * Blank when nothing was reported, which is what an unreadable catalogue
 * looks like. Blank rather than a guess: {@link draftProblems} then says
 * "choose a format" beside an empty picker, and the reader is looking at
 * the truth — this console does not currently know what may be chosen —
 * instead of at a default that a save would refuse.
 *
 * Enabled, because somebody adding a destination is adding one to publish
 * to.
 */
export function emptyDraft(formats: readonly FormatInfo[] = []): TargetDraft {
  const first = formats.find((entry) => entry.available)
  return { format: first?.name ?? '', name: '', target: '', enabled: true }
}

/** An existing destination, ready to be edited. */
export function draftOf(target: ExportTarget): TargetDraft {
  return {
    format: target.format,
    name: target.name,
    target: target.target,
    enabled: target.enabled,
  }
}

/** Which field a complaint is about, so the page can put it beside that
 *  field rather than in a list of grievances at the bottom. */
export type DraftField = 'name' | 'format' | 'target'

export interface DraftProblem {
  field: DraftField
  message: Message
}

/**
 * What is wrong with a draft, in the order the fields are read.
 *
 * Every rule here is one `routes_exports._requested_target` already
 * enforces, plus the name collision `create_target` refuses with a 409.
 * Restating them is deliberate and is the courtesy argued for above
 * {@link acceptsTarget}: `apiError` keeps nothing of a refusal but its
 * status, so a check that only the API makes is a check whose reason the
 * reader never sees.
 *
 * `taken` is the names this guild already uses. On an edit it excludes the
 * destination's own name — the API reads the name from the stored row on a
 * `PUT` and ignores whatever the body says, so a rename is a create plus a
 * delete rather than an edit, and complaining that a destination collides
 * with itself would refuse a save that changes something else.
 *
 * The format complaint is the one rule here that could not be stated
 * before `GET /api/export-formats` existed. **It fires only where the
 * deployment has said the format is unavailable** — never on a format that
 * is merely unreported, because "this console has not been told about it"
 * is not evidence of anything and refusing a save on it would make an
 * unreadable catalogue look like a broken configuration.
 */
export function draftProblems(
  draft: TargetDraft,
  taken: readonly string[] = [],
  formats: readonly FormatInfo[] = [],
): DraftProblem[] {
  const problems: DraftProblem[] = []
  const name = draft.name.trim()
  const spec = formatSpec(draft.format, formats)

  if (name === '') {
    problems.push({ field: 'name', message: { key: 'admin.destinations.nameEmpty' } })
  } else if (taken.some((held) => held.trim() === name)) {
    problems.push({ field: 'name', message: { key: 'admin.destinations.nameTaken' } })
  }

  if (draft.format.trim() === '') {
    problems.push({ field: 'format', message: { key: 'admin.destinations.formatEmpty' } })
  } else if (spec !== null && !spec.available) {
    problems.push({ field: 'format', message: { key: 'admin.destinations.formatUnavailable' } })
  }

  if (draft.target.trim() === '') {
    problems.push({ field: 'target', message: { key: 'admin.destinations.targetEmpty' } })
  } else if (!acceptsTarget(draft.format, draft.target, formats)) {
    problems.push({
      field: 'target',
      message: {
        key: spec?.sink === SINK_OBJECT_STORE
          ? 'admin.destinations.targetNotPrefix'
          : 'admin.destinations.targetNotCollection',
      },
    })
  }

  return problems
}

/** The complaint about one field, or `null`. */
export function problemFor(
  problems: readonly DraftProblem[],
  field: DraftField,
): Message | null {
  return problems.find((problem) => problem.field === field)?.message ?? null
}

/** Whether a draft may be submitted at all. */
export function isDraftReady(
  draft: TargetDraft,
  taken: readonly string[] = [],
  formats: readonly FormatInfo[] = [],
): boolean {
  return draftProblems(draft, taken, formats).length === 0
}

/** The body a create or an update sends. Trimmed the way the API trims,
 *  and carrying the stored `config` through untouched. */
export function draftBody(
  draft: TargetDraft,
  config: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    format: draft.format.trim(),
    name: draft.name.trim(),
    target: draft.target.trim(),
    config,
    enabled: draft.enabled,
  }
}

/** The names a guild already uses, optionally excluding one destination —
 *  the one being edited. */
export function takenNames(
  targets: readonly ExportTarget[],
  except: number | null = null,
): string[] {
  return targets.filter((target) => target.id !== except).map((target) => target.name)
}

/* -------------------------------------------------------------------- */
/* Where the requests go                                                 */
/* -------------------------------------------------------------------- */

/** The format catalogue. No guild in it: every format on it is a fact
 *  about the build, identical for every server this deployment serves. */
export const FORMATS_PATH = '/export-formats'

export function targetsPath(guildId: string): string {
  return `/guilds/${encodeURIComponent(guildId)}/export-targets`
}

export function targetPath(guildId: string, targetId: number): string {
  return `${targetsPath(guildId)}/${targetId}`
}

export function targetSecretPath(guildId: string, targetId: number): string {
  return `${targetPath(guildId, targetId)}/secret`
}

/* -------------------------------------------------------------------- */
/* When a write does not work                                            */
/* -------------------------------------------------------------------- */

/**
 * Why a write failed, from its status and nothing else.
 *
 * The status is all there is: `apiError.sanitiseFetchError` keeps nothing
 * from a failed response but that, on purpose, so that no page can
 * accidentally render an internal hostname out of a `$fetch` error. It is
 * enough for every refusal these routes have, because each status means
 * exactly one thing here — including the 400, which after
 * {@link draftProblems} has passed can only mean that the catalogue this
 * page is holding is older than the deployment answering the write. The
 * remedy is the same one every stale read has, and it is what the sentence
 * says: reload.
 */
export function describeTargetError(error: unknown): Message {
  const held = (error as { status?: unknown } | null)?.status
  const status = typeof held === 'number' ? held : null
  switch (status) {
    case 400:
      return { key: 'admin.destinations.errorRefused' }
    case 401:
      return { key: 'admin.destinations.errorSession' }
    case 404:
      return { key: 'admin.destinations.errorGone' }
    case 409:
      return { key: 'admin.destinations.errorDuplicate' }
    case 0:
    case null:
      return { key: 'admin.destinations.errorUnreachable' }
    default:
      return { key: 'admin.destinations.errorStatus', params: { status: String(status) } }
  }
}
