# Brand assets

The icon, and the reasoning behind it, so the next person changing it knows
what the constraints were.

## The icon

A starling in profile: iridescent plumage, the light speckles the bird is
named for, the long straight yellow beak, and two arcs for what it hears and
gives back.

**The gradient is the point.** OneLiteFeather's own mark is a feather in a
full spectrum, and a starling's plumage is iridescent — black that throws
green, violet and blue depending on the light. The house palette and the bird
are the same idea, so one gradient does both jobs: it reads as the
organisation's colours and as the species at the same time.

Colours are taken from the organisation mark
(`assets/onelitefeathernet.png` in the `assets` repository), not invented:

| | |
|---|---|
| `#1BC755` | green |
| `#06C5E8` | cyan — also the sound arcs |
| `#114CC8` | blue |
| `#B119B9` | magenta |
| `#D52A23` | red |
| `#F7CC2E` | yellow — also the beak |
| `#131260` → `#0B0B1F` | field |

## Designed for the smallest case

Discord renders avatars as a 32–40 px circle far more often than at full
size, so that is what the drawing is tuned for:

- **One closed silhouette**, no internal linework. Interior detail turns to
  mud below 48 px.
- **Two sound arcs, not three.** Three merge into a smudge at small sizes.
- **Nothing important in the corners**, which the circular mask removes.
- **Light speckles**, so they hold against every part of the spectrum the
  body passes through.

Check any change at 40 px before shipping it. An earlier draft looked fine
large and was unreadable small; another rendered as a goose.

## Files

| File | Use |
|---|---|
| `sturnus-icon.svg` | source — edit this one |
| `sturnus-icon-1024.png` | Discord application icon, store assets |
| `sturnus-icon-512.png` | bot avatar |
| `sturnus-icon-256.png` | smaller uses |

Regenerate the PNGs from the source after any edit:

```bash
for n in 1024 512 256; do
  rsvg-convert -w $n -h $n docs/brand/sturnus-icon.svg \
    -o docs/brand/sturnus-icon-$n.png
done
```

## Text

The Discord application description, the short form, and the wording
participants see are kept in Outline rather than here, since they are edited
by people who do not clone the repository:
<https://outline.onelitefeather.dev/doc/sturnus-projekt-discord-auftritt-und-icon-Wgdvn1VU0B>
