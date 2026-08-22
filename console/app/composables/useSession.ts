/**
 * Who is signed in, asked once per navigation and shared by every component.
 *
 * `useState` rather than a module-level ref: on the server one process
 * renders for many people at once, and a module-level value would be shared
 * between them -- which is the bug where one person sees another's name.
 */
import { ApiError } from '~/utils/apiError'

export interface ConsoleUser {
  /** A string, not a number. A Discord snowflake exceeds JavaScript's safe
   *  integer range, where a JSON number silently loses its last digits and
   *  produces an id that looks right and names nobody. */
  discord_user_id: string
  is_admin: boolean
}

export function useSession() {
  return useState<ConsoleUser | null>('session', () => null)
}

/**
 * Loads the session if it has not been loaded yet.
 *
 * **A 401 is the only failure that means "nobody is signed in".** It is an
 * ordinary state for a console whose front door is a login page, and
 * treating it as an error would put a banner in front of every anonymous
 * visitor.
 *
 * Everything else is rethrown, and that distinction is the whole point.
 * Swallowing a 500 or a network blip as "anonymous" bounces a signed-in
 * person to the sign-in page -- where signing in then works perfectly --
 * which reads as a random logout and is the single most confusing thing a
 * session layer can do. The person cannot tell "your session ended" from
 * "the API had a bad second", and only one of those is their problem.
 */
export async function loadSession(): Promise<ConsoleUser | null> {
  const session = useSession()
  if (session.value) return session.value
  const api = useApi()
  try {
    session.value = await api<ConsoleUser>('/me')
  } catch (error) {
    if (error instanceof ApiError && error.isUnauthenticated) {
      session.value = null
      return null
    }
    throw error
  }
  return session.value
}
