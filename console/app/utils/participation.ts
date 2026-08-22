/**
 * Who Sturnus has recorded in one server, named, and how many of its
 * meetings each of them was in.
 *
 * This is the only thing in the console that names other people and puts
 * them in an order, and everything in this file is shaped by that. It is
 * not a report about a server the way `~/utils/reporting` is: it is a
 * readout about individuals at work, assembled from recordings that were
 * collected in order to write up meetings. Nothing here pretends otherwise.
 *
 * A module rather than expressions in the page, for the same reason
 * `~/utils/reporting` is one: every function below is a *decision* -- what
 * order the rows are in, what an unmeasured speaking time reads like, what
 * somebody with no name is called, what the reader is told before they ask
 * for any of it -- and a decision embedded in a template can only be tested
 * by rendering one. The wording is the safeguard here, so the wording is
 * what the tests assert on.
 *
 * Six facts govern everything below, and none of them are softened
 * anywhere in this file:
 *
 * - **This ranks people, and it says so.** Not "engagement", not
 *   "activity", not a euphemism that lets somebody quote it without
 *   noticing what they are quoting. `PARTICIPATION_STANDING_NOTE` names it
 *   in the first sentence, and it stands above the list whether or not the
 *   list has been loaded.
 * - **In a German workplace this is subject to co-determination**
 *   (BetrVG §87(1)(6): technical equipment suited to monitoring conduct or
 *   performance), and it serves a further purpose than the one the
 *   recordings were collected for. That is not a reason it cannot exist. It
 *   is the reason `PARTICIPATION_PURPOSE_NOTE` is on the page rather than
 *   in a design document nobody reading the ranking will ever open.
 * - **Reading it is logged.** Which server, who looked, and when. Not who
 *   was in the list -- a log of who was ranked would be a second copy of
 *   the ranking, kept forever, in a place nobody would think to look for
 *   it. The reader is told this *before* the request goes out, because
 *   afterwards is too late to decline.
 * - **Nothing is fetched until somebody asks.** The page loads its
 *   aggregate figures on arrival; this list does not come with them.
 *   Somebody opening Reporting to check whether transcription is keeping up
 *   must not silently generate an audit line saying they looked at a
 *   ranking of their colleagues. The reveal control is what turns looking
 *   into a deliberate act, and `PARTICIPATION_REVEAL_NOTE` says what
 *   pressing it does.
 * - **Attendance is the figure; speaking time is a caveat underneath it.**
 *   Speaking time is a sentence in muted text, never a second column and
 *   never a second order. A "top talker" is a thing this console will not
 *   compute, and a column of durations lined up down a page is one whether
 *   or not anybody sorted it.
 * - **Null is not zero, and an id is not a name.** `speech_seconds` is
 *   null when nobody ever measured, which is a measurement that was not
 *   taken rather than a person who said nothing; `unmeasured_tracks` is the
 *   size of the hole in a figure that is not null. `display_name` is null
 *   when this server has never had a name for them, and the row says
 *   plainly that what it shows instead is an id.
 *
 * The order is the server's and is never recomputed here. See
 * `participationRows`.
 */
import { formatCount, formatDuration, formatMoment } from '~/utils/format'

/* -------------------------------------------------------------------- */
/* What the API describes                                                */
/* -------------------------------------------------------------------- */

/**
 * One person, as the endpoint describes them.
 *
 * Every field is about a named individual, which is why this interface
 * exists in a file of its own rather than as a member of `GuildReport`: a
 * shape that can be passed around is a shape that gets passed around, and
 * this one should never end up on a page that did not decide to carry it.
 */
export interface ParticipationPerson {
  /** A string, always. A Discord snowflake exceeds JavaScript's safe
   *  integer range, where a JSON number silently drops its last digits and
   *  produces an id that looks right and names somebody else. In a list
   *  that ranks people, that is not a rounding error -- it is the wrong
   *  person's name against somebody else's figures. */
  discord_user_id: string
  /** Null when this server has never had a name for them. Rendered as the
   *  id, said to be an id; see `participationIdentityNote`. */
  display_name: string | null
  /** How many of this server's recorded meetings they were in. The figure
   *  the list is ordered by, and the only one it is ordered by. */
  sessions: number
  /** Null means nobody ever measured, which is not zero. Zero means
   *  somebody measured and heard nothing. */
  speech_seconds: number | null
  /** How many of this person's tracks carried no measurement. The size of
   *  the hole in `speech_seconds` when it is not null, and the reason it is
   *  null when it is. */
  unmeasured_tracks: number
  first_seen_at: string | null
  last_seen_at: string | null
}

/**
 * The ranking for one server, and the number of meetings it is out of.
 *
 * `sessions` is not decoration. A rank without it is a claim with no
 * denominator: "in 11 meetings" is most of the year in a server that held
 * twelve and a rounding error in one that held four hundred, and the two
 * read identically on a row that omits the total. Every rendering below
 * carries it.
 */
export interface GuildParticipation {
  /** Null only when the payload named no server, which the page uses to
   *  refuse to show one server's people under another's heading. */
  guild_id: string | null
  /** How many meetings the whole ranking is computed over. */
  sessions: number
  /** In the order the API sent them, which is the order they are shown in.
   *  See `participationRows`. */
  people: ParticipationPerson[]
}

/* -------------------------------------------------------------------- */
/* Reading what the API sent                                             */
/* -------------------------------------------------------------------- */

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** `null` stays `null`; anything else becomes the string it prints as. Ids
 *  are strings on the wire, and a number that arrived instead has already
 *  lost whatever precision it was going to lose. */
function asText(value: unknown): string | null {
  if (value === null || value === undefined) return null
  const text = typeof value === 'string' ? value : String(value)
  return text.trim() === '' ? null : text
}

/** A count that can be printed. Anything absent, negative or not a number
 *  is a defect upstream, and rendering it as "-3 meetings" beside somebody's
 *  name would put that defect in front of the reader as though it were a
 *  fact about that person. Zero is the honest floor. */
function asCount(value: unknown): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return 0
  return Math.round(value)
}

/** A quantity that is allowed to be missing and is not a whole number --
 *  seconds of speech. Nonsense collapses to null rather than to zero,
 *  because "we do not know" is true of a broken figure and "they said
 *  nothing" is not. */
function asOptionalNumber(value: unknown): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return null
  return value
}

/**
 * The ranking in a payload.
 *
 * Always yields a well-formed value, never null, for the same reason
 * `parseGuildReport` does: a page that has to distinguish "the API refused"
 * from "the API answered something odd" already has the thrown `ApiError`
 * for the first, and a parser returning null for the second would turn a
 * strange payload into a blank section with no error anywhere.
 *
 * An entry naming no user is dropped rather than shown as an anonymous row.
 * A row in a ranking of people that identifies nobody cannot be checked,
 * cannot be corrected by the person it is about, and is the one kind of row
 * this list must not contain.
 */
export function parseGuildParticipation(payload: unknown): GuildParticipation {
  const raw = isRecord(payload) ? payload : {}
  const people: ParticipationPerson[] = []

  if (Array.isArray(raw.people)) {
    for (const entry of raw.people) {
      if (!isRecord(entry)) continue
      const id = asText(entry.discord_user_id) ?? asText(entry.user_id)
      if (!id) continue
      people.push({
        discord_user_id: id,
        display_name: asText(entry.display_name),
        sessions: asCount(entry.sessions),
        speech_seconds: asOptionalNumber(entry.speech_seconds),
        unmeasured_tracks: asCount(entry.unmeasured_tracks),
        first_seen_at: asText(entry.first_seen_at),
        last_seen_at: asText(entry.last_seen_at),
      })
    }
  }

  return {
    guild_id: asText(raw.guild_id),
    sessions: asCount(raw.sessions),
    people,
  }
}

/** Where a server's ranking is read from. The id is escaped: it is a string
 *  from an API, and a string allowed to contain a slash is a string allowed
 *  to address a different endpoint. */
export function participationPath(guildId: string): string {
  return `/guilds/${encodeURIComponent(guildId)}/report/participation`
}

/* -------------------------------------------------------------------- */
/* Saying it in words                                                    */
/* -------------------------------------------------------------------- */

/** One or the other, chosen by the count. Written out at each call rather
 *  than derived by adding an `s`, because half the pairs this file needs
 *  are `was`/`were` and `has`/`have`. */
function plural(count: number, one: string, many: string): string {
  return count === 1 ? one : many
}

function meetings(count: number): string {
  return `${formatCount(count)} ${plural(count, 'meeting', 'meetings')}`
}

/**
 * What to call somebody on screen.
 *
 * The whole id when there is no name -- never a shortened one, since
 * snowflakes minted in the same era share their leading digits and a
 * truncated id identifies a group rather than a person. The same rule
 * `personLabel` follows on the consent list, for the same reason: this is a
 * list somebody may act on, and a label that fits several people is worse
 * than a long one.
 */
export function participationPersonLabel(person: ParticipationPerson): string {
  return person.display_name ?? `Discord user ${person.discord_user_id}`
}

/**
 * The line under a nameless row, or `null` when there is a name.
 *
 * A bare snowflake in a list of names reads as a fault in the console, and
 * it is not one: Sturnus learns a display name from Discord and does not
 * always have one for a server it recorded somebody in. Saying outright
 * that the string is an id is also the honest thing to do to the reader who
 * is about to write this row into something -- an id is not a person's
 * name, and a ranking that quietly presents one as the other invites
 * somebody to be identified by guesswork.
 */
export function participationIdentityNote(person: ParticipationPerson): string | null {
  if (person.display_name) return null
  return (
    'This is a Discord user id, not a name. Sturnus has no display name on record for them in '
    + 'this server, so there is nothing else to call them here — and an id is a poor thing to put '
    + 'in a list about people. Work out who it is from Discord rather than from the digits.'
  )
}

/**
 * How much of this server's history one person was present for.
 *
 * Always "n of m", never a bare count and never a percentage. The bare
 * count invites the reader to supply their own denominator, and a
 * percentage is a figure that survives being quoted without one -- "she was
 * in 26 % of meetings" travels into a performance review far more easily
 * than "she was recorded in 11 of the 42 meetings this server held", which
 * is the same fact carrying its own scope.
 */
export function participationAttendanceLine(
  person: ParticipationPerson,
  total: number,
): string {
  if (total <= 0) {
    return (
      `Recorded in ${meetings(person.sessions)}, out of a total this ranking does not know: `
      + 'Sturnus reported no meeting count for this server, so there is nothing to read the figure '
      + 'against.'
    )
  }
  return (
    `Recorded in ${meetings(person.sessions)} of the ${formatCount(total)} this server has `
    + 'recorded.'
  )
}

/**
 * What this person's speaking time is, and what it is not.
 *
 * Written as a sentence rather than offered as a number, and that is the
 * decision, not the phrasing. A duration in a column beside a name is
 * ranked by the eye whether or not anybody sorted it, and speaking time is
 * the figure on this page most likely to be mistaken for a measure of
 * somebody's worth. In prose it has to be read, and reading it means
 * reading the caveat attached to it.
 *
 * Null is never "0 s". The column behind it is nullable: null means nobody
 * ever measured that track -- jobs that predate the measurement columns --
 * and a zero in its place would say that this named person sat through
 * eleven meetings without speaking, which is an accusation rather than a
 * gap in the data.
 */
export function participationSpeechLine(person: ParticipationPerson): string {
  const unmeasured = person.unmeasured_tracks

  if (person.speech_seconds === null) {
    if (unmeasured > 0) {
      return (
        `No speaking time was ever measured for them: all ${formatCount(unmeasured)} of their `
        + `${plural(unmeasured, 'recording', 'recordings')} here ${plural(unmeasured, 'predates', 'predate')} `
        + 'the columns that hold it. That is a measurement nobody took, not a person who said '
        + 'nothing.'
      )
    }
    return (
      'Sturnus holds no measured speaking time for them, and does not say why. Read that as a '
      + 'missing measurement rather than as silence.'
    )
  }

  const spoken = formatDuration(person.speech_seconds)
  if (unmeasured > 0) {
    return (
      `Their microphone carried ${spoken} of speech across the recordings that were measured. `
      + `${formatCount(unmeasured)} of their ${plural(unmeasured, 'recording', 'recordings')} here `
      + `${plural(unmeasured, 'was', 'were')} never measured at all, and a sum skips those in `
      + 'silence — so this figure covers part of what was recorded and falls short by an unknown '
      + 'amount.'
    )
  }
  return (
    `Their microphone carried ${spoken} of speech across those meetings. That is how long a `
    + 'microphone was open on speech, and it is not a measure of anything else.'
  )
}

/**
 * When this person was first and last recorded here.
 *
 * On the row because a rank read without it is read as a fact about the
 * present. Somebody who joined the team in June is behind a colleague who
 * has been in every meeting since November for a reason that has nothing to
 * do with either of them, and this is the only line on the row that can say
 * so.
 */
export function participationSeenLine(person: ParticipationPerson): string {
  const first = person.first_seen_at
  const last = person.last_seen_at

  if (!first && !last) {
    return 'Sturnus did not say when they were first or last recorded here.'
  }
  if (first && last) {
    if (first === last) {
      return `Recorded here once, on ${formatMoment(first)}.`
    }
    return `First recorded ${formatMoment(first)}, most recently ${formatMoment(last)}.`
  }
  const known = first ?? last
  return `Only one end of their span here is known: ${formatMoment(known)}.`
}

/* -------------------------------------------------------------------- */
/* The rows                                                              */
/* -------------------------------------------------------------------- */

export interface ParticipationRow {
  /** The person's id, unique across the rows, so it keys a `v-for`
   *  safely. */
  key: string
  discord_user_id: string
  /** Their display name, or their id said to be one; see
   *  `participationPersonLabel`. */
  name: string
  /** The sentence explaining a row whose name is an id, or null. */
  identity: string | null
  /** Their position in the list as the server ordered it, counting from
   *  one. Equal attendance shares a number; see below. */
  rank: number
  /** True when at least one other row carries the same rank. */
  tied: boolean
  sessions: number
  /** "Recorded in 11 of the 42 this server has recorded." */
  attendance: string
  /** Speaking time, as a sentence and never as a competing figure. */
  speech: string
  /** True when there is no measured speaking time for them at all, so the
   *  page can render the sentence as the absence it is rather than as a
   *  number the reader failed to parse. */
  speechAbsent: boolean
  seen: string
  /** The whole row said in one go, for the reader who is listening to the
   *  page rather than looking at it. */
  detail: string
}

/**
 * The people, in the order the API sent them.
 *
 * **Nothing here sorts.** The server orders the list -- most meetings
 * first, ties broken by name and then by id -- and it is left exactly as it
 * arrived. Two reasons, and the second is the important one. A second
 * ordering in the browser would be a second definition of the order, and
 * the two would drift the first time either changed. And re-sorting is one
 * line away from offering a sort control, which would turn a list somebody
 * has to justify opening into a tool for finding whoever is at the bottom
 * of whichever column you like. This console will not compute a top talker;
 * it should not hand out the means to derive one either.
 *
 * Ranks are shared between equal attendance rather than being the row's
 * index. Two people who were each in eleven meetings are not first and
 * second -- printing them that way would invent a distinction out of the
 * tie-break, which is alphabetical and about their names rather than about
 * them. The rank after a shared one skips, the way places do.
 */
export function participationRows(participation: GuildParticipation): ParticipationRow[] {
  const people = participation.people
  const total = participation.sessions
  const rows: ParticipationRow[] = []

  // The place most recently handed out. A row whose attendance matches the
  // row above it keeps this value instead of taking its own index, which is
  // what makes 1, 1, 3 out of three rows rather than 1, 2, 3.
  let place = 0

  for (let index = 0; index < people.length; index += 1) {
    const person = people[index]!
    const previous = index > 0 ? people[index - 1] : undefined
    const next = index + 1 < people.length ? people[index + 1] : undefined

    if (!previous || previous.sessions !== person.sessions) place = index + 1

    const name = participationPersonLabel(person)
    const attendance = participationAttendanceLine(person, total)
    const speech = participationSpeechLine(person)
    const seen = participationSeenLine(person)

    rows.push({
      key: person.discord_user_id,
      discord_user_id: person.discord_user_id,
      name,
      identity: participationIdentityNote(person),
      rank: place,
      tied:
        (previous !== undefined && previous.sessions === person.sessions)
        || (next !== undefined && next.sessions === person.sessions),
      sessions: person.sessions,
      attendance,
      speech,
      speechAbsent: person.speech_seconds === null,
      seen,
      detail: `${name}. ${attendance} ${speech} ${seen}`,
    })
  }

  return rows
}

/* -------------------------------------------------------------------- */
/* What the reader is told, and when                                     */
/* -------------------------------------------------------------------- */

export const PARTICIPATION_HEADING = 'Attendance ranking'

/**
 * What this is, said before anybody can ask for it.
 *
 * The first sentence names it as a ranking of people, on purpose and
 * without a softer word for it. Somebody who quotes this list elsewhere
 * should have had to read that sentence first; a heading like "engagement"
 * would have let them quote it without ever noticing what they were
 * quoting.
 *
 * The audit line is stated here rather than in a tooltip or a confirmation
 * dialog, and it is stated *before* the request goes out, because
 * afterwards is too late to decline. What the log holds is spelled out too:
 * the reader is entitled to know that their own colleagues' names are not
 * being copied into it every time somebody looks.
 */
export const PARTICIPATION_STANDING_NOTE =
  'This is a ranking of named people by how many of this server’s meetings each of them was '
  + 'recorded in, and by how long their microphone carried speech. It is a different kind of '
  + 'report from the figures above it: those describe a server, this describes the individuals in '
  + 'it. Every time somebody opens it, that is written to the audit log — which server, who '
  + 'looked, and when. The list itself is not: the log records that a ranking was read, never who '
  + 'was in it.'

/**
 * What the numbers do not mean.
 *
 * Second, and never folded into the note above. The first note tells the
 * reader what they are looking at; this one tells them what they must not
 * conclude from it, which is the sentence that has to survive being read by
 * somebody in a hurry who has already decided what they think.
 *
 * The list of what is invisible to Sturnus is specific for the same reason.
 * "Attendance" sounds like a complete record and is not one: the only
 * meetings in here are the ones held in a voice channel Sturnus watches,
 * with people who consented to being recorded in it. A person can do a
 * year's work and appear near the bottom.
 */
export const PARTICIPATION_CONTRIBUTION_NOTE =
  'Being present in more meetings is not a measure of contribution, and speaking time even less '
  + 'so. These numbers describe attendance in the voice channels Sturnus records, and nothing '
  + 'else. A meeting held in a room, in a call elsewhere, in a channel Sturnus does not watch, or '
  + 'with somebody who has not consented to being recorded, is invisible here — so a low place on '
  + 'this list is not evidence of anything, and a high one is not either.'

/**
 * Why this is fenced off the way it is.
 *
 * The recordings behind these figures were collected in order to write
 * meetings up. Counting how often each named person turned up, and how long
 * each of them talked, is a further purpose, and in a German workplace a
 * facility of this kind is subject to co-determination -- BetrVG §87(1)(6)
 * covers technical equipment suited to monitoring the conduct or
 * performance of employees, and this is squarely that.
 *
 * Said on the page rather than kept in a design document, because the
 * person who most needs to know it is the administrator about to paste this
 * list into a message, and they are not going to read the design document.
 */
export const PARTICIPATION_PURPOSE_NOTE =
  'These recordings were made in order to write meetings up. Counting how often each named person '
  + 'attended, and how long each of them spoke, is a further purpose than that one. Where Sturnus '
  + 'runs in a workplace, a facility that can be used to observe how individual employees behave '
  + 'or perform is subject to co-determination — in Germany, BetrVG §87(1)(6) — so this list is '
  + 'something to agree on with a works council before it is used, not something to quietly start '
  + 'quoting.'

export interface ParticipationNote {
  key: string
  label: string
  text: string
}

/**
 * The three things that stand above this list at all times.
 *
 * Above the reveal control as well as above the loaded rows, deliberately.
 * A note that appears only once the ranking is on screen is a note that
 * arrives after the decision it exists to inform; the reader is supposed to
 * be able to decide *not* to press the button, and they cannot do that on
 * information they have not been given yet.
 */
export function participationNotes(): ParticipationNote[] {
  return [
    { key: 'what', label: 'What this is', text: PARTICIPATION_STANDING_NOTE },
    { key: 'meaning', label: 'What it does not measure', text: PARTICIPATION_CONTRIBUTION_NOTE },
    { key: 'purpose', label: 'What it is for, and what it is not', text: PARTICIPATION_PURPOSE_NOTE },
  ]
}

/* -------------------------------------------------------------------- */
/* Asking for it                                                         */
/* -------------------------------------------------------------------- */

/**
 * The control that loads the list, and it says what it will do.
 *
 * Not "Show more", not "Details", not an arrow on a collapsible section.
 * The label names the thing on the other side of the click, because the
 * click is the moment somebody becomes a person who looked at a ranking of
 * their colleagues, and a control that hides that behind a generic word has
 * arranged for them to do it by accident.
 */
export const PARTICIPATION_REVEAL_LABEL = 'Show the attendance ranking'

/** While the request is out. Present tense and no cancel: the audit line is
 *  written by the API when it answers, and a button that looked like it
 *  could take that back would be lying. */
export const PARTICIPATION_REVEAL_BUSY_LABEL = 'Reading the ranking…'

/** Putting it away again. It does not unsay anything -- see
 *  `PARTICIPATION_HIDE_NOTE` -- but a list of named colleagues left on
 *  screen behind somebody who has finished with it is worth one click to
 *  clear. */
export const PARTICIPATION_HIDE_LABEL = 'Hide the ranking'

export const PARTICIPATION_HIDE_NOTE =
  'Hidden again, and the audit line stays: it records that the ranking was read, and hiding it '
  + 'afterwards does not change that it was.'

/**
 * What pressing the button does, said before it is pressed.
 *
 * The last sentence is the reason this section is loaded on demand at all,
 * and it is on the page rather than only in the code comment that
 * implements it. Somebody who opened Reporting to see whether transcription
 * is keeping up has not asked to see a ranking of the people they work
 * with, and their name should not appear in an audit log saying they did.
 * Fetching this alongside the aggregate figures would have put it there for
 * everybody who ever loaded the page.
 */
export const PARTICIPATION_REVEAL_NOTE =
  'Nothing has been loaded. Pressing this asks Sturnus for the list — the people it has recorded '
  + 'in this server, by name, ordered by how many meetings each of them was in — and writes a line '
  + 'in the audit log saying that you asked. The figures above were loaded without any of that, so '
  + 'opening this page to see whether transcription is keeping up does not record you as having '
  + 'looked at a ranking of your colleagues.'

/** While the request is in flight, in the page's own voice. Named as a
 *  ranking here too: the word does not get to disappear once somebody has
 *  agreed to load it. */
export const PARTICIPATION_LOADING_NOTE = 'Reading this server’s attendance ranking…'

/* -------------------------------------------------------------------- */
/* A server with nobody in it                                            */
/* -------------------------------------------------------------------- */

/** Whether there is anybody to rank at all. A list of nobody rendered as an
 *  empty table reads as a page that failed to load, which is the failure
 *  mode hardest to report -- and the one most likely to be retried, which
 *  in this section costs another audit line. */
export function isParticipationEmpty(participation: GuildParticipation): boolean {
  return participation.people.length === 0
}

export const PARTICIPATION_EMPTY_HEADING = 'Sturnus has recorded nobody in this server'

/**
 * The empty state, as a sentence and a second saying what would fill it.
 *
 * It also says the thing the reader is most likely to get wrong about an
 * empty list here: nobody is missing from it. A person Sturnus has never
 * recorded has no row rather than a row of zeros, because a zero would be a
 * measurement -- it would say this named person attended nothing -- and
 * nothing has been measured about them at all.
 */
export const PARTICIPATION_EMPTY_NOTE =
  'There is nobody to list: no meeting in a channel Sturnus watches has recorded anybody here who '
  + 'consented to it. Nobody is missing from this list — somebody Sturnus has never recorded has '
  + 'no row rather than a row of zeros, because a zero would say they attended nothing, and '
  + 'nothing about them has been measured at all.'

/**
 * What the list is ordered by, and out of how much.
 *
 * The total belongs above the rows as well as inside each of them. A reader
 * scanning names down a column has stopped reading the sentences by the
 * third row, and the denominator is the one thing that must not be lost on
 * the way down -- eleven meetings is most of a year here or a fortnight of
 * it, and only this line says which.
 */
export function participationScopeLine(participation: GuildParticipation): string {
  const people = participation.people.length
  const total = participation.sessions

  if (total <= 0) {
    return (
      `${formatCount(people)} ${plural(people, 'person', 'people')}, ordered by how many meetings `
      + 'each was recorded in, most first. Sturnus reported no meeting count for this server, so '
      + 'there is no total to read these figures against — a place in this order means very little '
      + 'without one.'
    )
  }
  return (
    `${formatCount(people)} ${plural(people, 'person', 'people')}, ordered by how many of this `
    + `server’s ${meetings(total)} each was recorded in, most first. Equal attendance shares a `
    + 'place; the order within a tie is alphabetical and means nothing. Every figure below is out '
    + `of those ${formatCount(total)}.`
  )
}

/* -------------------------------------------------------------------- */
/* When the API says no                                                  */
/* -------------------------------------------------------------------- */

/** `ApiError` names it `status`; a raw `$fetch` failure may name it
 *  `statusCode`; a request that never got a response has neither, and null
 *  says so rather than standing in a number that would read as an
 *  answer. */
function statusOf(error: unknown): number | null {
  if (!isRecord(error)) return null
  for (const candidate of [error.status, error.statusCode]) {
    if (typeof candidate === 'number' && Number.isFinite(candidate)) {
      // `ApiError` uses 0 for "never reached the API", which is
      // deliberately distinguishable from every real status.
      return candidate === 0 ? null : candidate
    }
  }
  return null
}

/**
 * A failed request, in a sentence somebody can act on.
 *
 * Built from the status alone. `useApi` throws `ApiError`, which carries no
 * body by design -- the API's own `{"error": "no such guild"}` never
 * reaches this console -- so every sentence below has to stand on its own
 * without it.
 *
 * Named `describeParticipationError` rather than `describeError` for the
 * same reason `describeReportError` is: everything under `app/utils` is
 * auto-imported into every component, and two exports sharing a name is a
 * build warning and a coin toss over which one a page actually gets.
 *
 * Every message here says the ranking was not shown, rather than that
 * "nothing could be loaded". A reader who is unsure whether they saw a
 * partial list is a reader who will press the button again, and pressing it
 * again is another audit line.
 */
export function describeParticipationError(error: unknown): string {
  const status = statusOf(error)
  switch (status) {
    case 401:
      return 'Your session has ended, so the ranking was not loaded. Sign in again to ask for it.'
    case 403:
      return (
        'You do not administer this server, so its ranking was not loaded. Administrators are the '
        + 'members holding the role named by that guild’s `admin_role_id`.'
      )
    case 404:
      // The API answers 404 both for a server that does not exist and for
      // one the caller does not administer, on purpose: it will not
      // confirm the existence of a server to somebody with no business
      // there. So this sentence has to cover both without guessing which.
      return (
        'Sturnus does not know this server, or you no longer administer it — it answers the same '
        + 'way to both, and no ranking was loaded. Reload the page; the list of servers is rebuilt '
        + 'from Discord.'
      )
    case null:
      return (
        'Could not reach the API, so no ranking was loaded. Check the connection and ask again if '
        + 'you still want it.'
      )
    default:
      return (
        `Sturnus answered ${status} and produced no ranking. Nothing is known about why, and `
        + 'nothing of the list was shown.'
      )
  }
}
