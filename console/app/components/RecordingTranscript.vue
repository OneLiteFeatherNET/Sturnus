<script setup lang="ts">
/**
 * The meeting in words.
 *
 * **The same words the protocol was written from.** The API assembles this
 * with the function the worker builds the published document with, so the
 * console cannot show a different reading of a meeting than the document
 * does. That is worth a sentence on screen: somebody comparing the two is
 * entitled to know they are looking at one transcript through two doors
 * rather than at two transcripts.
 *
 * **A block carries the moment it was spoken, and that moment is shown.**
 * It is measured from the start of the session, in the same `m:ss` the
 * transport on the meeting tab runs on, so a number taken off a line here
 * can be found in the audio. Without it the transcript is a wall of prose
 * with no way back into the recording it came from.
 *
 * **An empty transcript is never just empty.** Three entirely different
 * situations produce no blocks, and `~/utils/transcript` is where they are
 * told apart; this file renders whichever sentence that module chose. A
 * transcript that *has* words and is still missing some says so too — a
 * partial one reads as finished, and somebody concluding from it that a
 * colleague said nothing has been misled by an omission.
 *
 * The words are not fetched here. The page fetches them, because the same
 * response carries whether this session still has any audio and two other
 * tabs need that answer before anybody clicks this one.
 */
import { formatSeconds } from '~/utils/recordings'
import { NOT_MEASURED } from '~/utils/message'
import {
  transcriptAttribution,
  transcriptEmpty,
  transcriptExternalNames,
  transcriptOffset,
  transcriptPartial,
  type SessionTranscript,
} from '~/utils/transcript'

const props = defineProps<{ transcript: SessionTranscript }>()

const say = useSay()

const empty = computed(() => transcriptEmpty(props.transcript))
const partial = computed(() => transcriptPartial(props.transcript))
const external = computed(() => transcriptExternalNames(props.transcript))

/** Whether anybody in this meeting is written under a second name in the
 *  protocol. Nobody is, most of the time, and a roster that says four
 *  names nobody asked about is four lines between a reader and the words. */
const relabelled = computed(() =>
  props.transcript.participants.filter(
    (speaker) => external.value[speaker.discord_user_id] !== undefined,
  ),
)

function at(index: number): string {
  const block = props.transcript.blocks[index]
  if (!block) return NOT_MEASURED
  const offset = transcriptOffset(props.transcript, block)
  return offset === null ? NOT_MEASURED : formatSeconds(offset)
}
</script>

<template>
  <section
    class="rounded-2xl border p-5"
    :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
  >
    <h2 class="text-base font-semibold">{{ $t('recordings.transcriptHeading') }}</h2>
    <p class="mt-1 max-w-2xl text-sm" :style="{ color: 'var(--text-muted)' }">
      {{ $t('recordings.transcriptNote') }}
    </p>

    <!-- Who is who, and only when that is a question. A speaker linked to
         an account is printed under the other name in the protocol, and a
         reader holding both documents otherwise finds two names for one
         colleague and no explanation. -->
    <p
      v-if="relabelled.length > 0"
      class="mt-4 rounded-lg p-3 text-xs"
      :style="{ background: 'var(--surface-raised)', color: 'var(--text-muted)' }"
    >
      <span class="font-medium">{{ $t('recordings.transcriptSpeakersHeading') }}</span>
      <span v-for="(speaker, index) in relabelled" :key="speaker.discord_user_id">
        {{ index === 0 ? ' ' : ', ' }}
        {{ say(transcriptAttribution(speaker, external[speaker.discord_user_id])) }}
      </span>
    </p>

    <!-- Three sentences for three situations, chosen in the module. A tab
         that met all of them with "nothing here" is a tab people report as
         broken, which is what the two extra fields on the endpoint exist
         to prevent. -->
    <div
      v-if="empty"
      class="mt-4 rounded-lg border border-dashed p-4"
      :style="{ borderColor: 'var(--border)' }"
    >
      <p class="text-sm font-medium">{{ say(empty.heading) }}</p>
      <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">{{ say(empty.detail) }}</p>
    </div>

    <template v-else>
      <p
        v-if="partial"
        class="mt-4 rounded-lg p-3 text-sm"
        :style="{ background: 'var(--surface-raised)', color: 'var(--text-muted)' }"
        role="status"
      >
        {{ say(partial) }}
      </p>

      <!-- An ordered list, and said to be one: a transcript is its order,
           and a run of bare paragraphs gives assistive technology neither
           a count nor a way to step between turns. -->
      <ol class="mt-4 flex flex-col gap-4">
        <li
          v-for="(block, index) in transcript.blocks"
          :key="`${block.discord_user_id}-${block.started_at}-${index}`"
          class="flex flex-col gap-1 sm:flex-row sm:gap-4"
        >
          <!-- The clock in its own column, tabular, so a reader scanning
               for a moment reads down one edge rather than hunting a
               number inside each line. -->
          <p class="shrink-0 text-xs tabular-nums sm:w-16 sm:pt-1" :style="{ color: 'var(--text-muted)' }">
            <span class="sr-only">{{ $t('recordings.transcriptAtLabel') }}</span>
            {{ at(index) }}
          </p>
          <div class="min-w-0 flex-1">
            <p class="text-sm font-medium">{{ block.display_name }}</p>
            <!-- `whitespace-pre-line`: the merge keeps the line breaks a
                 speaker's turn was assembled with, and a paragraph run
                 together is a different paragraph. -->
            <p class="mt-0.5 whitespace-pre-line text-sm">{{ block.text }}</p>
          </div>
        </li>
      </ol>
    </template>
  </section>
</template>
