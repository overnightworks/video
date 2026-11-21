# Run the card proof

Reader: the operator on a host with the card. The proof drives the service through
the wire as an app does and writes a record of figures; a dry run drives the `fake`
provider and proves only that the drive works.

Needs: `setpriv` and `probe-run` on the host, `uv`, and a mono 16-bit WAV of at least
a second (`speech.wav`).

## Prepare FlashHead lite

From the repository root:

```sh
git submodule update --init
uv sync --locked --all-groups
uv sync --locked --all-groups --project providers/flashhead-lite
uv run --locked --project providers/flashhead-lite video-flashhead-lite-fetch-weights
```

The fetch fills `providers/flashhead-lite/weights` (about 8.2 GB) and, beside the
weights, `weights/notices`, the licence notices to keep when you pass the files on. It
ends non-zero naming the file it could not fetch, and a second run skips the
files it holds.

## Run

```sh
uv run --locked python scripts/prove_card.py --dry-run --audio speech.wav --record record.json
VIDEO_PROVIDER_ROOT=$PWD/providers VIDEO_CARD_MEMORY_MB=7168 \
  uv run --locked python scripts/prove_card.py --audio speech.wav --record record.json \
  --look-image face.png --lag-ms 500 1000
```

- `--look-image` serves a head-and-shoulders, centred, square PNG, JPEG or WebP as the
  look `alpha`; without it the cells use a stand-in square.
- `--cells` picks cells (`sentences`, `clear`, `looks`, `viewer`, `viewers`, `lag`).
- `--memory-floor-mb` is the free card memory the run keeps beside the cap.
- The command ends with one line naming the argument or setting it refuses, before any
  service starts.

## See it worked

The command exits 0 and prints the paths below. It exits non-zero when a cell, a group,
the memory guard or the kill fails; the record is written all the same, with each figure
that was not measured marked `not_measured` and its reason.

- `record.json`: the figures.
- `record.viewer.mp4` and `record.viewer-sheet-<n>.png`: one viewer's audio and
  video, and contact sheets of it; judge the picture by eye.
- `record.<group>.log`: each group's service log.

A dry run's figures are the fake's (a 256x256 picture on any host): only a run on the
card shows timing, memory and the picture.
