/**
 * The instant an administrator's withdrawal takes effect, and the arithmetic
 * that gets it to the API intact.
 *
 * `revoked_at` used to be a tombstone -- any non-null value meant "not
 * active", and it was always stamped with `now()`. §5.4 of the
 * personalisation spec makes it an *effective instant*: a past one is a
 * statement about recordings that already exist, a future one is a
 * scheduled withdrawal, and the bot's five-second consent cache makes the
 * scheduled case work with no new mechanism at all.
 *
 * Three decisions live here rather than in the page, and each of them is
 * the kind that is wrong silently:
 *
 * - **A naive instant is a 400.** `POST .../revoke` rejects an ISO-8601
 *   string with no offset, and a `datetime-local` input produces exactly
 *   such a string. This module is the only place the offset is attached,
 *   and it attaches it explicitly -- never `Z`, never `toISOString()` on a
 *   value the browser already interpreted in some zone or other.
 * - **An instant before `granted_at` is refused here.** A withdrawal cannot
 *   take effect before the consent it withdraws. Sending it would earn a
 *   400 and a sentence nobody wrote; refusing it client-side earns a
 *   sentence somebody did.
 * - **Whether the chosen instant is in the past decides what the
 *   confirmation has to say.** Choosing a past instant and being shown
 *   nothing would let somebody believe they had erased something.
 *
 * **Every function here returns a translation key, never a sentence** -- see
 * `i18n/README.md`. Keys live under `admin.consents.effective.*`, the
 * namespace `i18n/README.md` already reserves for this page.
 */
import { formatCount, formatMoment } from '~/utils/format'
import type { Line } from '~/utils/myConsents'

/** What a `datetime-local` input holds: wall-clock time with no zone. */
const LOCAL_INPUT = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/

function pad(value: number): string {
  return String(value).padStart(2, '0')
}

/**
 * An offset written the way ISO-8601 writes it.
 *
 * The argument uses `Date.prototype.getTimezoneOffset`'s sign -- **minutes
 * to add to local time to reach UTC** -- because that is what the browser
 * hands out, and converting the sign at the boundary is how the sign gets
 * converted twice somewhere. Berlin in summer is `-120` and renders as
 * `+02:00`.
 *
 * UTC renders as `+00:00` rather than `Z`. Both are legal and one of them
 * is a special case; a formatter with no special cases cannot get its
 * special case wrong.
 */
export function offsetLabel(offsetMinutes: number): string {
  if (!Number.isFinite(offsetMinutes)) return '+00:00'
  const east = -Math.round(offsetMinutes)
  const sign = east < 0 ? '-' : '+'
  const size = Math.abs(east)
  return `${sign}${pad(Math.floor(size / 60))}:${pad(size % 60)}`
}

/**
 * A `datetime-local` value as an ISO-8601 instant carrying its offset.
 *
 * `null` for anything that is not a real moment -- a half-typed value, or
 * `2026-02-30`, which the input's own validation lets through in some
 * browsers and `Date` silently rolls forward into March.
 *
 * Deliberately built by string assembly rather than through `Date`. Going
 * via a `Date` means asking a `Date` what zone it is in, and the answer
 * depends on where the code is running -- which on a server-rendered page
 * is a different machine from the one the administrator is sitting at.
 */
export function isoFromLocalInput(value: string, offsetMinutes: number): string | null {
  const match = LOCAL_INPUT.exec(value.trim())
  if (!match) return null
  const [, year, month, day, hour, minute, second = '00'] = match as unknown as string[]
  const y = Number(year)
  const mo = Number(month)
  const d = Number(day)
  const h = Number(hour)
  const mi = Number(minute)
  const s = Number(second)
  if (mo < 1 || mo > 12 || d < 1 || d > 31 || h > 23 || mi > 59 || s > 59) return null
  // `2026-02-30` parses digit by digit and is not a day. `Date.UTC` rolls
  // it into March rather than refusing, so the roll-forward is what is
  // detected: a date that survives the round trip is a date that exists.
  const probe = new Date(Date.UTC(y, mo - 1, d, h, mi, s))
  if (
    probe.getUTCFullYear() !== y
    || probe.getUTCMonth() !== mo - 1
    || probe.getUTCDate() !== d
  ) {
    return null
  }
  return `${year}-${month}-${day}T${hour}:${minute}:${pad(s)}${offsetLabel(offsetMinutes)}`
}

/**
 * An instant as a `datetime-local` value, in the zone described by
 * `offsetMinutes`.
 *
 * The other direction, used to seed the control with the consent's grant
 * date so that "not before this" is a bound somebody can see rather than
 * one they discover by tripping over it.
 */
export function localInputFromIso(iso: string, offsetMinutes: number): string | null {
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return null
  if (!Number.isFinite(offsetMinutes)) return null
  const shifted = new Date(at.getTime() - Math.round(offsetMinutes) * 60_000)
  return (
    `${shifted.getUTCFullYear()}-${pad(shifted.getUTCMonth() + 1)}-${pad(shifted.getUTCDate())}`
    + `T${pad(shifted.getUTCHours())}:${pad(shifted.getUTCMinutes())}`
  )
}

/**
 * The browser's offset **for the chosen wall-clock moment**, not for now.
 *
 * This is the whole of the daylight-saving correctness of this feature. An
 * administrator in Berlin choosing a January instant in July gets `+01:00`,
 * not `+02:00`, because `new Date(year, month, …)` is interpreted in the
 * host zone and therefore knows which side of the change it falls on.
 * Reading `new Date().getTimezoneOffset()` instead would be right for half
 * the year and an hour out for the other half -- and an hour is exactly the
 * error nobody notices until a recording lands on the wrong side of a
 * withdrawal.
 *
 * The fallback is the offset of `reference`, which is `now` unless a test
 * says otherwise: a value too broken to parse is one no instant will be
 * built from anyway.
 */
export function localOffsetMinutes(value: string, reference: Date = new Date()): number {
  const match = LOCAL_INPUT.exec(value.trim())
  if (!match) return reference.getTimezoneOffset()
  const [, year, month, day, hour, minute, second = '00'] = match as unknown as string[]
  const local = new Date(
    Number(year),
    Number(month) - 1,
    Number(day),
    Number(hour),
    Number(minute),
    Number(second),
  )
  return Number.isNaN(local.getTime()) ? reference.getTimezoneOffset() : local.getTimezoneOffset()
}

/* -------------------------------------------------------------------- */
/* Whether the instant may be sent at all                                */
/* -------------------------------------------------------------------- */

export type InstantVerdict = { ok: true } | { ok: false, problem: Line }

/**
 * Whether this instant can be the effective moment of a withdrawal.
 *
 * Two refusals, both client-side, both producing a sentence rather than a
 * 400 with a body `useApi` deliberately throws away:
 *
 * - It is not a moment. A `datetime-local` cleared to nothing, or a value
 *   typed into a browser that renders the control as a text field.
 * - It is before the consent was granted. §5.4 permits any instant *not
 *   before* `granted_at`, and the API enforces that; a withdrawal effective
 *   before the grant it withdraws describes no interval at all.
 *
 * A consent whose `granted_at` was never recorded has no lower bound to
 * check against, and this refuses nothing on the strength of a field that
 * is missing. The API is still the authority.
 */
export function validateEffectiveAt(iso: string | null, grantedAt: string | null): InstantVerdict {
  if (!iso) return { ok: false, problem: { key: 'admin.consents.effective.unreadable' } }
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) {
    return { ok: false, problem: { key: 'admin.consents.effective.unreadable' } }
  }
  if (grantedAt) {
    const granted = new Date(grantedAt)
    if (!Number.isNaN(granted.getTime()) && at.getTime() < granted.getTime()) {
      return {
        ok: false,
        problem: {
          key: 'admin.consents.effective.beforeGrant',
          values: { granted: formatMoment(grantedAt) },
        },
      }
    }
  }
  return { ok: true }
}

/* -------------------------------------------------------------------- */
/* What the chosen instant means                                         */
/* -------------------------------------------------------------------- */

/**
 * `now` is a state of its own, not a past instant that happens to be close.
 *
 * Pressing straight through the control without opening it must behave
 * exactly as it did before this change -- no instant is sent, the API
 * stamps its own -- so "now" is what *no choice* produces. A chosen instant
 * within a minute of now is folded into it as well: the difference is not
 * one an administrator meant to express, and it is not one worth a
 * paragraph about recordings that already exist.
 */
export type EffectiveKind = 'now' | 'past' | 'future'

const NEAR_ENOUGH_MS = 60_000

export function effectiveKind(iso: string | null, now: Date = new Date()): EffectiveKind {
  if (!iso) return 'now'
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return 'now'
  const delta = at.getTime() - now.getTime()
  if (Math.abs(delta) <= NEAR_ENOUGH_MS) return 'now'
  return delta < 0 ? 'past' : 'future'
}

/**
 * What the confirmation must add because of the instant that was chosen.
 *
 * The past case is the one this exists for. A withdrawal effective last
 * Tuesday is a statement about recordings that already exist, and somebody
 * who chose it and was shown nothing could reasonably conclude they had
 * erased them. So the count of recordings this person's audio is in is
 * named -- it is the upper bound on what falls inside the interval; the
 * exact figure comes back in `recordings_from_effective_at` and is reported
 * afterwards -- and it is immediately followed by the fact that none of
 * them are deleted.
 *
 * The future case says the opposite thing and has to say it, because a
 * scheduled withdrawal that looks like an immediate one is an administrator
 * believing recording has stopped when it has not.
 */
export function effectiveConsequence(
  kind: EffectiveKind,
  iso: string | null,
  recordingsHeld: number,
): Line[] {
  if (kind === 'now') return []
  const when = formatMoment(iso)
  if (kind === 'future') {
    return [{ key: 'admin.consents.effective.futureNote', values: { when } }]
  }
  const held = Math.max(0, Math.round(recordingsHeld))
  return [
    held === 0
      ? { key: 'admin.consents.effective.pastNone', values: { when } }
      : {
          key: held === 1
            ? 'admin.consents.effective.pastOne'
            : 'admin.consents.effective.pastMany',
          values: { when, count: formatCount(held) },
        },
    { key: 'admin.consents.effective.notDeletion' },
  ]
}

/**
 * What the API actually did, once it has done it.
 *
 * `effective_at` is echoed back rather than assumed, and the count with it,
 * because the console's arithmetic and the API's are two different
 * arithmetics and only one of them has the recordings table.
 */
export function effectiveOutcome(
  effectiveAt: string | null,
  recordingsFrom: number,
): Line[] {
  if (!effectiveAt) return []
  const when = formatMoment(effectiveAt)
  const held = Math.max(0, Math.round(recordingsFrom))
  return [
    { key: 'admin.consents.effective.tookEffect', values: { when } },
    held === 0
      ? { key: 'admin.consents.effective.fromNone', values: { when } }
      : {
          key: held === 1
            ? 'admin.consents.effective.fromOne'
            : 'admin.consents.effective.fromMany',
          values: { when, count: formatCount(held) },
        },
  ]
}
