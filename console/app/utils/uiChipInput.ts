/**
 * One field holding tags and words at the same time, and the line between
 * them.
 *
 * A real recordings search is `#standup #migration the bit where the
 * database fell over`: two tags and a sentence about them. The console has
 * two controls for that today — a tag list and a search box — sitting side
 * by side, and everybody types the whole thing into the search box.
 *
 * The thing that makes one field workable is that **the line between a
 * chip and free text is visible in the value, not only on screen**. So the
 * model this control emits is a pair: the chips, and the text that is
 * still text. A caller can send the chips as a filter and the text as a
 * query without parsing anything back out, and nobody ends up with a tag
 * called "the".
 *
 * Everything below is that pair and the operations on it. The component
 * holds a caret and a list of buttons; it holds no rules.
 */
import type { Message } from './message'

export interface ChipValue {
  chips: string[]
  /** What has been typed and not yet committed. Never a chip, and never
   *  silently promoted into one. */
  text: string
}

export const EMPTY_CHIPS: ChipValue = { chips: [], text: '' }

/**
 * How many suggestions are offered at once.
 *
 * Six, because the list hangs under a field somebody is typing into and a
 * list long enough to scroll covers whatever they were looking at. It is a
 * shortcut, not a directory.
 */
export const MAX_SUGGESTIONS = 6

/**
 * A string as a chip, or `null` if it is not one.
 *
 * Inner whitespace is collapsed as well as trimmed, so `sprint   review`
 * and `sprint review` are one tag rather than two that look identical in
 * every place either of them is rendered.
 */
export function normaliseChip(raw: string): string | null {
  const chip = raw.trim().replace(/\s+/g, ' ')
  return chip === '' ? null : chip
}

const same = (a: string, b: string) => a.toLowerCase() === b.toLowerCase()

/**
 * Everything before the last comma, as chips.
 *
 * A comma is how somebody says "that one is finished", whether they typed
 * it or pasted a line containing three of them. The tail is deliberately
 * left as text: it is the part still being typed.
 */
function splitOnCommas(raw: string): { chips: string[], tail: string } {
  const pieces = raw.split(',')
  return {
    chips: pieces
      .slice(0, -1)
      .map(normaliseChip)
      .filter((chip): chip is string => chip !== null),
    tail: pieces.at(-1) ?? '',
  }
}

/**
 * Chips added, and the field cleared.
 *
 * Duplicates are refused case-insensitively, and the **first** spelling is
 * the one kept: it is the one already on screen, and swapping it under
 * somebody who typed the same tag again in capitals would look like a
 * rendering fault.
 *
 * The field is cleared even when everything in it was a duplicate.
 * Leaving the word sitting there makes it look unaccepted, and pressing
 * Enter a second time then appears to do nothing at all.
 */
export function addChips(value: ChipValue, raw: string): ChipValue {
  const wanted = raw
    .split(',')
    .map(normaliseChip)
    .filter((chip): chip is string => chip !== null)
  if (wanted.length === 0) return value.text === '' ? value : { ...value, text: '' }

  const chips = [...value.chips]
  for (const chip of wanted) {
    if (!chips.some((held) => same(held, chip))) chips.push(chip)
  }
  return { chips, text: '' }
}

/** One chip gone, by name and without regard to case. The free text is
 *  untouched: removing a tag is not abandoning a search. */
export function removeChip(value: ChipValue, chip: string): ChipValue {
  const chips = value.chips.filter((held) => !same(held, chip))
  return chips.length === value.chips.length ? value : { ...value, chips }
}

/**
 * What the field now holds, after a keystroke or a paste.
 *
 * Commas are acted on here rather than in a key handler, because a paste
 * is not a keystroke and `standup, migration, inci` arriving all at once
 * has to become two chips and a half-typed third exactly as it would if it
 * had been typed.
 */
export function setText(value: ChipValue, raw: string): ChipValue {
  if (!raw.includes(',')) return { ...value, text: raw }
  const { chips, tail } = splitOnCommas(raw)
  return { ...addChips(value, chips.join(',')), text: tail.replace(/^\s+/, '') }
}

export interface ChipKeyOutcome {
  value: ChipValue
  /** Whether the component should call `preventDefault`. False for every
   *  key this control has no business taking — an Enter in an empty field
   *  still belongs to the form around it. */
  handled: boolean
}

/**
 * What a key does.
 *
 * `caretAtStart` is the one thing this needs from the DOM, and it is the
 * whole of the Backspace rule: Backspace takes a chip only when there is
 * no text left to delete *and* the caret is at the front of the field.
 * Without it, a reader editing the middle of a word deletes the tag behind
 * them and does not see it go.
 */
export function reduceChipKey(
  value: ChipValue,
  key: string,
  caretAtStart: boolean,
): ChipKeyOutcome {
  switch (key) {
    case 'Enter':
      if (normaliseChip(value.text) === null) return { value, handled: false }
      return { value: addChips(value, value.text), handled: true }

    case 'Backspace': {
      if (value.text !== '' || !caretAtStart) return { value, handled: false }
      const last = value.chips.at(-1)
      if (last === undefined) return { value, handled: false }
      return { value: { ...value, chips: value.chips.slice(0, -1) }, handled: true }
    }

    case 'Escape':
      // Only when there is something to abandon. Swallowing an Escape that
      // cleared nothing would trap somebody inside whatever panel the
      // field is sitting in.
      if (value.text === '') return { value, handled: false }
      return { value: { ...value, text: '' }, handled: true }

    default:
      return { value, handled: false }
  }
}

/**
 * The tags worth offering, given what is already chosen and what is being
 * typed.
 *
 * Substring rather than prefix: somebody who half-remembers a tag is more
 * often right about the middle of it than about its first two letters.
 */
export function suggestionsFor(
  all: readonly string[],
  value: ChipValue,
  limit: number = MAX_SUGGESTIONS,
): string[] {
  const needle = value.text.trim().toLowerCase()
  return all
    .filter((tag) => !value.chips.some((chip) => same(chip, tag)))
    .filter((tag) => needle === '' || tag.toLowerCase().includes(needle))
    .slice(0, limit)
}

/**
 * What the field holds, as a sentence.
 *
 * It exists because the distinction this control is built around is one a
 * screen reader cannot see: a row of chips and a text cursor are, to a
 * reader who is not looking at them, one field with some words in it. Four
 * shapes rather than one with holes in it, because "0 tags and no text" is
 * not a sentence anybody wants read out.
 */
export function describeChips(value: ChipValue): Message {
  const text = value.text.trim()
  const count = value.chips.length
  if (count === 0 && text === '') return { key: 'ui.chipInput.empty' }
  if (text === '') return { key: 'ui.chipInput.chipsOnly', params: { count } }
  if (count === 0) return { key: 'ui.chipInput.textOnly', params: { text } }
  return { key: 'ui.chipInput.chipsAndText', params: { count, text } }
}
