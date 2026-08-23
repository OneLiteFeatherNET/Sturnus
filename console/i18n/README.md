# How the console speaks two languages

The console is unusually prose-heavy: it explains itself in sentences rather
than labelling itself with nouns. That is a deliberate property of the
product, and it is also the reason internationalising it needs a convention
written down rather than a habit. A few hundred sentences translated by
whoever happened to be editing that file will not stay consistent.

## English is the source of truth

Every string is written in British English first, in `locales/en.json`.
German is a translation of that file and never the other way round. When a
sentence changes meaning, it changes in English first and the German follows
in the same commit.

`en.json` is therefore also the list of what the console can say. If a
sentence is not in it, no reader can ever see it in their own language.

## A key is added to both files in the same commit

Never one file and then the other. `test/i18n.spec.ts` fails the build if the
two files disagree about which keys exist, which is the point: a key that
lands in `en.json` alone is a German page with an English hole in it, and
nobody notices until a German-speaking reader finds it.

A missing German string falls back to English rather than rendering the key —
see `i18n.config.ts`. That fallback is a safety net for a bad deploy, not a
workflow. A reader meeting an English sentence in a German page has been
inconvenienced; a reader meeting `admin.queue.emptyNote` has been shown a bug
they cannot act on.

## Keys are namespaced by the area they serve

The first segment names the part of the console the string belongs to, and it
matches the file that renders it:

| Namespace          | Serves                                              |
| ------------------ | --------------------------------------------------- |
| `common.*`         | Strings with no single home — the product name, a duration |
| `nav.*`            | `utils/navigation.ts`, `AppSidebar`, the header burger |
| `auth.*`           | `pages/sign-in.vue`, signing out                     |
| `error.*`          | `error.vue`                                          |
| `dashboard.*`      | `pages/index.vue`, `utils/format.ts`                 |
| `recordings.*`     | `pages/recordings/*` and the components under them   |
| `calendar.*`       | `pages/calendar.vue`, the heatmap and the timeline   |
| `settings.*`       | `pages/settings.vue` — a person's own settings        |
| `admin.settings.*` | `pages/admin/bot-settings.vue`                        |
| `admin.consents.*` | `pages/admin/consents.vue`                            |
| `admin.queue.*`    | `pages/admin/queue.vue`                               |
| `admin.reporting.*`| `pages/admin/reporting.vue`                           |
| `ui.*`             | `components/ui/*` — the shared controls, and the gallery |

`admin.settings.*`, `admin.consents.*` and `admin.queue.*` do not exist yet:
Bot Settings, User Settings and the Queue are still English, and are being
rewritten in three other pull requests. They are listed so that whichever of
those lands first does not have to invent a name, and so that two of them
landing in parallel do not invent two.

Until then `utils/queue.ts` and `utils/consents.ts` still build English
sentences by hand, and three helpers exist only to serve them:
`formatCount` and `formatMoment` in `utils/format.ts`, and `formatDuration`
in `utils/duration.ts`. Each says so in its own comment. They go when the
last of those three pages is translated.

`admin.settings.*` is a partial exception. The name pickers on the bot
settings page were written against this convention from the start, while the
sentences that page had before them are still hard-coded English awaiting
their own conversion.

`admin.consents.*` is another exception, and it is half-populated on purpose.
The page it serves is still English prose, because the whole administrative
area is and converting it belongs to the sweep that converts all four admin
pages together. What is already keyed there is the material the
effective-instant work added — the withdrawal's effective moment, and the
scope on a roster row — so that the sweep has less to do rather than more.
New strings on that page go through `$t` from now on; the existing ones move
when the sweep reaches them.

`ui.*` is the one namespace that does not name a page. It serves
`app/components/ui/*` — the six shared controls every page is about to be
built out of — and the gallery at `/dev/ui` that renders them. They are keyed
from the start and keyed completely: a control is used by several pages, so a
hard-coded English string in one of them would appear in a German page
without any German page having been edited, and nobody would know which file
to look in.

**A control's contents are not translated here.** The labels in a dropdown, a
tab's name, the word a bulk action is called — those come from the page using
the control, which knows whether they are sentences this console wrote or a
guild's own channel names. What lives under `ui.*` is the control's own
chrome: its placeholder, its "there is nothing to choose from", the sentence
saying how much of a selection is not on this page.

`settings.consent.*` sits under `settings.*` rather than in a namespace of
its own, because it is a section of that page and not a page. It is the one
place a person acts on their own consent, and every sentence in it is
translated — a new section in English on an already-translated page is
exactly the hole `test/i18n.spec.ts` exists to prevent.

The second segment names the string by what it says, in camelCase —
`notLinkedHeading`, `goToSignIn`, `unreachableDetail`. Not by where it sits
on the screen: `nav.reporting` survives the entry moving to a different
group, `nav.fourthItem` does not.

## A pure module returns a key, not a sentence

`app/utils/*.ts` holds roughly a hundred and thirty sentence-length strings as
pure functions. Those functions **return translation keys**, and the template
that renders them calls `$t`. `NavEntry.labelKey` is the first of them and the
pattern for the rest.

The alternative was to thread a translator into each function. That would
make every one of these modules need a Vue application — or a fixture standing
in for one — before it could be tested, which is exactly the property that put
them in their own modules in the first place. A key is data. Data is what a
pure function should return.

Where a util's return value is passed straight to `$t`, name the field
`…Key`, so that a reader of the interface can see that the value is not
displayable on its own.

## Most sentences carry values, so a key alone is not enough

A bare key covers a label. It does not cover the sentences this console is
mostly made of, which count things, quote an instant, name a channel, and
several of which put one decided sentence inside another — a heatmap cell
says a date, a number of meetings, a length and a word for how busy the day
was, and every one of those four is a decision of its own.

So a pure module returns a **`Message`**: a key, and named values that are
strings, quantities, instants, or further messages. The type and the
reasoning are in `app/utils/message.ts`; `app/composables/useSay.ts` is the
one thing that turns one into words, and templates call it as `say(…)`.

Nesting rather than concatenation is the point. A sentence assembled by
adding one fragment to another carries the word order of whoever assembled
it, and no translation can move the pieces; a sentence with named holes in
it can be rewritten from scratch in German and handed the same four values.
Where a note is several sentences, return **a list of messages** rather than
one joined string, for the same reason.

A sentence with inline markup uses `<i18n-t keypath=…>` and a named slot —
see `pages/sign-in.vue` and the `admin_role_id` sentence in
`pages/admin/reporting.vue`.

### Counting

**Never an `if` in a module.** A parameter named `count` chooses the plural
form, and each locale file says what that does to its own sentence. Where a
sentence has two counts in it, the one that governs the verb is the `count`
and the other is a value beside it; where both need to pluralise, they are
two messages nested into one sentence with a hole for each.

Both files must keep the same placeholders, and `test/i18n.spec.ts` counts
them — so if the English singular writes the number out as a word, the
German singular does too.

### Numbers, dates and times

**A number in `params` is a quantity**, and `say` writes it in the locale's
grouping: 48213 is `48,213` to an English reader and `48.213` to a German
one. Anything that is a number without being a quantity — a year, a status
code, an id, a page number — is passed as a **string**, because `2,026` is
not a year.

**A date is an `Instant`**: the moment, and the name of a datetime format in
`i18n.config.ts`. That file decides the shapes and the zone each of them is
pinned to, and says why. Modules no longer keep tables of English month or
weekday names; `Intl` supplies those, and the console formats with the
language *tag* (`en-GB`) rather than the locale code (`en`), because the two
disagree about the order of a date.

The hydration argument that used to forbid all of this — `Intl` formats for
whatever locale the runtime resolves, so a server render and a browser could
disagree — was about an *ambient* locale. The console has a chosen one now,
carried in a cookie that travels with the request. Two places still assemble
a string by hand and should stay that way, and both say so in their own
comments: `formatTimestamp` in `utils/recordings.ts`, whose `YYYY-MM-DD
HH:MM` is unambiguous in every country and sorts the way it reads, and
`formatSeconds` beside it, which is a clock and not prose.

## The German

- **Orthographically correct German, always.** Umlauts and ß are written as
  umlauts and ß: `für`, `löschen`, `Maßnahme` — never `fuer`, `loeschen`,
  `Massnahme`. There is no transport in this stack that cannot carry them.
- **Impersonal, the way German software speaks.** Interface elements are
  infinitives or nouns: `Abmelden`, `Einstellungen`, `Aufnahmen`,
  `Erneut versuchen`. Not `Melden Sie sich ab`.
- **No `Sie`, and no `du` unless the English is itself informal.** Where a
  full sentence is unavoidable, prefer a subject-less phrasing —
  `Dazu /link in Discord ausführen` rather than `Führen Sie /link aus` or
  `Führ /link aus`. The console addresses the reader as little as possible in
  either language, and the German should not be more familiar or more formal
  than the English it translates.
- **Translate the meaning, not the words.** `Dashboard` is `Übersicht`
  because that is what a German-speaking reader would call the page, not
  because `Dashboard` is untranslatable.
- **Product names stay.** `Sturnus`, `Discord`, `Outline` are not translated,
  and neither is anything else that is somebody's trademark.

## What stops a fake translation

`test/i18n.spec.ts` refuses a German value that is byte-identical to its
English one. That is the check that catches a `de.json` produced by copying
`en.json` and translating the first screenful.

Genuine collisions exist — `Status` is the same word in both languages — so
the test keeps a small allowlist of keys that are permitted to match. The
allowlist lives in the test file rather than in a data file, so that a
reviewer watching it grow is a reviewer who has been asked a question.
