/**
 * What each configuration key is *for*, and therefore which tab it sits on.
 *
 * `settings.ts` describes a key: whether it is required, when a write to it
 * lands, whether it may be cleared. None of that says what the key is about,
 * and nothing else in the stack does either — the API sends a flat registry,
 * `sturnus.domain.settings` is a flat registry, and `orderSettings` ranks the
 * flat list by urgency. So the notion of a category exists here and nowhere
 * else, which is exactly why it is a table of data with tests over it rather
 * than a `v-if` ladder in a page.
 *
 * Three properties matter more than where any individual key landed:
 *
 * **A key this module has never heard of is still reachable.** New settings
 * arrive in their own pull requests, on their own schedule, and this file is
 * not going to be edited in each of them. An unfiled key goes to a
 * clearly-named catch-all group. The alternative — a `Record` lookup that
 * yields `undefined` and a `filter` that drops it — is a settings page that
 * silently hides a setting, and it looks perfectly finished while doing it.
 *
 * **A group with no keys is not a tab.** A deployment whose API has not
 * shipped `video_consent_offered` yet must not be offered an empty Consent
 * panel: an empty panel reads as a page that failed to load, not as a
 * feature that is not there.
 *
 * **A tab says when something behind it needs attention.** A required key
 * with no value is what keeps the bot from watching a guild at all. In a
 * flat column it was at least on the screen; behind a tab it is behind a
 * tab. So the count travels with the group and the page renders it into the
 * tab's own label — as words, because a coloured dot is not read out to
 * anybody who is not looking at it.
 *
 * The ranking *within* a group is still `orderSettings`': required-and-unset
 * first, then alphabetical. Grouping changes which keys sit together, not
 * which of them somebody needs to see first.
 */
import type { Message } from '~/utils/message'
import { type SettingView, missingRequired, orderSettings } from '~/utils/settings'

/**
 * The tabs, in the order they are shown.
 *
 * `other` is a real group and not an error state, which is why it is in the
 * union rather than modelled as `null`.
 */
export type SettingGroupId =
  | 'recording'
  | 'consent'
  | 'transcription'
  | 'publishing'
  | 'administration'
  | 'other'

/** The id of the group a key with no home goes to. Named once. */
export const UNFILED: SettingGroupId = 'other'

export interface SettingGroupSpec {
  id: SettingGroupId
  /** A key for `$t`, never a sentence — `i18n/README.md`. */
  labelKey: string
  /** The keys this group claims. Empty for the catch-all, which claims
   *  whatever nothing else did. */
  keys: readonly string[]
}

/**
 * Every group, and what each one holds.
 *
 * The shape is argued rather than inherited, because there was nothing to
 * inherit it from. Each group answers one question an administrator arrives
 * with:
 *
 * - **Recording** — *where does Sturnus listen, and when does it stop?* The
 *   channel list and the three timers that end a session. `voice_channel_id`
 *   is here beside the `voice_channel_ids` that replaced it: they are one
 *   setting with two names, and a guild being moved off the deprecated
 *   spelling needs to see both at once.
 * - **Consent and data** — *what has this server promised the people in the
 *   room?* The consent role, the policy the consent names, whether video may
 *   be consented to at all, and how long the audio is kept. Retention is
 *   here and not in a group of its own on purpose: `audio_retention_days` is
 *   not a performance dial, it is the answer to "how long do you hold my
 *   voice", which is the same document `policy_url` points at. A tab holding
 *   one number would also have hidden it behind a word — "Retention" — that
 *   nobody looking for a data-protection promise would think to press.
 * - **Transcription** — *what does the speech-to-text stage do with the
 *   audio?* Language and vocabulary bias.
 * - **Publishing** — *where does the protocol go, and what does it look
 *   like?* Provider and target, the poll interval that moves it, and the two
 *   keys that shape the text: `merge_gap_seconds` decides where one
 *   contribution ends, `timezone` decides what the times in it mean.
 * - **Administration** — *who may change any of this?* One key, and it stays
 *   alone: `admin_role_id` decides who can see this page at all, and filing
 *   it under any of the others would make the way back in depend on
 *   remembering which subject it was filed under.
 * - **Everything else** — the catch-all. See the note at the top of the
 *   file: it exists so that a key nobody has filed is still reachable.
 *
 * The table covers every key the API ships today, so the catch-all is a
 * fall-through and not where half the page lives. The two newest are
 * `admin_audio_download_offered` — may an administrator download the raw
 * audio, which is a promise to the people recorded and therefore sits with
 * the policy that has to say so — and `max_parallel_tracks`, how many
 * tracks the transcription stage runs at once.
 *
 * Nothing is filed from a guess at its name. `spectrograms_by_default` is
 * arriving in a pull request that has not said what it belongs with, so it
 * will land on the catch-all tab, work exactly as every other key does, and
 * stay there until somebody who knows what it is moves it — one line, one
 * test. That is the whole point of the catch-all existing.
 */
export const SETTING_GROUPS: readonly SettingGroupSpec[] = [
  {
    id: 'recording',
    labelKey: 'admin.settings.groupRecording',
    keys: [
      'voice_channel_ids',
      'voice_channel_id',
      'empty_grace_seconds',
      'idle_timeout_minutes',
      'max_session_hours',
    ],
  },
  {
    id: 'consent',
    labelKey: 'admin.settings.groupConsent',
    keys: [
      'consent_role_id',
      'policy_version',
      'policy_url',
      'video_consent_offered',
      'admin_audio_download_offered',
      'audio_retention_days',
    ],
  },
  {
    id: 'transcription',
    labelKey: 'admin.settings.groupTranscription',
    keys: ['transcription_language', 'transcription_prompt', 'max_parallel_tracks'],
  },
  {
    id: 'publishing',
    labelKey: 'admin.settings.groupPublishing',
    keys: [
      'document_provider',
      'document_target',
      'publish_poll_seconds',
      'merge_gap_seconds',
      'timezone',
    ],
  },
  {
    id: 'administration',
    labelKey: 'admin.settings.groupAdministration',
    keys: ['admin_role_id'],
  },
  {
    id: UNFILED,
    labelKey: 'admin.settings.groupOther',
    keys: [],
  },
]

/** The group a key sits in. Total: every string has an answer. */
export function groupOfKey(key: string): SettingGroupId {
  return SETTING_GROUPS.find((group) => group.keys.includes(key))?.id ?? UNFILED
}

export interface SettingGroup {
  id: SettingGroupId
  labelKey: string
  /** The keys on this tab, ranked the way the flat list always ranked them. */
  views: readonly SettingView[]
  /** The required keys here with no value, sorted. Empty is the usual case
   *  and the one that leaves the tab unmarked. */
  needsAttention: readonly string[]
}

/**
 * The tabs a payload produces: the groups that have something in them,
 * in `SETTING_GROUPS` order, each ranked internally by `orderSettings`.
 *
 * The input is not mutated — `orderSettings` already copies before sorting,
 * and a page handing over its reactive list of views would otherwise watch
 * it reorder itself.
 */
export function groupSettings(views: readonly SettingView[]): SettingGroup[] {
  const groups: SettingGroup[] = []
  for (const spec of SETTING_GROUPS) {
    const held = views.filter((view) => groupOfKey(view.key) === spec.id)
    if (held.length === 0) continue
    groups.push({
      id: spec.id,
      labelKey: spec.labelKey,
      views: orderSettings(held),
      // The same function the page's banner counts with, asked of a subset.
      // A second rule for "what counts as needing attention" is a second
      // rule that can disagree with the one above the tabs.
      needsAttention: missingRequired(held),
    })
  }
  return groups
}

/**
 * What one tab reads.
 *
 * A plain group name, or the name with the count of what is unset inside
 * it. The mark is part of the label rather than an ornament beside it for
 * two reasons: `UiTab.label` is the accessible name of the tab, so a mark
 * put there is announced along with the tab instead of only being seen; and
 * a tab strip that wraps to three rows on a phone has nowhere to hang a
 * badge that would not end up on the row below.
 */
export function groupTabLabel(group: SettingGroup): Message {
  const name: Message = { key: group.labelKey }
  if (group.needsAttention.length === 0) return name
  return {
    key: 'admin.settings.groupNeedsAttention',
    params: { group: name, count: group.needsAttention.length },
  }
}
