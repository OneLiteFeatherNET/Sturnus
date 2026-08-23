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
 * **1. The formats this console offers are the three the API accepts, and
 * `pdf` and `confluence` are absent rather than disabled.** They are
 * specified and deliberately not built (`sturnus.application.
 * export_formats`, spec §3.4), and configuring one answers 400. The
 * console has two precedents for an option it cannot honour, and they
 * point opposite ways. The account menu renders two-factor authentication
 * as an inert "coming soon" row, because that is a *promise to the reader
 * about their own account* sitting in a menu of things to read, and an
 * absent row would read as an oversight. `video_consent_offered` goes the
 * other way: when a guild has no video policy the option is **absent** —
 * not greyed — and one sentence beside the control says the server records
 * audio only.
 *
 * This is the second case, twice over. It is a *form field*, and a
 * dropdown exists to be chosen from and saved; a row inside it that a save
 * would refuse is not a promise, it is a trap laid under the cursor of
 * somebody deciding. And more decisively: **which formats exist is the
 * deployment's answer and this console cannot see it.** `supported_formats`
 * is a registry in the API process, there is no endpoint that reads it
 * out, and `apiError.ts` deliberately keeps nothing from a failed response
 * but its status — so the `{"supported": [...]}` a 400 carries never
 * reaches a page. A "PDF — coming soon" row would therefore be this
 * console asserting a fact about a build it cannot inspect, and on the day
 * `pdf` is added to that registry — which `export_formats` promises needs
 * no change anywhere else — the console would still be calling it
 * unavailable until somebody remembered to edit a second list. So the list
 * below is a belief, it is stated as one, and {@link formatChoices} defers
 * to the stored value whenever the two disagree.
 *
 * **2. A stored format this console has never heard of is still rendered,
 * and still choosable while editing that destination.** Exactly the rule
 * `~/utils/directory` applies to a channel id with no row in the mirror:
 * an option that is not rendered is a configuration nobody can remove from
 * this page, and a picker that silently dropped `pdf` would rewrite it to
 * `outline` the next time somebody renamed the destination.
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
/* The formats this console believes the deployment builds               */
/* -------------------------------------------------------------------- */

export const FORMAT_OUTLINE = 'outline'
export const FORMAT_MARKDOWN = 'markdown'
export const FORMAT_HTML = 'html'

/**
 * What a format's `target` column is.
 *
 * `collection` is an Outline collection id, and the page already has a
 * picker over those names — the same one Bot Settings uses for
 * `document_target`. `prefix` is a key prefix in the object store, which
 * has no directory to browse and is therefore typed.
 */
export type TargetKind = 'collection' | 'prefix'

export interface FormatSpec {
  name: string
  /** What this format is called, under `common.*` rather than under this
   *  page's own namespace. It is the one string two pages have to say —
   *  the destinations page names it in a picker, and the recording page
   *  names it beside a published document — and two copies of a word are
   *  two words that drift. Everything else here is only ever rendered by
   *  the destinations page and is keyed there. */
  labelKey: string
  /** One sentence saying what this format produces and where it lands. */
  noteKey: string
  targetKind: TargetKind
  /** The label over the field that holds `target`. Every format calls it
   *  something different, because it *is* something different: a
   *  collection is a place in Outline, a prefix is part of an object key. */
  targetLabelKey: string
  targetHintKey: string
  /** Whether this format's documents can be read back through the console.
   *  True for the object-store family, whose bytes this deployment holds;
   *  false for Outline, whose document lives in Outline. Mirrors
   *  `routes_documents._is_readable`. */
  readable: boolean
}

/**
 * The three formats, in the registry's own order.
 *
 * A belief about the deployment and not a fact about it — see the note at
 * the top of this module for why the console cannot have the fact, and why
 * `pdf` and `confluence` are absent from here rather than present and
 * inert.
 */
export const EXPORT_FORMATS: readonly FormatSpec[] = [
  {
    name: FORMAT_OUTLINE,
    labelKey: 'common.formatOutline',
    noteKey: 'admin.destinations.formatOutlineNote',
    targetKind: 'collection',
    targetLabelKey: 'admin.destinations.collectionLabel',
    targetHintKey: 'admin.destinations.collectionHint',
    readable: false,
  },
  {
    name: FORMAT_MARKDOWN,
    labelKey: 'common.formatMarkdown',
    noteKey: 'admin.destinations.formatMarkdownNote',
    targetKind: 'prefix',
    targetLabelKey: 'admin.destinations.prefixLabel',
    targetHintKey: 'admin.destinations.prefixHint',
    readable: true,
  },
  {
    name: FORMAT_HTML,
    labelKey: 'common.formatHtml',
    noteKey: 'admin.destinations.formatHtmlNote',
    targetKind: 'prefix',
    targetLabelKey: 'admin.destinations.prefixLabel',
    targetHintKey: 'admin.destinations.prefixHint',
    readable: true,
  },
] as const

/** The spec for a format, or `null` for one this console does not know. */
export function formatSpec(name: string): FormatSpec | null {
  return EXPORT_FORMATS.find((entry) => entry.name === name) ?? null
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
 */
const OUTLINE_TARGET = /^[^\s/][^\s]*$/
const OBJECT_PREFIX = /^[A-Za-z0-9_-]+(?:[./][A-Za-z0-9_-]+)*$/

/** Whether a format can address that target at all. Unknown formats accept
 *  anything non-empty: this console has no pattern for a format it has
 *  never heard of, and inventing one would refuse a target the deployment
 *  is perfectly happy with. */
export function acceptsTarget(format: string, target: string): boolean {
  const spec = formatSpec(format)
  if (spec === null) return target.trim() !== ''
  return spec.targetKind === 'collection'
    ? OUTLINE_TARGET.test(target)
    : OBJECT_PREFIX.test(target)
}

/**
 * The rows of the format dropdown.
 *
 * The three this console knows, plus — when a destination already stores a
 * format that is not among them — that one, labelled with its own raw
 * name. Never dropped: a picker that omitted the stored value would render
 * as though `outline` were chosen and rewrite the destination to `outline`
 * on the next save, which is `directory.ts`'s argument for keeping an
 * unresolved id in the list, applied to a word instead of a snowflake.
 *
 * Takes a translator rather than returning keys, the way
 * `recordingTabs(label)` does: a `UiOption.label` is text by contract, and
 * the raw name of an unknown format is text that no key exists for.
 */
export function formatChoices(
  label: (key: string) => string,
  stored: string | null = null,
): UiOption[] {
  const options: UiOption[] = EXPORT_FORMATS.map((entry) => ({
    value: entry.name,
    label: label(entry.labelKey),
    detail: label(entry.noteKey),
  }))
  const held = (stored ?? '').trim()
  if (held !== '' && formatSpec(held) === null) {
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

/** The subtitle under a destination's name in the list: what it publishes
 *  and where, with the format named in words where this console has a word
 *  for it and by its raw name where it does not. */
export function targetSummary(target: ExportTarget): Message {
  const spec = formatSpec(target.format)
  return {
    key: spec === null
      ? 'admin.destinations.rowUnknownFormat'
      : 'admin.destinations.rowSummary',
    params: { format: spec === null ? target.format : { key: spec.labelKey }, target: target.target },
  }
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

/** A blank destination, ready to be added. Outline first because it is
 *  what every guild configured before this page existed, and enabled
 *  because somebody adding a destination is adding one to publish to. */
export function emptyDraft(): TargetDraft {
  return { format: FORMAT_OUTLINE, name: '', target: '', enabled: true }
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
 */
export function draftProblems(
  draft: TargetDraft,
  taken: readonly string[] = [],
): DraftProblem[] {
  const problems: DraftProblem[] = []
  const name = draft.name.trim()

  if (name === '') {
    problems.push({ field: 'name', message: { key: 'admin.destinations.nameEmpty' } })
  } else if (taken.some((held) => held.trim() === name)) {
    problems.push({ field: 'name', message: { key: 'admin.destinations.nameTaken' } })
  }

  if (draft.format.trim() === '') {
    problems.push({ field: 'format', message: { key: 'admin.destinations.formatEmpty' } })
  }

  if (draft.target.trim() === '') {
    problems.push({ field: 'target', message: { key: 'admin.destinations.targetEmpty' } })
  } else if (!acceptsTarget(draft.format, draft.target)) {
    const spec = formatSpec(draft.format)
    problems.push({
      field: 'target',
      message: {
        key: spec?.targetKind === 'prefix'
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
export function isDraftReady(draft: TargetDraft, taken: readonly string[] = []): boolean {
  return draftProblems(draft, taken).length === 0
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
 * {@link draftProblems} has passed can only mean that this console's list
 * of formats and the deployment's registry disagree.
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
