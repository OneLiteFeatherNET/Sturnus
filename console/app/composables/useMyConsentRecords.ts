/**
 * A person's own consent records, read once and shared by every page that
 * shows them.
 *
 * Two pages now do: `/settings`, where consent is the section the page
 * exists for, and the dashboard, where it is the band a participant most
 * likely came for. One key, one handler, one parse. Two copies of this fetch
 * would be two places to forget that the endpoint answers a `{consents: …}`
 * envelope, and two places for the ordering to drift -- and a list that
 * reshuffles itself between two pages is a list nobody trusts.
 *
 * The 404 is deliberately *not* handled here. `GET /api/me/consents` does
 * not exist until the API that serves it is deployed, and what to say about
 * that differs between the two pages -- a whole section on one, one sentence
 * on the other. `isConsentServiceMissing` in `~/utils/myConsents` is the
 * shared answer to "is this that"; the sentence is each page's own.
 */
import {
  type MyConsent,
  orderMyConsents,
  parseMyConsents,
} from '~/utils/myConsents'

/**
 * @param options Passed through to `useAsyncData`. The dashboard asks for
 * `lazy` so the band arrives after the figures rather than holding the whole
 * page on the server; `/settings` does not, because a spinner where the fact
 * belongs is a page that makes somebody wait to find out whether they are
 * being recorded.
 */
export function useMyConsentRecords(options?: { lazy?: boolean }) {
  const api = useApi()
  return useAsyncData<MyConsent[]>(
    'my-consents',
    async () => orderMyConsents(parseMyConsents(await api('/me/consents'))),
    options,
  )
}
