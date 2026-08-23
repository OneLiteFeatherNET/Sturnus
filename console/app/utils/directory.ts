/**
 * Names for the snowflakes the bot's configuration is made of.
 *
 * Three keys on the settings page hold a Discord id and one holds an
 * Outline collection id. Until this module existed, configuring the bot
 * meant pasting `1289374650912837465` into a text field and reading it
 * back, with nothing anywhere in the console able to say whether that is
 * the meeting room or the archive. The API mirrors the guild's channels,
 * roles and members, and Outline's collections; this module turns that
 * mirror into pickers.
 *
 * **The name is the value; the id is the subtext.** The id remains what is
 * stored and what travels on the wire — a picker spares a human a
 * copy-paste, it does not change what is configured. So every function
 * here takes and returns the stored string.
 *
 * Two decisions carry the weight, and both are about being honest with a
 * guild that is already misconfigured:
 *
 * - **An id with no row in the mirror renders as the bare id and a note
 *   saying it could not be resolved.** Never as a blank, and never as an
 *   option quietly missing from the list. A channel deleted in Discord is
 *   a configuration problem the administrator has to see; an interface
 *   that hides it by omission makes a broken guild look configured.
 * - **A kind this console has never heard of is still rendered.** Discord
 *   keeps adding channel types, and a picker that only knew the ones it
 *   was written against would hide a recordable channel with nothing on
 *   screen to say so.
 *
 * As everywhere in `app/utils`, the sentences are translation keys rather
 * than prose: a pure function returns data, and a key is data. See
 * `i18n/README.md`.
 */
import { formatDuration } from '~/utils/duration'
import { formatMoment } from '~/utils/format'
import type { SettingView } from '~/utils/settings'

/** A row of the mirror: something with an id and something to call it. */
export interface NamedRow {
  id: string
  name: string
}

export interface DirectoryChannel extends NamedRow {
  /** Deliberately a plain string rather than a union. Discord's channel
   *  types are an open set and this console is not the place that decides
   *  which of them exist. */
  kind: string
  position: number
}

export interface DirectoryRole extends NamedRow {
  position: number
}

export interface GuildDirectory {
  /** The guild the rows belong to, so a list loaded for one server can
   *  never be rendered under another one's heading. */
  guildId: string | null
  /** The oldest of the three mirrors, or `null` before the first sweep. */
  syncedAt: string | null
  channels: DirectoryChannel[]
  roles: DirectoryRole[]
  /** Read but not yet rendered: no setting names a member today, and a
   *  parser that dropped half its payload would be the surprise when one
   *  does. */
  members: NamedRow[]
}

export interface CollectionDirectory {
  syncedAt: string | null
  collections: NamedRow[]
}

/* -------------------------------------------------------------------- */
/* Reading what the API sent                                             */
/* -------------------------------------------------------------------- */

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** `null` stays `null`; anything else becomes the string it prints as. Ids
 *  are strings on the wire and have to stay strings: a snowflake exceeds
 *  `Number.MAX_SAFE_INTEGER`, so anything that round-trips through a
 *  JavaScript number hands back an id ending in other digits. */
function asText(value: unknown): string | null {
  if (value === null || value === undefined) return null
  return typeof value === 'string' ? value : String(value)
}

function asNumber(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

function rowsOf(payload: unknown, field: string): Record<string, unknown>[] {
  if (!isRecord(payload)) return []
  const container = payload[field]
  if (!Array.isArray(container)) return []
  return container.filter(isRecord)
}

/**
 * The guild's mirror of Discord.
 *
 * Tolerant of everything except a missing id: a row nothing can be set to
 * is a row with no reason to be in a picker. A row with no name is kept
 * and labelled with its own id, which is exactly what the page would have
 * shown before this module existed.
 */
export function parseDirectory(payload: unknown): GuildDirectory {
  const channels: DirectoryChannel[] = []
  for (const raw of rowsOf(payload, 'channels')) {
    const id = asText(raw.id)
    if (!id) continue
    channels.push({
      id,
      name: asText(raw.name) ?? id,
      kind: asText(raw.kind) ?? '',
      position: asNumber(raw.position),
    })
  }

  const roles: DirectoryRole[] = []
  for (const raw of rowsOf(payload, 'roles')) {
    const id = asText(raw.id)
    if (!id) continue
    roles.push({ id, name: asText(raw.name) ?? id, position: asNumber(raw.position) })
  }

  const members: NamedRow[] = []
  for (const raw of rowsOf(payload, 'members')) {
    const id = asText(raw.discord_user_id) ?? asText(raw.id)
    if (!id) continue
    members.push({ id, name: asText(raw.display_name) ?? asText(raw.name) ?? id })
  }

  return {
    guildId: isRecord(payload) ? asText(payload.guild_id) : null,
    syncedAt: isRecord(payload) ? asText(payload.synced_at) : null,
    channels,
    roles,
    members,
  }
}

/** The Outline collections the signed-in installation can write into. */
export function parseCollections(payload: unknown): CollectionDirectory {
  const collections: NamedRow[] = []
  for (const raw of rowsOf(payload, 'collections')) {
    const id = asText(raw.id)
    if (!id) continue
    collections.push({ id, name: asText(raw.name) ?? id })
  }
  return {
    syncedAt: isRecord(payload) ? asText(payload.synced_at) : null,
    collections,
  }
}

/* -------------------------------------------------------------------- */
/* Which control a key gets                                              */
/* -------------------------------------------------------------------- */

/** `plain` is the text input or textarea the page has always had, chosen
 *  by `inputKind`. It is what every key that is not one of the four keeps
 *  getting. */
export type ControlKind = 'channels' | 'role' | 'collection' | 'plain'

/**
 * The picker a key deserves, by name.
 *
 * A map rather than a list of `if`s so that the fall-through is the
 * default rather than a branch somebody has to remember to write. The
 * settings page renders whatever the registry returns and the registry
 * gains keys without this module hearing about it — a key that fell
 * through to nothing would be a setting that cannot be edited at all,
 * which is far worse than one that asks for an id.
 */
const CONTROL_BY_KEY: Record<string, ControlKind> = {
  voice_channel_ids: 'channels',
  consent_role_id: 'role',
  admin_role_id: 'role',
  document_target: 'collection',
}

export function controlKind(view: SettingView): ControlKind {
  return CONTROL_BY_KEY[view.key] ?? 'plain'
}

/* -------------------------------------------------------------------- */
/* The comma-separated list behind `voice_channel_ids`                   */
/* -------------------------------------------------------------------- */

/**
 * The ids in a stored list, in the order they are stored.
 *
 * Tolerant on the way in — whitespace, a trailing comma, a single id with
 * no comma at all, an empty string — and strict on the way out. The order
 * is preserved on purpose: the settings page decides whether to enable
 * Save by comparing the draft string with the stored one, so a parse that
 * sorted would make every visit to this page look like an unsaved edit.
 *
 * A single id is a list of one. Every guild configured before the bot
 * learned to accept several still stores exactly that.
 */
export function parseIdList(raw: string | null | undefined): string[] {
  if (!raw) return []
  const ids: string[] = []
  for (const part of raw.split(',')) {
    const id = part.trim()
    if (id === '' || ids.includes(id)) continue
    ids.push(id)
  }
  return ids
}

/** Commas, no spaces: what the API stores, and what a value read straight
 *  back out has to equal byte for byte. */
export function serialiseIdList(ids: readonly string[]): string {
  return ids.join(',')
}

export function idListHas(raw: string, id: string): boolean {
  return parseIdList(raw).includes(id)
}

/** Appends, rather than inserting in sorted position: the stored order is
 *  the administrator's and this console has no better one. */
export function addToIdList(raw: string, id: string): string {
  const ids = parseIdList(raw)
  if (ids.includes(id)) return raw
  return serialiseIdList([...ids, id])
}

export function removeFromIdList(raw: string, id: string): string {
  const ids = parseIdList(raw)
  if (!ids.includes(id)) return raw
  return serialiseIdList(ids.filter((candidate) => candidate !== id))
}

/* -------------------------------------------------------------------- */
/* Putting a name to an id                                               */
/* -------------------------------------------------------------------- */

export interface Choice {
  id: string
  /** The name from the mirror, or `null` when there is no row for this
   *  id. */
  name: string | null
  /** What to render: the name where there is one, and the bare id where
   *  there is not. Never empty. */
  label: string
  resolved: boolean
}

/**
 * One id, with a name if the mirror has one.
 *
 * The unresolved case is the one this whole module exists for. It is not
 * an error state and it is not empty: it is a configured id whose row has
 * gone, and the page shows it as the id it is with a sentence saying so.
 */
export function resolveChoice(rows: readonly NamedRow[], id: string): Choice {
  const row = rows.find((candidate) => candidate.id === id)
  return row
    ? { id, name: row.name, label: row.name, resolved: true }
    : { id, name: null, label: id, resolved: false }
}

/** Every id in a stored list, in the stored order, resolved where it can
 *  be. */
export function resolveIdList(rows: readonly NamedRow[], raw: string): Choice[] {
  return parseIdList(raw).map((id) => resolveChoice(rows, id))
}

/* -------------------------------------------------------------------- */
/* Grouping the channels                                                 */
/* -------------------------------------------------------------------- */

export interface KindHeading {
  /** A translation key, or `null` for a kind this console has no word
   *  for — in which case the raw kind is what gets rendered. */
  labelKey: string | null
  raw: string
}

const KIND_LABELS: Record<string, string> = {
  voice: 'admin.settings.kindVoice',
  stage_voice: 'admin.settings.kindStage',
  stage: 'admin.settings.kindStage',
  text: 'admin.settings.kindText',
  '': 'admin.settings.kindUnknown',
}

/**
 * What to call a group of channels.
 *
 * A kind with no entry gets `null` and its own raw string on screen.
 * Inventing a friendly name for `media_stage_beta` would be this console
 * guessing at a Discord release note; showing the raw kind is at least
 * something an administrator can search for.
 */
export function channelKindHeading(kind: string): KindHeading {
  return { labelKey: KIND_LABELS[kind] ?? null, raw: kind }
}

export interface ChoiceGroup extends KindHeading {
  kind: string
  /** True for the one synthetic group holding chosen ids the mirror has no
   *  row for. It is rendered last, and its rows carry the note. */
  unresolved: boolean
  choices: Choice[]
}

/** Voice first, then stage, then text, then anything Discord invented
 *  after this was written — in the order the API sent it. */
const KIND_RANK: Record<string, number> = { voice: 0, stage_voice: 1, stage: 1, text: 2 }

export interface ChannelChoices {
  groups: ChoiceGroup[]
  /** The chosen channels, in the order they are stored, resolved or not. */
  selected: Choice[]
}

/**
 * The channels to offer, and the ones currently chosen.
 *
 * Every channel the mirror holds is offered, whatever its kind: the key is
 * called `voice_channel_ids` and the bot records voice, but a picker that
 * filtered on the kinds this console recognises would drop a stage channel
 * — or whatever Discord ships next — and say nothing about it. Grouping by
 * kind with voice first puts the right ones under the cursor without
 * hiding the rest.
 *
 * A chosen id with no row of its own is appended as its own group rather
 * than dropped, so that unticking it is possible: an option that is not
 * rendered is a configuration nobody can remove from this page.
 */
export function channelChoices(
  channels: readonly DirectoryChannel[],
  raw: string,
): ChannelChoices {
  const byKind = new Map<string, Choice[]>()
  for (const row of channels) {
    const group = byKind.get(row.kind)
    const choice: Choice = { id: row.id, name: row.name, label: row.name, resolved: true }
    if (group) group.push(choice)
    else byKind.set(row.kind, [choice])
  }

  const groups: ChoiceGroup[] = [...byKind.entries()]
    .map(([kind, choices], seen) => ({
      ...channelKindHeading(kind),
      kind,
      unresolved: false,
      choices,
      rank: KIND_RANK[kind] ?? 3,
      seen,
    }))
    .sort((a, b) => a.rank - b.rank || a.seen - b.seen)
    .map(({ rank: _rank, seen: _seen, ...group }) => group)

  const selected = resolveIdList(channels, raw)
  const stray = selected.filter((choice) => !choice.resolved)
  if (stray.length > 0) {
    groups.push({
      kind: '',
      labelKey: 'admin.settings.kindUnresolved',
      raw: '',
      unresolved: true,
      choices: stray,
    })
  }

  return { groups, selected }
}

export interface SingleChoices {
  choices: Choice[]
  /** What is stored right now, or `null` when nothing is. */
  current: Choice | null
}

/**
 * The rows to offer for a key that holds exactly one id.
 *
 * The stored id is added to the list when the mirror has no row for it.
 * Without that the select would render as though nothing were configured,
 * and the next save from this page would overwrite a value the
 * administrator was never shown.
 */
export function singleChoices(rows: readonly NamedRow[], raw: string): SingleChoices {
  const id = (raw ?? '').trim()
  const choices: Choice[] = rows.map((row) => ({
    id: row.id,
    name: row.name,
    label: row.name,
    resolved: true,
  }))
  if (id === '') return { choices, current: null }

  const current = resolveChoice(rows, id)
  if (!current.resolved) choices.push(current)
  return { choices, current }
}

/** Whether a single-choice picker offers "not set" at all. */
export type BlankOption = 'offer' | 'placeholder' | 'none'

/**
 * What the empty row of a select does.
 *
 * This page never offers an action it knows will fail, and an empty value
 * fails for a required key and for an integer one alike — `validateValue`
 * refuses both. So "not set" is a real option only where writing an empty
 * string is a real value; everywhere else the empty row is a disabled
 * placeholder that disappears once something is chosen, and unsetting the
 * key is what the Clear button is for.
 */
export function blankOption(view: SettingView, current: Choice | null): BlankOption {
  if (!view.required && !view.integer) return 'offer'
  return current === null ? 'placeholder' : 'none'
}

/* -------------------------------------------------------------------- */
/* How fresh the mirror is                                               */
/* -------------------------------------------------------------------- */

export interface Freshness {
  /** The sentence to render, as a translation key. */
  key: string
  params: Record<string, string>
  /** Whether the sentence is a warning rather than a note. `true` also for
   *  a mirror that has never been swept and for a timestamp that cannot be
   *  read: in all three cases the list on screen is not known to describe
   *  Discord as it is now. */
  stale: boolean
}

/**
 * An hour. Arbitrary, and the failure it guards against is not: a picker
 * that silently offers a list from last week is how somebody configures a
 * channel that was deleted on Tuesday and finds out at the next meeting.
 */
const STALE_AFTER_SECONDS = 3600

/**
 * How current the names on screen are, in a sentence.
 *
 * `now` is a parameter rather than a `Date.now()` inside this function for
 * the reason `queue.ts` gives: a pure function is testable, and the page
 * only has a clock after it has mounted. A server render that computed an
 * age and a browser render a second later would disagree about the text of
 * the same paragraph, which Vue reports as a hydration mismatch — so
 * before the clock arrives the sentence names the instant and no age, and
 * that is what both renders say.
 */
export function mirrorFreshness(syncedAt: string | null, now: number | null): Freshness {
  if (!syncedAt) {
    return { key: 'admin.settings.mirrorNeverSwept', params: {}, stale: true }
  }

  const at = Date.parse(syncedAt)
  if (Number.isNaN(at)) {
    return { key: 'admin.settings.mirrorSyncUnreadable', params: {}, stale: true }
  }

  const moment = formatMoment(syncedAt)
  const seconds = now === null ? null : Math.floor((now - at) / 1000)
  // A negative age is a clock disagreeing with a clock, not a fresh sweep
  // and not a stale one. Saying only when it happened is the honest half.
  if (seconds === null || seconds < 0) {
    return { key: 'admin.settings.mirrorSynced', params: { moment }, stale: false }
  }

  const age = formatDuration(seconds)
  return seconds >= STALE_AFTER_SECONDS
    ? { key: 'admin.settings.mirrorStale', params: { moment, age }, stale: true }
    : { key: 'admin.settings.mirrorSyncedAgo', params: { moment, age }, stale: false }
}
