/**
 * How one recording is divided, and which part of it a bare link opens.
 *
 * The page was one long scroll: a metadata card, a tag editor, a transport
 * for the whole meeting, an administrator's panel and then one `<audio>`
 * and one spectrogram per speaker. Everything on it was worth having and
 * no two of them were the same question, which is exactly the shape a tab
 * bar is for.
 *
 * **Four tabs, and each is a question somebody actually arrives with.**
 *
 * - `meeting` — *what was this, and let me hear it.* The transport, the
 *   protocol link, what was written about the meeting, and who was in the
 *   room including the people with no audio.
 * - `tracks` — *what did one person say, and what is in this file.* One
 *   player and one spectrogram per speaker, with the file's own
 *   measurements beside the audio they describe.
 * - `transcript` — *what was said*, in words.
 * - `details` — *the things somebody writes down here.* The title and
 *   description, the reader's own tags, and — for an administrator of the
 *   guild — the re-queue panel.
 *
 * **There is no `metadata` tab, and that is a decision rather than an
 * omission.** The session's own facts — when it started, when it ended,
 * how long it ran, how many speakers were recorded — are in the page's
 * header, above the bar, because every tab is about that meeting and
 * somebody reading the transcript should not have to leave it to find out
 * when the meeting was. The audio measurements went the other way, down
 * onto `tracks` and into the row of the track they describe: a sample rate
 * is only ever the answer to "why does this one sound wrong", and that
 * question is asked while listening to that track, not in a table of
 * numbers three tabs away. What is left for a metadata tab to hold is
 * nothing.
 *
 * **The re-queue panel is on `details` rather than in a tab of its own.**
 * It renders nothing at all for anybody who does not administer the guild
 * — which is almost everybody — so a tab for it would be an empty tab for
 * almost every reader. Worse, it discovers its own audience from an HTTP
 * status after a round trip, so that tab would have to appear late; and a
 * tab bar that grows a member after it has rendered moves every tab beside
 * it and, because `uiTabs` reads the default from the *first* tab, could
 * change which panel a bare address opens. `details` already means "the
 * things you change about this recording", and re-running a transcription
 * is the largest of them.
 *
 * **`meeting` is first, and first is what a bare address opens.**
 * `~/utils/uiTabs` drops the query parameter for the first tab, so
 * `/recordings/4711` and `/recordings/4711?tab=meeting` are the same place
 * and the plain one stays plain. Three arrivals were weighed:
 *
 * - *From the recordings list.* #141 took playback off that page on
 *   purpose; the row already showed the date, the channel, who was in it
 *   and whether a protocol exists. The one thing it withheld is the audio,
 *   and the click is somebody coming for it.
 * - *From a protocol link.* They have read the words. What a document
 *   cannot give them is the sixty seconds it was written from.
 * - *From an announcement in Discord.* They know nothing yet, and
 *   `meeting` is the tab that says what this was: the description, the
 *   protocol link, the roster.
 *
 * `transcript` is the real rival and loses twice over. The words are
 * already in the protocol document, which is where most people read them —
 * and it is the one panel whose cost is paid on arrival rather than on a
 * click. Making it the default would charge every listener for a document
 * they came here *not* to read. It is one query parameter away, which is
 * the entire point of a tab having an address.
 *
 * **No tab is ever disabled.** A disabled tab cannot explain itself, and
 * two of these have a state — the audio erased by retention — that needs
 * explaining rather than hiding. Greying out `tracks` would also move the
 * default and quietly redirect `?tab=tracks` links that were shared before
 * the retention window closed.
 */
import { queryForTab, type TabQuery, type UiTab } from './uiTabs'

/** One tab, before anybody has translated it. `labelKey` rather than a
 *  label, for the reason `i18n/README.md` gives: a module under
 *  `app/utils` returns keys, and the template calls `$t`. */
export interface RecordingTab {
  id: string
  labelKey: string
}

/**
 * The tabs, in the order they are read and in the order they are shown.
 *
 * The order is the argument. First is the default, and the four run from
 * "the meeting as a whole" to "the parts of it" to "the words" to "what
 * you write down", which is also the order somebody works through a
 * recording they have just opened.
 */
export const RECORDING_TABS: readonly RecordingTab[] = [
  { id: 'meeting', labelKey: 'recordings.tabMeeting' },
  { id: 'tracks', labelKey: 'recordings.tabTracks' },
  { id: 'transcript', labelKey: 'recordings.tabTranscript' },
  { id: 'details', labelKey: 'recordings.tabDetails' },
] as const

/** What a bare `/recordings/{id}` opens. Derived from the list rather than
 *  written beside it, because two statements of one fact are two things to
 *  keep in step and `uiTabs` already decided that the first tab wins. */
export const RECORDING_DEFAULT_TAB: string = RECORDING_TABS[0]!.id

/** The tabs as `UiTabs` wants them, once the page has words for them. */
export function recordingTabs(label: (key: string) => string): UiTab[] {
  return RECORDING_TABS.map((tab) => ({ id: tab.id, label: label(tab.labelKey) }))
}

/**
 * The query a link to one of these tabs carries.
 *
 * The rest of the address survives, and the default tab drops the
 * parameter — both of which `queryForTab` already decides. This wrapper
 * exists so that a link from one panel to another (the audio tabs point at
 * `transcript` once retention has swept the recordings) does not have to
 * restate the tab list, and so that a page never has to invent a label to
 * ask an arithmetic question.
 */
export function recordingTabQuery(query: Readonly<TabQuery>, id: string): TabQuery {
  return queryForTab(query, recordingTabs((key) => key), id)
}
