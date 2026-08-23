<script setup lang="ts">
/**
 * One recording, at its own address, divided into the questions people
 * arrive with.
 *
 * `/recordings/{id}` is canonical: a link in a protocol, a chat message or
 * a bookmark lands on the recording itself rather than on a list somebody
 * then has to search through. Since #141 it is also the **only** place a
 * recording can be heard — the list is a list again — so everything that
 * page stopped showing had to have somewhere to live here.
 *
 * **Tabs, not one long scroll.** What was here was a metadata card, a tag
 * editor, a transport, an administrator's panel and then one `<audio>` and
 * one spectrogram per speaker, stacked. Every part of it was worth having
 * and no two of them answered the same question, which is the shape a tab
 * bar is for. `~/utils/recordingTabs` holds the division and the argument
 * for it: which four, why there is no metadata tab, why the re-queue panel
 * is on `details`, and why `meeting` is the one a bare address opens.
 *
 * `UiTabs` mounts a panel the first time it is shown and keeps it
 * afterwards, and that matters here more than anywhere else in the
 * console: the tracks tab is one `<audio>` and one spectrogram request per
 * speaker, and an eight-speaker meeting would otherwise fire sixteen
 * requests at somebody who came for the protocol link.
 *
 * **Both the session and its transcript are fetched on arrival, and only
 * one of them is lazy.** The transcript response is the only thing that
 * says whether this session still has any audio, and *two* tabs have to
 * know that before anybody clicks them — a player that is going to 404 has
 * to say why instead of looking broken. So the words come with the page;
 * what the tab bar defers is rendering them, which is where the cost of a
 * ninety-minute meeting actually is. A transcript that fails to load is
 * not a failed page: the session is still here, the audio still plays, and
 * the transcript tab says what happened.
 *
 * A 404 from the API is not an error page here. Somebody who was not in a
 * session and somebody following a link to one that never existed get the
 * same answer, and it is deliberately the same — see the audio endpoint's
 * module docstring for why that is a security property and not a
 * politeness.
 */
import UiTabs from '~/components/ui/UiTabs.vue'
import {
  formatSeconds,
  formatTimestamp,
  hasProtocol,
  isInProgress,
  recordingNaming,
  sessionLength,
  type RecordedSession,
} from '~/utils/recordings'
import { recordingTabQuery, recordingTabs } from '~/utils/recordingTabs'
import { transcriptAudioErased, transcriptPath, type SessionTranscript } from '~/utils/transcript'
import type { SessionName } from '~/utils/sessionNaming'
import { ApiError } from '~/utils/apiError'

/** Everything the page needs before it can render anything. */
interface Recording {
  session: RecordedSession
  /** The words, or `null` when the transcript alone could not be read.
   *  The session is authoritative for the page's existence; a transcript
   *  that failed is one tab's problem and not five. */
  transcript: SessionTranscript | null
}

const { t } = useI18n()
const say = useSay()

const route = useRoute()
const api = useApi()
const id = computed(() => String(route.params.id ?? ''))

const { data, status, error, refresh } = await useAsyncData<Recording>(
  () => `recording-${id.value}`,
  async () => {
    const sessionId = id.value
    // In parallel. They are two independent reads behind the same
    // authorisation, and doing them one after the other would double the
    // wait for no answer either of them needs from the other.
    const [session, transcript] = await Promise.all([
      api<RecordedSession>(`/sessions/${encodeURIComponent(sessionId)}`),
      // Swallowed on purpose, and only here: a transcript that cannot be
      // read must not take down the audio, the tags or the metadata. What
      // it costs is the distinction between a 500 and a 404 on this one
      // call, and neither of those is a sentence the transcript tab needs
      // — "the words could not be read" covers both, and the session's own
      // 404 is what decides whether this recording is anybody's at all.
      api<SessionTranscript>(transcriptPath(sessionId)).catch(() => null),
    ])
    return { session, transcript }
  },
  { watch: [id] },
)

/**
 * What somebody has just renamed this to, laid over what arrived.
 *
 * The heading above the tabs is a title, and the editor that writes it is
 * three tabs away — so without this, saving a name leaves the page
 * disagreeing with the box it was typed into until a reload. Cleared when
 * the address changes, because it belongs to one recording.
 */
const renamed = ref<SessionName | null>(null)
watch(id, () => {
  renamed.value = null
})

const session = computed<RecordedSession | null>(() => {
  const found = data.value?.session ?? null
  if (found === null) return null
  return renamed.value === null ? found : { ...found, ...renamed.value }
})
const transcript = computed<SessionTranscript | null>(() => data.value?.transcript ?? null)
const length = computed(() => (session.value ? sessionLength(session.value) : null))
const naming = computed(() => (session.value ? recordingNaming(session.value) : null))

/** The retention sweep has taken the recordings and left the minutes.
 *  Needs the track count as well as the flag: a session nobody consented
 *  to answers `audio_available: false` too, and "your recording was
 *  deleted" is not what happened to that one. */
const audioGone = computed(() =>
  transcriptAudioErased(transcript.value, session.value?.tracks.length ?? 0),
)

/** A session this viewer may not see, and one that does not exist, are the
 *  same answer. Distinguishing them here would undo the endpoint's care in
 *  not distinguishing them. */
const missing = computed(() => error.value instanceof ApiError && error.value.status === 404)

const tabs = computed(() => recordingTabs(t))
/** Where the audio tabs send somebody once there is no audio left. */
const toTranscript = computed(() => ({
  path: route.path,
  query: recordingTabQuery(route.query, 'transcript'),
}))
const toDetails = computed(() => ({
  path: route.path,
  query: recordingTabQuery(route.query, 'details'),
}))

useHead(() => ({
  title: naming.value
    ? t('recordings.headTitle', { name: say(naming.value.heading) })
    : t('recordings.one'),
}))

const timeZone = ref('UTC')
onMounted(() => {
  try {
    timeZone.value = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  } catch {
    // Keep UTC; see `formatTimestamp`.
  }
})

const others = computed(() => session.value?.other_participants ?? [])

function onRenamed(name: SessionName) {
  renamed.value = name
}
</script>

<template>
  <div class="mx-auto flex max-w-5xl flex-col gap-4">
    <NuxtLink
      to="/recordings"
      class="text-sm transition-colors hover:underline"
      :style="{ color: 'var(--text-muted)' }"
    >
      {{ $t('recordings.backToAll') }}
    </NuxtLink>

    <!-- The header, the bar and the panel, in the three sizes this page
         is. The back link above is real and stays put, so the only thing
         that moves when the recording lands is the recording. -->
    <div
      v-if="status === 'pending' && !error"
      aria-busy="true"
      class="flex flex-col gap-4"
    >
      <p class="sr-only">{{ $t('recordings.loadingOne') }}</p>
      <div
        class="h-44 animate-pulse rounded-2xl border motion-reduce:animate-none"
        :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
      />
      <div
        class="h-9 w-80 max-w-full animate-pulse rounded-t-lg motion-reduce:animate-none"
        :style="{ background: 'var(--surface)' }"
      />
      <div
        class="h-72 animate-pulse rounded-2xl border motion-reduce:animate-none"
        :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
      />
    </div>

    <div
      v-else-if="missing"
      class="rounded-2xl border p-6"
      :style="{ borderColor: 'var(--border)' }"
    >
      <p class="text-sm font-medium">{{ $t('recordings.missingHeading') }}</p>
      <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ $t('recordings.missingDetail') }}
      </p>
    </div>

    <div
      v-else-if="error"
      class="rounded-2xl border p-6"
      :style="{ borderColor: 'var(--danger)' }"
    >
      <p class="text-sm font-medium">{{ $t('recordings.oneFailedHeading') }}</p>
      <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ $t('recordings.oneFailedDetail') }}
      </p>
      <!-- Disabled rather than replaced while the retry runs: the button
           that started it is the one that would vanish, and a control that
           unmounts itself when pressed drops the keyboard to the top of
           the document. -->
      <button
        type="button"
        class="mt-3 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)] disabled:opacity-60"
        :style="{ color: 'var(--action)' }"
        :disabled="status === 'pending'"
        @click="refresh()"
      >
        {{ status === 'pending' ? $t('recordings.retrying') : $t('error.retry') }}
      </button>
    </div>

    <template v-else-if="session && naming">
      <!-- Above the bar, because it identifies the meeting every tab is
           about. This is where the answer to "is there a metadata tab"
           lives: somebody reading the transcript should not have to leave
           it to find out when the meeting was. -->
      <header
        class="rounded-2xl border p-5"
        :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
      >
        <h1 class="flex flex-wrap items-center gap-2 text-2xl font-semibold">
          <span>{{ say(naming.heading) }}</span>
          <span
            v-if="isInProgress(session)"
            class="rounded-full px-2 py-0.5 text-xs font-medium"
            :style="{ background: 'var(--positive)', color: 'var(--positive-contrast)' }"
          >
            {{ $t('recordings.recordingNow') }}
          </span>
        </h1>
        <!-- Where it happened, once the heading has stopped saying so. A
             named meeting is called what somebody named it; the channel is
             still a fact and drops to a subordinate line rather than
             disappearing. -->
        <p v-if="naming.under" class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
          {{ say(naming.under) }}
        </p>

        <dl class="mt-4 grid grid-cols-2 gap-x-6 gap-y-3 text-sm sm:grid-cols-4">
          <div>
            <dt :style="{ color: 'var(--text-muted)' }">{{ $t('recordings.startedLabel') }}</dt>
            <dd class="mt-0.5 tabular-nums">{{ formatTimestamp(session.started_at, timeZone) }}</dd>
          </div>
          <div>
            <dt :style="{ color: 'var(--text-muted)' }">{{ $t('recordings.endedLabel') }}</dt>
            <dd class="mt-0.5 tabular-nums">
              {{ session.ended_at ? formatTimestamp(session.ended_at, timeZone) : NOT_MEASURED }}
            </dd>
          </div>
          <div>
            <dt :style="{ color: 'var(--text-muted)' }">{{ $t('recordings.lengthLabel') }}</dt>
            <dd class="mt-0.5 tabular-nums">
              {{ length !== null ? formatSeconds(length) : $t('recordings.lengthNotKnown') }}
            </dd>
          </div>
          <div>
            <dt :style="{ color: 'var(--text-muted)' }">{{ $t('recordings.speakersLabel') }}</dt>
            <dd class="mt-0.5 tabular-nums">{{ $n(session.tracks.length) }}</dd>
          </div>
        </dl>

        <!-- Retention, said once and above the bar, because it explains
             the state of two tabs and has to be legible from the third.
             Not in the danger role: a recording deleted when its window
             closed is the system keeping a promise, not a fault. -->
        <div
          v-if="audioGone"
          class="mt-4 rounded-lg border border-dashed p-3"
          :style="{ borderColor: 'var(--border)' }"
        >
          <p class="text-sm font-medium">{{ $t('recordings.audioGoneHeading') }}</p>
          <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
            {{ $t('recordings.audioGoneDetail') }}
          </p>
        </div>
      </header>

      <UiTabs :tabs="tabs" :label="$t('recordings.tabsLabel')">
        <!-- The meeting, on one clock. -->
        <template #meeting>
          <div class="flex flex-col gap-4">
            <section
              class="rounded-2xl border p-5"
              :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
            >
              <h2 class="text-base font-semibold">{{ $t('recordings.wholeMeetingHeading') }}</h2>
              <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
                {{ $t('recordings.wholeMeetingNote') }}
              </p>

              <!-- "Nobody consented" is asked first and is the more
                   specific answer: a session with no tracks reports no
                   available audio for the honest reason that there never
                   was any, and calling that an erasure would report a loss
                   that did not happen. -->
              <p
                v-if="session.tracks.length === 0"
                class="mt-4 rounded-lg border border-dashed p-4 text-sm"
                :style="{ borderColor: 'var(--border)', color: 'var(--text-muted)' }"
              >
                {{ $t('recordings.noAudio') }}
              </p>
              <p
                v-else-if="audioGone"
                class="mt-4 rounded-lg border border-dashed p-4 text-sm"
                :style="{ borderColor: 'var(--border)', color: 'var(--text-muted)' }"
              >
                {{ $t('recordings.audioGoneNothingToPlay') }}
                <NuxtLink
                  :to="toTranscript"
                  class="font-medium transition-colors hover:underline"
                  :style="{ color: 'var(--action)' }"
                >
                  {{ $t('recordings.audioGoneReadInstead') }}
                </NuxtLink>
              </p>
              <MultiTrackPlayer v-else :session="session" />
            </section>

            <section
              class="rounded-2xl border p-5"
              :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
            >
              <h2 class="text-base font-semibold">{{ $t('recordings.aboutHeading') }}</h2>

              <div class="mt-3 flex flex-wrap items-center gap-3">
                <a
                  v-if="hasProtocol(session)"
                  :href="session.document_url ?? ''"
                  target="_blank"
                  rel="noreferrer"
                  class="rounded-lg px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)]"
                  :style="{ color: 'var(--action)' }"
                >
                  {{ $t('recordings.openProtocol') }}
                </a>
                <span v-else class="text-sm" :style="{ color: 'var(--text-muted)' }">
                  {{ $t('recordings.noProtocolWritten') }}
                </span>
              </div>

              <!-- What somebody wrote about the meeting, shown where a
                   listener wants it and edited on `details`. The rest of
                   this page is the reading room; that tab is the desk.
                   `whitespace-pre-line` because a description keeps its
                   paragraphs and the API keeps them too. -->
              <p
                v-if="session.description"
                class="mt-3 max-w-3xl whitespace-pre-line text-sm"
              >
                {{ session.description }}
              </p>
              <p v-else class="mt-3 text-sm" :style="{ color: 'var(--text-muted)' }">
                {{ $t('recordings.noDescriptionYet') }}
                <NuxtLink
                  :to="toDetails"
                  class="font-medium transition-colors hover:underline"
                  :style="{ color: 'var(--action)' }"
                >
                  {{ $t('recordings.nameThisMeeting') }}
                </NuxtLink>
              </p>

              <!-- Everybody who was in the channel but has no track: they
                   did not consent before it began. Named rather than
                   omitted, because "who else was there" is a fact about
                   the meeting and their absence from the audio is the
                   point. The list deliberately does not carry this; this
                   page is where there is room to say why. -->
              <p
                v-if="others.length > 0"
                class="mt-4 text-sm"
                :style="{ color: 'var(--text-muted)' }"
              >
                {{
                  $t('recordings.alsoInChannelUnrecorded', {
                    people: others.map((person) => person.display_name).join(', '),
                  })
                }}
              </p>
            </section>
          </div>
        </template>

        <!-- One speaker at a time, with what each file is. -->
        <template #tracks>
          <section
            class="rounded-2xl border p-5"
            :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
          >
            <h2 class="text-base font-semibold">{{ $t('recordings.eachTrackHeading') }}</h2>
            <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
              {{ $t('recordings.eachTrackNote') }}
            </p>

            <!-- Retention took the recordings and left what was written
                 from them. The list stays, without its players and its
                 pictures, because the measurements are part of "what was
                 written from it" — dropping them here would make the
                 notice above the tab bar untrue on the one page it is
                 shown on. -->
            <template v-if="audioGone">
              <p
                class="mt-4 rounded-lg border border-dashed p-4 text-sm"
                :style="{ borderColor: 'var(--border)', color: 'var(--text-muted)' }"
              >
                {{ $t('recordings.audioGoneNoTracks') }}
                <NuxtLink
                  :to="toTranscript"
                  class="font-medium transition-colors hover:underline"
                  :style="{ color: 'var(--action)' }"
                >
                  {{ $t('recordings.audioGoneReadInstead') }}
                </NuxtLink>
              </p>
              <RecordingTrackList class="mt-4" :session="session" :playable="false" />
            </template>
            <p
              v-else-if="session.tracks.length === 0"
              class="mt-4 rounded-lg border border-dashed p-4 text-sm"
              :style="{ borderColor: 'var(--border)', color: 'var(--text-muted)' }"
            >
              {{ $t('recordings.noAudio') }}
            </p>
            <RecordingTrackList v-else class="mt-4" :session="session" />
          </section>
        </template>

        <!-- The words. -->
        <template #transcript>
          <RecordingTranscript v-if="transcript" :transcript="transcript" />
          <!-- The one failure that is a tab's own and not the page's. The
               retry re-reads the recording, because the two arrived
               together and a transcript alone has nothing to be right
               about. -->
          <div
            v-else
            class="rounded-2xl border p-6"
            :style="{ borderColor: 'var(--danger)' }"
          >
            <p class="text-sm font-medium">{{ $t('recordings.transcriptFailedHeading') }}</p>
            <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
              {{ $t('recordings.transcriptFailedDetail') }}
            </p>
            <button
              type="button"
              class="mt-3 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)] disabled:opacity-60"
              :style="{ color: 'var(--action)' }"
              :disabled="status === 'pending'"
              @click="refresh()"
            >
              {{ status === 'pending' ? $t('recordings.retrying') : $t('error.retry') }}
            </button>
          </div>
        </template>

        <!-- The writing desk: what this meeting is called, what the reader
             files it under, and — for an administrator of the guild — the
             one control that changes what the recording *is*. The panel
             renders nothing at all for everybody else, which is why it is
             here rather than behind a tab that would be empty for almost
             every reader. -->
        <template #details>
          <div class="flex flex-col gap-4">
            <RecordingName
              :session-id="session.id"
              :name="{ title: session.title, description: session.description }"
              @saved="onRenamed"
            />
            <RecordingTags :session-id="session.id" :tags="session.tags" />
            <RequeuePanel :session-id="session.id" />
          </div>
        </template>
      </UiTabs>
    </template>
  </div>
</template>
