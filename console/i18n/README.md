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
| `common.*`         | Strings with no single home — the product name       |
| `nav.*`            | `utils/navigation.ts`, `AppSidebar`, the header burger |
| `auth.*`           | `pages/sign-in.vue`, signing out                     |
| `error.*`          | `error.vue`                                          |
| `dashboard.*`      | `pages/index.vue`                                    |
| `recordings.*`     | `pages/recordings/*`                                 |
| `calendar.*`       | `pages/calendar.vue`                                 |
| `admin.settings.*` | `pages/admin/bot-settings.vue`                        |
| `admin.consents.*` | `pages/admin/user-settings.vue`                       |
| `admin.queue.*`    | `pages/admin/queue.vue`                               |
| `admin.reporting.*`| `pages/admin/reporting.vue`                           |

The namespaces below `error.*` do not exist yet. They are listed so that the
first pull request to convert one of those pages does not have to invent a
name, and so that two of them converted in parallel do not invent two.

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
