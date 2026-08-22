/**
 * What the attendance ranking says about the people in it, and what it
 * refuses to say.
 *
 * All of it lives in `~/utils/participation` rather than in the page,
 * because every one of these is a decision -- what order the rows are in,
 * what an unmeasured speaking time reads like, what somebody with no name
 * is called, what the reader is told before the request goes out -- and a
 * decision embedded in a template can only be tested by rendering one.
 *
 * The wording is asserted here far more heavily than in the other specs,
 * and deliberately. This is the only thing in the console that names other
 * people and puts them in an order; the sentences around it are the whole
 * of what stops it being read as a scoreboard, and a test that checked only
 * the numbers would let every one of them be deleted without a failure.
 *
 * Five things are asserted that are not facts about the data at all:
 *
 * - the standing note calls it a ranking of people, in those words;
 * - it says that opening it is written to the audit log, and that the log
 *   does not hold who was in the list;
 * - it says outright that attendance is not a measure of contribution;
 * - the reveal control names what it will load, rather than saying "show
 *   more";
 * - and no sentence this module can produce anywhere contains the
 *   vocabulary of a leaderboard.
 */
import { describe, expect, it } from 'vitest'

import {
  PARTICIPATION_CONTRIBUTION_NOTE,
  PARTICIPATION_EMPTY_HEADING,
  PARTICIPATION_EMPTY_NOTE,
  PARTICIPATION_HIDE_LABEL,
  PARTICIPATION_HIDE_NOTE,
  PARTICIPATION_LOADING_NOTE,
  PARTICIPATION_PURPOSE_NOTE,
  PARTICIPATION_REVEAL_BUSY_LABEL,
  PARTICIPATION_REVEAL_LABEL,
  PARTICIPATION_REVEAL_NOTE,
  PARTICIPATION_STANDING_NOTE,
  describeParticipationError,
  isParticipationEmpty,
  parseGuildParticipation,
  participationAttendanceLine,
  participationIdentityNote,
  participationNotes,
  participationPath,
  participationPersonLabel,
  participationRows,
  participationScopeLine,
  participationSeenLine,
  participationSpeechLine,
  type GuildParticipation,
  type ParticipationPerson,
} from '../app/utils/participation'

/** Somebody the system knows a name for, who was in a good share of the
 *  meetings and whose recordings were all measured -- so each test states
 *  only the one property it is actually about. */
function person(overrides: Partial<ParticipationPerson> = {}): ParticipationPerson {
  return {
    discord_user_id: '200',
    display_name: 'ben',
    sessions: 11,
    speech_seconds: 4200,
    unmeasured_tracks: 0,
    first_seen_at: '2025-11-04T09:00:00+00:00',
    last_seen_at: '2026-08-21T12:00:00+00:00',
    ...overrides,
  }
}

function ranking(overrides: Partial<GuildParticipation> = {}): GuildParticipation {
  return {
    guild_id: '4711',
    sessions: 42,
    people: [person()],
    ...overrides,
  }
}

/** What `ApiError` looks like to the function that reads a failure. */
function failure(status: number) {
  return { status, path: '/guilds/4711/report/participation' }
}

/** Every sentence this module can produce for one ranking, so a test can
 *  assert on what none of them says. The standing notes are in here too:
 *  they are as much a part of what the page claims as any row is. */
function everySentence(value: GuildParticipation): string {
  return [
    ...participationRows(value).map(
      (row) => `${row.name} ${row.attendance} ${row.speech} ${row.seen} ${row.identity ?? ''}`,
    ),
    ...participationNotes().map((note) => `${note.label} ${note.text}`),
    participationScopeLine(value),
    PARTICIPATION_REVEAL_LABEL,
    PARTICIPATION_REVEAL_BUSY_LABEL,
    PARTICIPATION_REVEAL_NOTE,
    PARTICIPATION_HIDE_LABEL,
    PARTICIPATION_HIDE_NOTE,
    PARTICIPATION_LOADING_NOTE,
    PARTICIPATION_EMPTY_HEADING,
    PARTICIPATION_EMPTY_NOTE,
  ].join(' ')
}

describe('reading the ranking payload', () => {
  it('reads the whole envelope the endpoint sends', () => {
    const parsed = parseGuildParticipation({
      guild_id: '4711',
      sessions: 42,
      people: [
        {
          discord_user_id: '200',
          display_name: 'ben',
          sessions: 11,
          speech_seconds: 4200.0,
          unmeasured_tracks: 0,
          first_seen_at: '2025-11-04T09:00:00+00:00',
          last_seen_at: '2026-08-21T12:00:00+00:00',
        },
      ],
    })

    expect(parsed.guild_id).toBe('4711')
    expect(parsed.sessions).toBe(42)
    expect(parsed.people).toEqual([
      {
        discord_user_id: '200',
        display_name: 'ben',
        sessions: 11,
        speech_seconds: 4200,
        unmeasured_tracks: 0,
        first_seen_at: '2025-11-04T09:00:00+00:00',
        last_seen_at: '2026-08-21T12:00:00+00:00',
      },
    ])
  })

  it('keeps an id as a string even when it arrived as a number', () => {
    // A snowflake past 2^53 loses its last digits as a JSON number, and in
    // a list that ranks people that is not a rounding error -- it is one
    // person's figures under another person's id.
    const parsed = parseGuildParticipation({ people: [{ discord_user_id: 200 }] })
    expect(parsed.people[0]!.discord_user_id).toBe('200')
  })

  it('drops an entry that names nobody', () => {
    // A row in a ranking of people that identifies nobody cannot be
    // checked and cannot be corrected by the person it is about.
    const parsed = parseGuildParticipation({
      people: [{ display_name: 'ben', sessions: 11 }, { discord_user_id: '200' }],
    })
    expect(parsed.people).toHaveLength(1)
    expect(parsed.people[0]!.discord_user_id).toBe('200')
  })

  it('keeps an absent speaking time absent rather than turning it into zero', () => {
    // The whole point of the column being nullable. Zero here would say
    // this named person sat through eleven meetings without speaking.
    const parsed = parseGuildParticipation({
      people: [{ discord_user_id: '200', speech_seconds: null, unmeasured_tracks: 3 }],
    })
    expect(parsed.people[0]!.speech_seconds).toBeNull()
    expect(parsed.people[0]!.unmeasured_tracks).toBe(3)
  })

  it('refuses a negative count and a nonsensical duration', () => {
    const parsed = parseGuildParticipation({
      sessions: -4,
      people: [{ discord_user_id: '200', sessions: -1, speech_seconds: Number.NaN, unmeasured_tracks: -2 }],
    })
    expect(parsed.sessions).toBe(0)
    expect(parsed.people[0]!.sessions).toBe(0)
    // Nonsense collapses to null rather than to zero: "we do not know" is
    // true of a broken figure and "they said nothing" is not.
    expect(parsed.people[0]!.speech_seconds).toBeNull()
    expect(parsed.people[0]!.unmeasured_tracks).toBe(0)
  })

  it('yields a well-formed ranking for a payload that is not one', () => {
    const parsed = parseGuildParticipation('nonsense')
    expect(parsed).toEqual({ guild_id: null, sessions: 0, people: [] })
  })

  it('escapes the guild id in the path', () => {
    expect(participationPath('4711')).toBe('/guilds/4711/report/participation')
    expect(participationPath('a/b')).toBe('/guilds/a%2Fb/report/participation')
  })
})

describe('the order of the rows', () => {
  it('leaves the order exactly as the API sent it', () => {
    // The server orders this list -- most meetings first, ties broken by
    // name and then by id -- and a second ordering in the browser would be
    // a second definition of the order. A payload that arrives in an order
    // this module would not have chosen is still shown in that order.
    const rows = participationRows(
      ranking({
        people: [
          person({ discord_user_id: '1', display_name: 'ana', sessions: 3 }),
          person({ discord_user_id: '2', display_name: 'ben', sessions: 30 }),
          person({ discord_user_id: '3', display_name: 'cai', sessions: 12 }),
        ],
      }),
    )
    expect(rows.map((row) => row.name)).toEqual(['ana', 'ben', 'cai'])
  })

  it('shares a place between people with the same attendance', () => {
    // Two people who were each in eleven meetings are not first and
    // second. Printing them that way would invent a distinction out of the
    // tie-break, which is alphabetical and about their names rather than
    // about them.
    const rows = participationRows(
      ranking({
        people: [
          person({ discord_user_id: '1', display_name: 'ana', sessions: 20 }),
          person({ discord_user_id: '2', display_name: 'ben', sessions: 11 }),
          person({ discord_user_id: '3', display_name: 'cai', sessions: 11 }),
          person({ discord_user_id: '4', display_name: 'dee', sessions: 2 }),
        ],
      }),
    )
    expect(rows.map((row) => row.rank)).toEqual([1, 2, 2, 4])
    expect(rows.map((row) => row.tied)).toEqual([false, true, true, false])
  })

  it('keys every row by the id, so no two rows can collide', () => {
    const rows = participationRows(
      ranking({
        people: [person({ discord_user_id: '1' }), person({ discord_user_id: '2' })],
      }),
    )
    expect(new Set(rows.map((row) => row.key)).size).toBe(2)
  })
})

describe('naming a person', () => {
  it('uses the display name when there is one', () => {
    expect(participationPersonLabel(person({ display_name: 'ben' }))).toBe('ben')
    expect(participationIdentityNote(person({ display_name: 'ben' }))).toBeNull()
  })

  it('shows the whole id when there is no name, and says it is an id', () => {
    // Never a shortened one: snowflakes minted in the same era share their
    // leading digits, so a truncated id identifies a group.
    const nameless = person({ display_name: null, discord_user_id: '987654321098765432' })
    expect(participationPersonLabel(nameless)).toBe('Discord user 987654321098765432')

    const note = participationIdentityNote(nameless)!
    expect(note).toContain('This is a Discord user id, not a name')
    expect(note).toContain('no display name on record for them in this server')
    expect(note).toContain('an id is a poor thing to put in a list about people')
  })
})

describe('how much of the server one person was present for', () => {
  it('says it out of the total, never as a bare count', () => {
    // A percentage survives being quoted without its scope; "11 of the 42
    // this server has recorded" carries it.
    const line = participationAttendanceLine(person({ sessions: 11 }), 42)
    expect(line).toBe('Recorded in 11 meetings of the 42 this server has recorded.')
    expect(line).not.toContain('%')
  })

  it('counts one meeting as a meeting', () => {
    expect(participationAttendanceLine(person({ sessions: 1 }), 42)).toContain('1 meeting of the 42')
  })

  it('says so when there is no total to read the figure against', () => {
    const line = participationAttendanceLine(person({ sessions: 11 }), 0)
    expect(line).toContain('out of a total this ranking does not know')
    expect(line).toContain('nothing to read the figure against')
  })

  it('puts the total above the rows as well as inside them', () => {
    const line = participationScopeLine(ranking({ sessions: 42, people: [person(), person({ discord_user_id: '3' })] }))
    expect(line).toContain('2 people')
    expect(line).toContain('42 meetings')
    expect(line).toContain('Equal attendance shares a place')
    expect(line).toContain('the order within a tie is alphabetical and means nothing')
  })

  it('says a place means little when the server reported no meeting count', () => {
    const line = participationScopeLine(ranking({ sessions: 0 }))
    expect(line).toContain('no meeting count for this server')
    expect(line).toContain('a place in this order means very little without one')
  })
})

describe('speaking time', () => {
  it('reads an absent measurement as an absence and never as silence', () => {
    const line = participationSpeechLine(person({ speech_seconds: null, unmeasured_tracks: 4 }))
    expect(line).toContain('No speaking time was ever measured for them')
    expect(line).toContain('all 4 of their recordings here predate the columns that hold it')
    expect(line).toContain('a measurement nobody took, not a person who said nothing')
    expect(line).not.toContain('0 s')
  })

  it('says it does not know why when there is no measurement and no explanation', () => {
    const line = participationSpeechLine(person({ speech_seconds: null, unmeasured_tracks: 0 }))
    expect(line).toContain('holds no measured speaking time for them, and does not say why')
    expect(line).toContain('missing measurement rather than as silence')
  })

  it('says how far short the figure falls when some recordings were never measured', () => {
    const line = participationSpeechLine(person({ speech_seconds: 4200, unmeasured_tracks: 3 }))
    expect(line).toContain('1 h 10 min of speech')
    expect(line).toContain('3 of their recordings here were never measured at all')
    expect(line).toContain('falls short by an unknown amount')
  })

  it('says what a complete figure is and is not a measure of', () => {
    const line = participationSpeechLine(person({ speech_seconds: 4200, unmeasured_tracks: 0 }))
    expect(line).toContain('1 h 10 min of speech')
    expect(line).toContain('how long a microphone was open on speech')
    expect(line).toContain('not a measure of anything else')
  })

  it('marks a row with no measurement so the page can render it as an absence', () => {
    const rows = participationRows(
      ranking({
        people: [
          person({ discord_user_id: '1', speech_seconds: null }),
          person({ discord_user_id: '2', speech_seconds: 0 }),
        ],
      }),
    )
    // Nobody ever measured, against somebody measured it and heard
    // nothing. Two different facts, and only the first is an absence.
    expect(rows[0]!.speechAbsent).toBe(true)
    expect(rows[1]!.speechAbsent).toBe(false)
  })
})

describe('when somebody was recorded here', () => {
  it('gives both ends of the span', () => {
    const line = participationSeenLine(person())
    expect(line).toContain('First recorded 4 Nov 2025, 09:00 UTC')
    expect(line).toContain('most recently 21 Aug 2026, 12:00 UTC')
  })

  it('says once rather than printing the same instant twice', () => {
    const at = '2026-08-21T12:00:00+00:00'
    expect(participationSeenLine(person({ first_seen_at: at, last_seen_at: at }))).toBe(
      'Recorded here once, on 21 Aug 2026, 12:00 UTC.',
    )
  })

  it('says which end is missing rather than standing a dash in for half a range', () => {
    const line = participationSeenLine(person({ last_seen_at: null }))
    expect(line).toContain('Only one end of their span here is known')
    expect(line).toContain('4 Nov 2025, 09:00 UTC')
  })

  it('says nothing is known when neither end is', () => {
    const line = participationSeenLine(person({ first_seen_at: null, last_seen_at: null }))
    expect(line).toContain('did not say when they were first or last recorded here')
  })

  it('is on every row, because a rank read without it is read as a fact about the present', () => {
    // Somebody who joined in June is behind a colleague who has been here
    // since November for a reason that is about neither of them.
    const row = participationRows(ranking())[0]!
    expect(row.detail).toContain(row.attendance)
    expect(row.detail).toContain(row.speech)
    expect(row.detail).toContain(row.seen)
    expect(row.detail.startsWith('ben.')).toBe(true)
  })
})

describe('what the reader is told, and when', () => {
  it('calls it a ranking of named people in the first sentence', () => {
    // Not "engagement", not "activity". Somebody who quotes this list
    // elsewhere should have had to read that sentence first.
    expect(PARTICIPATION_STANDING_NOTE).toContain('a ranking of named people')
    expect(PARTICIPATION_STANDING_NOTE).toContain('how long their microphone carried speech')
  })

  it('says it is a different kind of report from the figures above it', () => {
    expect(PARTICIPATION_STANDING_NOTE).toContain('a different kind of report from the figures above it')
    expect(PARTICIPATION_STANDING_NOTE).toContain('those describe a server, this describes the individuals in it')
  })

  it('says that opening it is written to the audit log, and what the log holds', () => {
    // Before the request goes out, because afterwards is too late to
    // decline -- and the log holds who looked, never who was looked at.
    expect(PARTICIPATION_STANDING_NOTE).toContain('written to the audit log')
    expect(PARTICIPATION_STANDING_NOTE).toContain('which server, who looked, and when')
    expect(PARTICIPATION_STANDING_NOTE).toContain('never who was in it')
  })

  it('says outright that attendance is not a measure of contribution', () => {
    expect(PARTICIPATION_CONTRIBUTION_NOTE).toContain(
      'Being present in more meetings is not a measure of contribution, and speaking time even less so.',
    )
  })

  it('names what is invisible to it, so attendance is not read as a complete record', () => {
    expect(PARTICIPATION_CONTRIBUTION_NOTE).toContain('attendance in the voice channels Sturnus records')
    expect(PARTICIPATION_CONTRIBUTION_NOTE).toContain('a channel Sturnus does not watch')
    expect(PARTICIPATION_CONTRIBUTION_NOTE).toContain('has not consented to being recorded')
    expect(PARTICIPATION_CONTRIBUTION_NOTE).toContain('a low place on this list is not evidence of anything')
  })

  it('says this is a further purpose than the recordings were made for', () => {
    expect(PARTICIPATION_PURPOSE_NOTE).toContain('made in order to write meetings up')
    expect(PARTICIPATION_PURPOSE_NOTE).toContain('a further purpose than that one')
  })

  it('names co-determination rather than leaving it to a design document', () => {
    expect(PARTICIPATION_PURPOSE_NOTE).toContain('subject to co-determination')
    expect(PARTICIPATION_PURPOSE_NOTE).toContain('BetrVG §87(1)(6)')
    expect(PARTICIPATION_PURPOSE_NOTE).toContain('agree on with a works council')
  })

  it('stands all three notes above the list at all times', () => {
    // Above the reveal control as well as above the loaded rows: a note
    // that appears only once the ranking is on screen arrives after the
    // decision it exists to inform.
    expect(participationNotes().map((note) => note.key)).toEqual(['what', 'meaning', 'purpose'])
    expect(participationNotes().map((note) => note.text)).toEqual([
      PARTICIPATION_STANDING_NOTE,
      PARTICIPATION_CONTRIBUTION_NOTE,
      PARTICIPATION_PURPOSE_NOTE,
    ])
    for (const note of participationNotes()) expect(note.label.length).toBeGreaterThan(0)
  })
})

describe('asking for it', () => {
  it('names what the control will load rather than saying "show more"', () => {
    // The click is the moment somebody becomes a person who looked at a
    // ranking of their colleagues; a generic label arranges for them to do
    // it by accident.
    expect(PARTICIPATION_REVEAL_LABEL).toBe('Show the attendance ranking')
    expect(PARTICIPATION_REVEAL_BUSY_LABEL).toBe('Reading the ranking…')
    expect(PARTICIPATION_LOADING_NOTE).toContain('attendance ranking')
  })

  it('says what pressing it does before it is pressed', () => {
    expect(PARTICIPATION_REVEAL_NOTE).toContain('Nothing has been loaded')
    expect(PARTICIPATION_REVEAL_NOTE).toContain('by name, ordered by how many meetings each of them was in')
    expect(PARTICIPATION_REVEAL_NOTE).toContain('writes a line in the audit log saying that you asked')
  })

  it('says why it is not loaded with the figures above it', () => {
    // The reason this section is on demand at all, on the page rather than
    // only in the code comment that implements it.
    expect(PARTICIPATION_REVEAL_NOTE).toContain('The figures above were loaded without any of that')
    expect(PARTICIPATION_REVEAL_NOTE).toContain('whether transcription is keeping up')
    expect(PARTICIPATION_REVEAL_NOTE).toContain('does not record you as having looked at a ranking of your colleagues')
  })

  it('does not pretend hiding it again unsays anything', () => {
    expect(PARTICIPATION_HIDE_LABEL).toBe('Hide the ranking')
    expect(PARTICIPATION_HIDE_NOTE).toContain('the audit line stays')
    expect(PARTICIPATION_HIDE_NOTE).toContain('hiding it afterwards does not change that it was')
  })
})

describe('a server with nobody in it', () => {
  it('knows an empty list from a list', () => {
    expect(isParticipationEmpty(ranking({ people: [] }))).toBe(true)
    expect(isParticipationEmpty(ranking())).toBe(false)
    // Nobody to rank even though meetings were recorded is still empty:
    // there is no row to draw for a person who does not exist in it.
    expect(isParticipationEmpty(ranking({ sessions: 42, people: [] }))).toBe(true)
  })

  it('says nobody is missing from an empty list', () => {
    expect(PARTICIPATION_EMPTY_HEADING).toBe('Sturnus has recorded nobody in this server')
    expect(PARTICIPATION_EMPTY_NOTE).toContain('There is nobody to list')
    expect(PARTICIPATION_EMPTY_NOTE).toContain('Nobody is missing from this list')
    expect(PARTICIPATION_EMPTY_NOTE).toContain('no row rather than a row of zeros')
    expect(PARTICIPATION_EMPTY_NOTE).toContain('a zero would say they attended nothing')
  })

  it('draws no rows for it', () => {
    expect(participationRows(ranking({ people: [] }))).toEqual([])
    expect(participationScopeLine(ranking({ people: [] }))).toContain('0 people')
  })
})

describe('what this list is never allowed to become', () => {
  it('uses none of the vocabulary of a leaderboard', () => {
    // Every sentence this module can produce, checked at once. Speaking
    // time is secondary to attendance and must never be presented as a
    // competing order, and the words that would do that are the words that
    // creep back in first.
    const prose = everySentence(
      ranking({
        people: [
          person({ discord_user_id: '1', display_name: 'ana', sessions: 20 }),
          person({ discord_user_id: '2', display_name: null, sessions: 11, speech_seconds: null, unmeasured_tracks: 4 }),
          person({ discord_user_id: '3', display_name: 'cai', sessions: 11, unmeasured_tracks: 2 }),
        ],
      }),
    ).toLowerCase()

    for (const forbidden of [
      'top talker',
      'top speaker',
      'leaderboard',
      'podium',
      'winner',
      'champion',
      'trophy',
      'medal',
      'most active',
      'talk share',
      'share of talk',
      'productivity',
      'engagement',
    ]) {
      expect(prose).not.toContain(forbidden)
    }
  })

  it('states no figure about a person as a percentage', () => {
    // A percentage is the form of this figure that travels furthest from
    // its own scope, which is exactly what a page about named individuals
    // must not hand out.
    expect(everySentence(ranking({ people: [person(), person({ discord_user_id: '3', sessions: 40 })] }))).not.toContain('%')
  })
})

describe('when the API says no', () => {
  it('says a session has ended rather than that the ranking is empty', () => {
    const message = describeParticipationError(failure(401))
    expect(message).toContain('Your session has ended')
    expect(message).toContain('the ranking was not loaded')
  })

  it('names what an administrator is when it refuses one who is not', () => {
    expect(describeParticipationError(failure(403))).toContain('admin_role_id')
  })

  it('covers both readings of a 404 without guessing which', () => {
    // The API answers 404 both for a server that does not exist and for
    // one the caller does not administer, on purpose: it will not confirm
    // the existence of a server to somebody with no business there.
    const message = describeParticipationError(failure(404))
    expect(message).toContain('does not know this server, or you no longer administer it')
    expect(message).toContain('answers the same way to both')
    expect(message).toContain('no ranking was loaded')
  })

  it('says a request that never arrived did not arrive', () => {
    const message = describeParticipationError(new Error('offline'))
    expect(message).toContain('Could not reach the API')
    expect(message).toContain('no ranking was loaded')
  })

  it('reports an unexpected status with the number and no invention', () => {
    const message = describeParticipationError(failure(503))
    expect(message).toContain('503')
    expect(message).toContain('Nothing is known about why')
  })

  it('never leaves the reader unsure whether they saw part of the list', () => {
    // A reader who is unsure will press the button again, and pressing it
    // again is another audit line.
    for (const status of [401, 403, 404, 500]) {
      expect(describeParticipationError(failure(status)).toLowerCase()).toMatch(/no ranking|not loaded|no ranking was/)
    }
  })
})
