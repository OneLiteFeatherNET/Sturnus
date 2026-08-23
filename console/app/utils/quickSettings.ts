/**
 * What belongs on the dashboard, decided by what the reader actually is.
 *
 * The dashboard is the page somebody lands on, and §7.3 of the
 * personalisation spec names the mistake this module exists to avoid:
 * consent is the thing a participant is most likely to have come to do, and
 * making them find it two clicks deep is the current design's error repeated
 * in a new place.
 *
 * So the dashboard grows a band. Not a settings page in miniature -- the
 * smallest set that answers "what would this person most likely open the
 * console for" -- and which parts of it exist at all is a decision rather
 * than a template condition. Three properties are worth having tested
 * without rendering anything:
 *
 * - **Silence is a legitimate answer.** Somebody with no consent record
 *   anywhere gets nothing: not an empty box, not "you have consented
 *   nowhere". A failure is different and says so in one sentence, because a
 *   404 drawn as an empty list would be a false statement about somebody's
 *   own data.
 * - **The registry is the source of truth for which keys exist.** This
 *   module names the keys it would like; the API says which of them are
 *   real. A name nothing serves is simply absent, never a crash, so adding
 *   or renaming a setting upstream cannot break the dashboard. That is not
 *   hypothetical: the recording-channel key is `voice_channel_id` on the
 *   deployed API and `voice_channel_ids` on the branch that lets a guild
 *   name more than one, and this band has to render correctly against
 *   whichever of the two answers it.
 * - **A key whose write needs a confirmation is not offered here.** The
 *   band has no room for the confirmation the full page insists on, and an
 *   interface that quietly skips a warning the other one gives is worse
 *   than one that says where the control lives. The rule is expressed
 *   against `confirmation()` rather than against a list of key names, so a
 *   key that starts invalidating consent tomorrow drops out of the band on
 *   its own.
 *
 * Everything a key *says* -- its label, its hints, when a change lands,
 * whether it may be cleared, what a bad value reads like -- stays in
 * `~/utils/settings`, which the full Bot Settings page uses. This module
 * chooses; it does not describe. Two descriptions of one key is how the two
 * pages start telling an administrator different things about it.
 */
import { type SettingView, clearability, confirmation } from '~/utils/settings'

/** Where the rest of a guild's configuration lives, for the sentence that
 *  sends somebody there. */
export const BOT_SETTINGS_PATH = '/admin/bot-settings'

/** Where a person's own consent lives in full. */
export const OWN_SETTINGS_PATH = '/settings'

/**
 * The settings a guild changes often rather than once, in the order they
 * are shown.
 *
 * Which channels are recorded, which language the transcription is in, and
 * how long the audio is kept. Everything else -- the policy document, the
 * role ids, the timeouts, the Whisper prompt -- is set once when a server is
 * onboarded and then left alone, and belongs a click away rather than on the
 * page everybody lands on.
 *
 * Both spellings of the channel key are listed on purpose. The plural
 * arrives with the change that lets a guild name more than one recording
 * channel; the singular is what the API answers with until then. Listing
 * both means this band works before and after that deploy, and the one the
 * registry does not serve is simply not there.
 */
export const QUICK_SETTING_KEYS: readonly string[] = [
  'voice_channel_ids',
  'voice_channel_id',
  'transcription_language',
  'audio_retention_days',
]

/**
 * Whether this band may write this key at all.
 *
 * `invalidates_consent` is the one flag that turns a write into something
 * that needs asking first: bumping `policy_version` stops every consent
 * naming the old value from counting, in the middle of a meeting that is
 * already running. The full page hosts that confirmation. This band does
 * not, so it does not offer the key -- rather than offering a control that
 * skips a warning the other page insists on.
 */
export function mayWriteHere(view: SettingView): boolean {
  return confirmation(view) === null
}

export interface QuickSettingsSelection {
  /** The keys this band renders a control for, in `QUICK_SETTING_KEYS`
   *  order. */
  shown: SettingView[]
  /** Keys the registry does serve and this band still will not write,
   *  because they need a confirmation it has no room for. Named rather
   *  than dropped silently, so their absence reads as a decision and the
   *  band can say where they live. */
  withheld: string[]
}

/**
 * Which of a guild's settings the dashboard shows.
 *
 * Driven by `QUICK_SETTING_KEYS` and filtered by what the payload actually
 * contained, so the order is this module's and the existence is the
 * registry's. A name the API never mentioned contributes nothing at all --
 * no row, no placeholder, no error.
 */
export function selectQuickSettings(views: readonly SettingView[]): QuickSettingsSelection {
  const byKey = new Map(views.map((view) => [view.key, view]))
  const shown: SettingView[] = []
  const withheld: string[] = []
  for (const key of QUICK_SETTING_KEYS) {
    const view = byKey.get(key)
    if (!view) continue
    if (mayWriteHere(view)) shown.push(view)
    else withheld.push(view.key)
  }
  return { shown, withheld }
}

/**
 * Whether a clear is offered beside a quick control.
 *
 * Delegated rather than decided again: `clearability` is the console's
 * mirror of the API's `may_clear`, it is what the full page asks, and a
 * second copy of that rule here is how the two pages end up disagreeing
 * about whether a required key can be emptied.
 */
export function mayClearHere(view: SettingView): boolean {
  return clearability(view).clearable
}

/* -------------------------------------------------------------------- */
/* What the consent half of the band does                                */
/* -------------------------------------------------------------------- */

/**
 * The three shapes the consent half can take.
 *
 * - `records` -- the guilds this person participates in, and the control
 *   that changes each.
 * - `unavailable` -- one sentence saying the consent service could not be
 *   read. The endpoint answers 404 until it is deployed, and the console
 *   ships as a separate image from the API.
 * - `silent` -- nothing whatsoever. Somebody with no consent record
 *   anywhere is not owed a box explaining that; a heading over an empty
 *   space on the page they land on is a worse answer than no heading.
 */
export type ConsentBand = 'records' | 'unavailable' | 'silent'

/**
 * Which of the three, from the only two facts that decide it.
 *
 * A failure outranks emptiness deliberately. A failed read and an empty
 * list are indistinguishable in the rendered output of a naive template,
 * and only one of them means "nothing of you is being recorded anywhere".
 */
export function consentBand(input: { failed: boolean, records: number }): ConsentBand {
  if (input.failed) return 'unavailable'
  return input.records > 0 ? 'records' : 'silent'
}

export interface DashboardBand {
  consent: ConsentBand
  /** Whether the administrator half exists. `GET /api/guilds` answering
   *  `[]` is a real answer and not a failure: somebody who administers no
   *  server simply has no such settings, and the half is absent rather
   *  than empty. */
  guildSettings: boolean
}

/**
 * The whole band, composed from what this reader is.
 *
 * Deliberately one function rather than two independent conditions in a
 * template: the interesting case is the person who is neither -- no consent
 * anywhere and no guild to administer -- and the property worth a test is
 * that they see nothing at all rather than two headings over empty space.
 */
export function dashboardBand(input: {
  consentFailed: boolean
  consentRecords: number
  administeredGuilds: number
}): DashboardBand {
  return {
    consent: consentBand({ failed: input.consentFailed, records: input.consentRecords }),
    guildSettings: input.administeredGuilds > 0,
  }
}

/** Whether the band has anything to say. When it does not, the dashboard
 *  renders no separator, no heading and no space where it would have been. */
export function bandIsEmpty(band: DashboardBand): boolean {
  return band.consent === 'silent' && !band.guildSettings
}
