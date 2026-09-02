# Palmimo DevKit docs site

The public-facing site for Palmimo DevKit: a product landing page in front of
the documentation the repository already carries.

**The site does not hold a copy of the documentation — it renders `doc/`.**
Edit the page in `doc/`; never edit `src/content/docs/`, which is build output.
The one page written by hand here is the splash, `src/site-pages/index.mdx`.

Built with [Astro](https://astro.build) and [Starlight](https://starlight.astro.build).

## Commands

Run from this directory (`docs-site/`):

| Command | Action |
| :--- | :--- |
| `npm install` | Install dependencies |
| `npm test` | Test the link rewriting in `sync-doc.mjs`, the joint posing, and the console's Python runtime |
| `npm run sync` | Render `doc/` into `src/content/docs/` |
| `npm run dev` | Start the local dev server at `localhost:4321` |
| `npm run build` | Build the production site to `./dist/` |
| `npm run preview` | Preview the production build locally |

`test`, `dev` and `build` sync first — the console's runtime test reads the SDK
payload that `sync` writes, so it has to. `astro preview` and `astro check` do
not, so run `npm run sync` yourself before those.

## How the render works

`sync-doc.mjs` walks `doc/**/*.md` and writes each page into
`src/content/docs/`, giving it the frontmatter Starlight needs — a `title`
taken from the page's `# H1`, a sidebar `order`, and an `editUrl` pointing back
at the page in `doc/`. Images come across from `doc/images/` into
`public/images/`.

Two more fields ride along: `description`, the page's first prose paragraph
with its Markdown stripped (used as the meta description search engines and
link previews show), and `lastUpdated`, the source page's last commit date
from `git log`.

Links are rewritten as they pass through:

| Link in `doc/` | Becomes |
| :--- | :--- |
| Another `doc/` page | A site route, e.g. `/reference/api-reference/` |
| `doc/images/…` | `/images/…` |
| Anything outside `doc/` | A GitHub URL under `REPO_BLOB_BASE` |

`REPO_BLOB_BASE` points at this repository on GitHub. Set
`PALMIMO_DOC_BLOB_BASE` to build those links against a fork or a mirror
instead.

A link that resolves to nothing fails the build rather than shipping broken.
That is the point of doing this at build time: the site cannot drift from
`doc/`, because it is not stored separately from it.

## Hosting

The output is static files, so any static host serves it. What the host has to
get right is the checkout, because `doc/` sits above this directory:

| Setting | Value |
| :--- | :--- |
| Root directory | the repository root — **not** `docs-site` |
| Build command | `cd docs-site && npm ci && npm run build` |
| Output directory | `docs-site/dist` |
| Node | 22.12 or newer |

Leaving the root directory at the repository root is the whole trick: a host
that prunes the checkout to `docs-site` hides `doc/`, and the build fails
rather than shipping an empty site. Verified from a clean clone — the 10 pages
of `doc/` plus the splash, images included. On Vercel, setting a Root Directory of `docs-site` also needs
"Include files outside the root directory" enabled; the settings above avoid
the question entirely.

The checkout also has to carry full git history for `lastUpdated` to appear:
`sync-doc.mjs` reads each source page's last commit date with `git log`, but on
a shallow checkout (`fetch-depth: 1`, GitHub Actions' default) that read is
unreliable — the grafted tip commit has no parent, so `git log` diffs it
against the empty tree and every tracked file matches, which would stamp every
page with that one commit's date instead of its own. `sync-doc.mjs` detects a
shallow checkout with `git rev-parse --is-shallow-repository` and omits the
field there rather than shipping that wrong date; that is not a build failure,
the page just renders without a date. A host whose checkout defaults to
shallow needs its fetch depth turned up if the date matters.

`astro.config.mjs` sets `site: 'https://docs.palmimo.dev'`, so Starlight emits
canonical URLs and the `@astrojs/sitemap` integration writes `sitemap-index.xml`
at the site root.

## Structure

```
docs-site/
├── public/                 -> Static assets (icons, the logo, models/; images/, python/, pyodide/ are synced)
├── src/
│   ├── site-pages/         -> Hand-written pages (the splash, and nothing else)
│   ├── components/         -> Astro components the splash reaches for
│   ├── scripts/            -> Browser modules those components run
│   ├── python/             -> Python those modules run in the browser
│   ├── styles/             -> Starlight's custom CSS, loaded by astro.config.mjs
│   ├── content/docs/       -> Build output: doc/ rendered for Starlight
│   └── content.config.ts   -> Starlight content collection config
├── sync-doc.mjs            -> The renderer described above
├── astro.config.mjs        -> Site title, GitHub link, and sidebar nav
└── package.json
```

`public/favicon.ico`, `public/favicon-32.png` and `public/apple-touch-icon.png`
are the landing page's icons, provided assets shared with the rest of the
product. A drawing this site made for itself stood here until those existed,
and it reacted to the reader's colour scheme, which a raster icon cannot —
but one product should not introduce itself with two different marks, so the
shared one wins.

`public/palmimo-devkit-logo.png` and its `-white` counterpart are the wordmark,
in public/ rather than `src/assets/` because two places ask for it: the header
reads them through `astro.config.mjs`, and the splash hero names the same URLs
in its heading. One file each, so neither can drift from the other.

## The robot on the splash

`public/models/palmimo.glb` is the robot, a provided asset exported from the
robot's CAD definition. It is not a
still: each driven link is a node named after the joint that drives it, and the
joints carry the names the SDK gives its servos, so `leg_1_yaw` in a tick dict
out of `Palmimo.step()` and `leg_1_yaw` in the model are the same joint. Each
node's frame puts that joint's axis on local +Z, so turning one servo is one
rotation about Z.

It stands in the robot definition's own rest pose. The angles the SDK hands
out are relative to whatever its servos call neutral, so the model and the SDK
only have to agree on which way a joint turns, not on where it started.

`src/scripts/robot-stage.js` renders it, `robot-pose.js` turns the joints. That
second file takes **radians, never ticks**: converting a servo position into an
angle is the SDK's knowledge, and a copy of it here would be a second opinion
about how the robot moves. Whatever drives the stage brings its own angles.

Turning the joints alone leaves the model's root fixed, so a motion that moves
the *body* -- a pushup, a tilt, a dance roll -- would be invisible: the legs
would animate under a body pinned in space. `robot-ground.js` stands the posed
model on its feet instead, each frame, and `robot-stage.js` writes the result
onto the model root.

It reads the **posed model's own geometry** rather than computing the body pose
from the SDK's kinematics: the glb carries the real leg link lengths, while the
SDK's leg model is documented as not matching them. Two things about the asset
shape how the fit has to work:

- **Its rest pose is not level.** The six feet sit up to 11 mm apart in height,
  because it is a CAD rest pose rather than a stance settled onto a floor. So
  the fit measures each foot against *where that foot rested*, not against a
  level plane. A robot standing as it was exported is then corrected by exactly
  zero, which is the only answer that leaves it standing where the photographs
  show it.
- **Which feet carry weight has to be a matter of degree.** Choosing the three
  lowest and fitting a plane through them makes the result jump the moment the
  gait hands over from one tripod to the next, and jump again on nothing at all
  when four feet are within a rounding error of each other. Every foot is in
  the fit, weighted by how far it has risen above the most loaded one, so a
  swing leg leaves the fit smoothly instead of being switched out of it.

Where it walks to comes from the same feet. A foot on the ground does not move,
so its travel backwards through the body is the body's travel forwards through
the world, and the rigid motion that best carries this frame's loaded feet onto
the last frame's is that travel. Height and lean are measured against the rest
pose afresh every frame; heading and position have nothing to be measured
against, so they accumulate.

What moves on screen is the ground, not the robot. The camera travels with it,
keeping whatever angle and distance the reader dragged it to, and a grid slides
underneath — kept under the robot to the nearest whole cell, which is a move a
grid is unchanged by, so the floor has no end to walk off. Its far edge is
fogged to the colour the canvas is transparent over, so the lines become the
page rather than stopping on it.

Against 300 frames of real SDK output, `forward()` covers 166 mm in five
seconds and crosses two grid cells, the largest single-frame slide is 1.0 mm,
and `idle` moves the robot by exactly zero.

The robot also curves as it walks, by about 0.07° per millimetre travelled — a
turning circle around 1.5 m across. That belongs to the gait rather than to the
fit: a stride is made by the yaw servos alone, so each foot travels an arc, and
the sideways halves of those arcs add up into a turn instead of cancelling.
Halving and doubling `yaw_amplitude` moves the travel and the turn together.

## What the robot sees

`robot-view.js` puts a camera on the robot's own head, and the stage draws it
into the corner of the same canvas. It is a second pass over one context rather
than a second canvas, so there is one renderer, one scene, and one set of
shadows.

The camera is a child of `neck_yaw`. That is the *last* link in the neck chain
-- the model nests `neck_yaw` under `neck_pitch2` under `neck_pitch1` -- so
every neck joint the SDK turns has already carried the camera before it is
drawn. Anchoring anywhere higher would drop a gesture: on `neck_pitch2`, a
`head_shake()` moves the head and not the view. `neck_pitch2` itself is never
driven; the engine emits `neck_yaw` and `neck_pitch1` only.

Where the lens sits and which way it points are measured off the asset at rest,
not written down: forward is whichever direction in the head's own frame the
model's +x lands on, which is the direction `forward()` carries the body, and
the lens goes in the opening the shell actually cuts for one — the widest
of the three the head has, the window through the bracket below the face plate,
found by ray-mapping the face rather than by picking a spot on it. A re-export
that reframes the head moves the camera with it.

The one number that is a choice rather than a measurement is the mount angle:
the lens is set about 18° below level. Dead level its axis runs parallel to the
ground and never meets it, so the frame was horizon and whatever the fog had not
taken, and the neck cannot fix that, since its pitch is clamped to the servos'
safe band. Angled down, the view holds the ground the robot is about to walk
over. `DECLINATION` in `robot-view.js` carries the heights and distances.

A frame read back off the GPU is painted against the stage's own ground colour
first. On screen the canvas is transparent and the page supplies that colour,
but a render target has no page behind it: without this, everything above the
horizon came back as a black void no viewer ever saw.

The inset's box is the element the page draws the border around: the renderer
reads that element's rectangle and uses it as the viewport, so the frame on
screen and the picture inside it cannot come apart. Nothing is read back off
the GPU to show it -- only `read()` does that, on demand.

The ground the inset shows is a grid and nothing else. A grid is thinner
evidence of travel than a landmark going past would be -- the pattern repeats,
so a turn moves it less -- but it is the only ground the stage can promise:
nothing here computes contact, so an object standing on it would be an object
the robot walks straight through.

## The console under it

What drives the stage is the SDK itself. The prompt under the robot runs
CPython, compiled to WebAssembly, with the real `palmimo_sdk` in front of it:
`robot.forward()` typed there is the same call a Raspberry Pi makes, computing
the same tick dict, sixty times a second. Nothing about how the robot moves is
reimplemented here, so a motion the SDK gains is one this console can run.

| Piece | What it does |
| :--- | :--- |
| `src/python/stage_bridge.py` | Turns a tick dict into the stage's radians, undoing the SDK's servo encoding; and carries the peripherals back the other way |
| `src/scripts/robot-runtime.js` | Loads the interpreter, mounts the SDK, runs the prompt |
| `src/scripts/robot-console.js` | The transcript, the history, and the frame loop |

`sync-doc.mjs` stages both halves into `public/` at sync time: the SDK's
sources are read out of `packages/palmimo_sdk/` into one JSON payload, and the
interpreter is copied out of `node_modules/pyodide/`. Neither is committed —
the SDK's copy would go stale the day the package changed, which is the whole
thing this arrangement exists to prevent.

The interpreter is around 11 MB, so nothing is fetched until the reader runs
their first command.

Three of the four peripherals go through the same seam the hardware does.
`StageCamera`, `StageDisplay` and `StageMic` are handed to the facade exactly
as a `HeadCamera`, a `FaceDisplay` and a `Microphone` are on the robot, and
nothing in the SDK was told about any of them: `Palmimo` calls a handful of
methods on what it was given, and these have those methods.

What the SDK does not do is publish that shape — `camera=` is typed as the
concrete `HeadCamera` — so `stage_bridge.py` writes it down on this side as
`CameraSource`, `DisplaySink` and `MicSource`, and `isinstance(robot.display,
DisplaySink)` is true at the prompt. A contract a page writes about itself
proves nothing on its own, so `robot-runtime.test.mjs` also holds the SDK's own
`HeadCamera` against `CameraSource`: if the page's idea of the shape and the
robot's camera ever part company, that fails. The console's opening lines show
the substitution rather than hiding it, because it is the thing worth seeing.

`speaker=` is the exception and takes the concrete `Speaker`, so there is no
`StageSpeaker`: the `SpeechHandle` its `say` returns reaches back into the
speaker that made it, and a contract naming that handle could only be satisfied
by the speaker that builds it. A browser has no piper either way.

Each peripheral has exactly one place it shows itself. The camera has the
inset; the display has a lit disc on the model's own head, because the export
carries no face and the real robot wears one; the mic has a meter under the
inset, which fills for exactly as long as the capture asked for.

Two places the browser and the robot differ, both deliberate. **The page never
asks for your microphone.** A landing page that opens a permission prompt to
demonstrate an API has taken something it was not offered, so `record()` returns
a real, correctly formed WAV of the length asked for — header, duration and byte
count all genuine — containing silence. The meter under the inset is what says a
capture is running; what it holds is documented on `StageMic`, not on the page.

And what `read()` hands back. The real camera returns a numpy array; this page
has no numpy. Pyodide ships the
interpreter alone and fetches every package from a CDN, so an array would cost
this site the one property it has been built to keep — that everything it
serves comes from itself. So a frame is the BGR bytes with a `shape` on them:
`numpy.frombuffer(frame, "uint8").reshape(frame.shape)` for anyone who has
numpy to hand, and a readable `repr` for everyone else.

`robot-runtime.test.mjs` runs the real payload against the real interpreter
under Node, which is what catches the failure that matters: the SDK growing a
dependency that a browser has no way to import. The site would still build.
