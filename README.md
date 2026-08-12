# AYNI production panel

A local review and production panel for the remainder of the AYNI shoot: ~100
shots across ~20 scenes, generated with Higgsfield by a Claude Code agent and
judged by a director who cannot afford forty blocking handoffs.

It fixes two things and treats everything else as secondary:

1. **Review stops being serial.** Shots are judged on a wall — a grid of stills,
   or of muted looping clips — instead of one at a time in the generation UI.
2. **Defect feedback stops being prose.** A rejection is one or more codes, and
   the code carries the routing.

**The panel is not a generation tool. No click in the browser can spend a
credit.** Decisions are written to disk as intent; `runner.py` is the only
component that spends anything.

```
shots/*.json      single source of truth on disk — one file per shot per stage
runner.py         the ONLY component that spends credits
server.py         reads and writes those files; serves the panel and the media
panel.html        a view and an intent recorder — no build step, no framework
```

## Run it

```bash
python3 seed_demo.py --root . --reset   # demo project: 109 shots, placeholder media
python3 server.py --root .              # http://127.0.0.1:8787
python3 runner.py --watch               # in a second terminal, drains the queue
```

On a real project, skip the demo seed and migrate the shot plan instead:

```bash
python3 migrate.py --from shotplan.csv --root .
```

Tests, including one per mechanically checkable acceptance criterion:

```bash
python3 tests.py
```

## Data model

Three files per shot, each with exactly one writer. `shot_id` (`S07_04`) is the
join key.

```
shots/plan/S07_04.json      written once by the shot-plan pass, read-only after
shots/images/S07_04.json    written by the image stage only
shots/videos/S07_04.json    written by the video stage only
```

One writer per file means an image-stage session and a video-stage session can
run at the same time on the same shot range without either being able to
corrupt the other's state. It also means a defect needs no `layer` field: which
file it lives in *is* its layer.

State machines:

```
image:   planned → prompted → queued → done → approved
                                            ↘ rejected → prompted
                                            ↘ escape_hatch

video:   (record does not exist)
         → prompted → queued → done → approved
                                    ↘ rejected → prompted
                                    ↘ escape_hatch
                                    ↘ fix_in_post
```

**A video record is created only when its still is approved.** The existence of
`shots/videos/S07_04.json` *means* the still is approved, so stage progress is a
directory listing and coverage gaps are set differences between three
directories. The approved still's media id is written into the video record as
`start_frame`; `references` then holds only what the start frame does not
already carry.

Retired records are not deleted outright — they move to `shots/attic/`, so a
routing decision can be audited, and undone, after the fact.

## Rules the panel enforces

| Rule | Where it lives |
| --- | --- |
| A clip rejection can route back to the still, and defaults from the defect code | `store.Store._reject_upstream`, overridable with <kbd>T</kbd> |
| Attempt cap of three **per stage**, structurally | `store.Store.enqueue` returns 409; the panel removes the re-roll control and offers escape hatches instead |
| Local edit is a distinct, cheaper action from a re-roll | `kind="local_edit"`, priced separately, does not consume an attempt |
| Every decision writes through immediately | atomic `os.replace` on every mutation; no batch save anywhere |
| No panel action spends a credit | no route in `server.py` can generate; credits are booked only in `runner.py` |

## Screens

**Coverage** — scene rows against shot columns, colour-coded, with the gap list
above it: planned but never prompted, still approved with no clip record, a clip
whose `start_frame` is no longer the current approved still, orphan records,
untiered shots, shots sitting at the cap. The point of the screen is gaps, not
progress.

**Review** — the wall. Three to four tiles per row, stills or muted autoplaying
loops, keyboard-first.

| Key | Action |
| --- | --- |
| <kbd>A</kbd> | Approve — approving a still opens its clip record |
| <kbd>1</kbd>–<kbd>9</kbd> | Apply a defect code. More than one press adds detail to the same rejection. |
| <kbd>→</kbd> <kbd>←</kbd> <kbd>↑</kbd> <kbd>↓</kbd> | Move the cursor |
| <kbd>F</kbd> | Flag for review with Suhan |
| <kbd>L</kbd> / <kbd>⇧L</kbd> | Mark locally editable / queue the local edit |
| <kbd>R</kbd> | Queue a re-roll |
| <kbd>T</kbd> | Override the routing of the last clip rejection, either direction |
| <kbd>⇧R</kbd> <kbd>⇧C</kbd> <kbd>⇧P</kbd> | Escape hatches: reframe, cutaway, fix in post |
| <kbd>I</kbd> | Shot inspector — prompt, references, attempt history, every defect ever logged |
| <kbd>g</kbd> then <kbd>c/r/q/l</kbd> | Switch tab · <kbd>?</kbd> for help |

**Queue** — what the runner is doing, what is pending, what failed and why.

**Ledger** — spent, projected on the queue, remaining, and the
defect-frequency histogram. A code that keeps recurring is a doctrine problem to
fix in the Shorthand document, not a shot to re-roll.

## Defect codes

Nine per stage, bound to <kbd>1</kbd>–<kbd>9</kbd>. Video codes carry a default
route; three of them point upstream, because the defect is in the still.

| | Image | | Video | Routes to |
| --- | --- | --- | --- | --- |
| 1 | I1 identity | 1 | V1 morph | video |
| 2 | I2 anatomy · local | 2 | V2 identity drift | **image** |
| 3 | I3 wardrobe · local | 3 | V3 anatomy | **image** |
| 4 | I4 framing | 4 | V4 motion | video |
| 5 | I5 light | 5 | V5 camera | video |
| 6 | I6 environment | 6 | V6 pacing · local | video |
| 7 | I7 artifact · local | 7 | V7 flicker · local | video |
| 8 | I8 text · local | 8 | V8 element · local | video |
| 9 | I9 doctrine | 9 | V9 start frame | **image** |

Edit `codes.py` to change the vocabulary; the panel, the validator and the
keyboard map all read from it.

## Server routes

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/api/shots` | plan records joined with current image and video state |
| `GET` | `/api/images` | image records only — the still review queue |
| `GET` | `/api/videos` | video records only — the clip review queue |
| `GET` | `/api/status` | runner state, queue depth, live progress |
| `GET` | `/api/ledger` | credits spent, projected, remaining, defect histogram |
| `GET` | `/api/coverage` | the scene grid and the gap set differences |
| `GET` | `/api/codes` | defect registry, tiers, pricing, attempt cap |
| `GET` | `/api/shot?id=` | everything known about one shot — the inspector |
| `GET` | `/api/validate` | manifest validation report |
| `POST` | `/api/review` | write a decision — takes `shot_id` **and** `stage` |
| `POST` | `/api/queue` | enqueue regeneration or local edit for one stage |
| `POST` | `/api/bulk_approve` | C-tier only |
| `POST` | `/api/export/contact_sheet` | static HTML export |
| `GET` | `/thumb/…` | review-wall thumbnail, falling back to the original |
| `GET` | `/media/…` | generated stills and clips, range requests supported |

The review filters are query parameters: `state`, `tier`, `scene`, `code`,
`flagged`.

## Media weight, and why the wall stays quick

Forty tiles on screen at 2k each is how a review wall turns back into
one-at-a-time inspection. Two things keep that from happening, and both matter
more with real media than with the demo's placeholders.

**Stills come from `/thumb`, not `/media`.** Thumbnails are written under
`media/.thumbs/`, built by the runner when it books a result, on demand when the
panel asks for one that does not exist, and in bulk by `thumbs.py`. The
inspector still serves full resolution — that is where you judge detail.

**Clips are never all decoded at once.** A tile's `<video>` carries no `src`
until an IntersectionObserver hands it one, and never more than twelve are
attached at a time, ranked by distance from the middle of the screen. Browsers
cap concurrent video decoders and the failure mode past that cap is tiles that
silently never start. Eviction is on a 400ms delay so that moving the cursor
across a row does not make clips decode themselves from scratch on the way back.

Measured on a 109-shot project with real media — 2k JPEG stills, h264-class
clips, nothing mocked:

| | full resolution | through the panel |
| --- | --- | --- |
| 117 media files on disk | 27.5 MB | 2.0 MB of thumbnails (13.9×) |
| first paint of the visible wall | — | 0.80s, 1.3 MB transferred, zero full-res requests |
| clip decoders attached, worst case | 29 | 12 |

Those synthetic stills averaged 303 KB. Real Cinema Studio 2k output is
heavier, which moves the ratio further in the thumbnails' favour, not less.

Thumbnails are optional and have no hard dependency. Backends are detected
independently — stills prefer Pillow, then ffmpeg, then macOS `sips`; poster
frames need ffmpeg and nothing else will do. With none installed, `/thumb`
serves the original and the panel behaves exactly as it did before thumbnails
existed. The one thing worth installing:

```bash
pip install pillow            # stills
# ffmpeg, if you want poster frames on clip tiles
python3 thumbs.py --root .    # warm the cache for media already on disk
```

## The runner, and where real generation attaches

`runner.py` has two drivers.

`--driver sim` (default) fabricates placeholder media locally and books the
credits, so the whole panel — ledger, queue, cap, coverage — can be exercised
without spending anything.

`--driver agent` writes a work order to `runner/orders/pending/` and stops. That
order is a complete brief: prompt, start frame, references, tier, attempt
number, prior defects, price. A Claude Code agent session picks it up, calls
Higgsfield, and reports back:

```bash
python3 runner.py complete --shot S07_04 --stage image \
    --media images/S07_04_a2.jpg --credits 8
python3 runner.py fail --shot S07_04 --stage image --error "job timed out"
```

A failure that produced nothing and cost nothing refunds the attempt — the cap
exists to bound credit burn, not to punish a timeout. Hero shots are processed
first, so if the sprint runs out of credits it runs out on inserts.

## Utilities

```bash
python3 contact_sheet.py --root . --stage image --tier A   # static, sendable
python3 validate.py --root .                               # schema, elements, tiers
python3 thumbs.py --root . [--force]                       # review-wall thumbnails
```

The contact sheet embeds stills as data URIs, so a stills sheet is a single file
you can attach to a message. Clip sheets copy the clips next to the HTML.

The validator checks schema, legal states, orphan records, missing media on
disk, untagged tiers, attempts over the cap, stale start frames, and prompts
referencing `{ELEMENT}` tokens that no plan record declares.

Bulk approve (C-tier only) and the shot inspector live in the panel.

## Out of scope

Authentication, multi-user concurrency, deployment, cloud storage, editorial,
sound, and any generation logic inside the panel. It runs on one machine for
three days and then stops.
