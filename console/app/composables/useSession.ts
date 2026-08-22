/**
 * Who is signed in, asked once per navigation and shared by every component.
 *
 * `useState` rather than a module-level ref: on the server one process
 * renders for many people at once, and a module-level value would be shared
 * between them -- which is the bug where one person sees another's name.
 */
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
 * A 401 is not an error here -- it is the answer "nobody is signed in",
 * which is an ordinary state for a console whose front door is a login
 * page. Treating it as a failure would put an error banner in front of
 * every anonymous visitor.
 */
export async function loadSession(): Promise<ConsoleUser | null> {
  const session = useSession()
  if (session.value) return session.value
  const api = useApi()
  try {
    session.value = await api<ConsoleUser>('/me')
  } catch {
    session.value = null
  }
  return session.value
}
