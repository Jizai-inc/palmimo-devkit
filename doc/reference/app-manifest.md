# App Manifest — `palmimo.toml`

An app that ships on Palmimo (a `pyproject.toml`-based Python project like the
examples in this repository) declares itself with one static file at its
root, `palmimo.toml`. A host reads only this file to list an app in a
catalog, build its parameter form, and launch it — it never imports or runs
the app's Python to find that out. `schema` declares the manifest's own
version; a future incompatible change to this format raises it.

## Example

`examples/teleop/palmimo.toml`, in full:

```toml
schema = 1
name = "teleop"
description = "Drive Palmimo from a phone or laptop browser over WebSocket, with live MJPEG camera video, on the same LAN."
command = ["palmimo-teleop", "--port", "{port}", "{no_camera}"]
url = "http://{host}:{port}/"
devices = ["camera", "motor_display"]

[params.port]
type = "int"
default = 8000
min = 1024
max = 65535
description = "HTTP/WebSocket port the teleop web app is served on."

[params.no_camera]
type = "bool"
default = false
flag = "--no-camera"
description = "Disable the head camera video feed."
```

## File name

An app directory may ship more than one manifest: a file named `palmimo.toml`
(the default) or `palmimo.<variant>.toml`, where `<variant>` matches
`^[a-z][a-z0-9-]{0,39}$`. Any other name in the directory (`palmimo_backup.toml`,
`palmimo.TOML`, `palmimo..toml`) is not a manifest and is ignored. Each
manifest file is an independent app with its own `name`, unique across the
catalog; a host that launches an app by name with none given reads
`palmimo.toml`. `examples/agents/companion/palmimo.realtime.toml` is an
example: the same directory's `palmimo.toml` is the cascaded pipeline app,
and `palmimo.realtime.toml` is the OpenAI Realtime voice runtime, because the
two need different env vars and devices.

## Display translations

A manifest may have a companion JSON file named `<manifest filename>.i18n.json`:
`palmimo.toml.i18n.json` or `palmimo.realtime.toml.i18n.json`, for example.
It contains display text only. The manifest itself remains schema 1 and older
hosts ignore the companion, retaining the original descriptions.

```json
{
  "schema": 1,
  "translations": {
    "ja": {
      "description": "ブラウザからPalmimoを遠隔操作します。",
      "params.port": "遠隔操作画面に使うポート番号です。"
    }
  }
}
```

Message keys are `description`, `env.<NAME>`, `params.<name>`, `assets.<name>`
(translates the asset's `description`), or `assets.<name>.params.<sid>` and must
refer to fields declared by that manifest. Each value is a nonblank string
of at most 200 characters. A file is at most 64 KiB, is not a symlink, and
contains 1–32 locale objects. Locale tags use lowercase language codes with
optional hyphenated subtags, such as `ja`, `en`, and `ja-jp`.

The Portal selects each field using its UI language: exact locale first,
then the base language, then the manifest's original text. Switching the UI
language changes neither launch parameters nor the robot's spoken language.
Unknown keys and malformed optional translations are ignored by Portal;
the reference validator rejects them when building a catalog. The catalog
carries the same `translations` map, including through the persisted cache.
Translations are not part of the executable-settings snapshot in the app ledger.

## App identity

On the device, an app is identified by an ID of the form `<namespace>.<name>`.
A host derives the namespace itself from where the app was installed from —
an install from the official catalog gets `palmimo`, a GitHub install gets
the repository owner, a zip upload gets `zip`, and so on — nothing in the
manifest sets it.

`name` is only the default for the ID's name part, and the app's display
name in the catalog. It does not need to be unique across every possible
install source: if installing it would collide with an app already on the
device, a host appends `-2`, `-3`, and so on to the name part, and the
person installing it can rename it before install completes. Because the
namespace already carries the organization or vendor, avoid putting one in
`name` — no `palmimo-` or similar prefix.

## Fields

| Key | Type | Required | Rule |
|---|---|---|---|
| `schema` | int | yes | `1`, or `2` when the manifest declares `[assets]`. A host that predates schema 2 rejects a schema 2 manifest, so the app is listed as needing a newer host instead of running without its assets |
| `name` | string | yes | Must match `^[a-z][a-z0-9-]{0,39}$` — see [App identity](#app-identity) for what it is used for |
| `description` | string | yes | 1-200 characters. The catalog's one-line summary |
| `command` | array of strings | yes | The argv to execute. An array, not a shell string, so no shell ever parses it; an element may contain one or more `{param}` placeholders |
| `url` | string | no | A page to open once the app is running. May contain `{host}` and param placeholders. Must start with `http://` or `https://` (`javascript:` and similar are rejected) |
| `devices` | array of strings | no | A subset of `camera`, `audio`, `motor_display`. Omitted (or empty) opens no device at all |
| `[env.<NAME>]` | table | no | One table per environment variable the app reads — see [Environment variables](#environment-variables) |
| `[params.<name>]` | table | no | One table per launch-time parameter — see [Parameters](#parameters) |
| `[assets]` | table | no (schema 2 only) | Face-display assets the app plays — see [Display assets](#display-assets) |

Every top-level key not in this table is a validation error — an unrecognized
key is far more likely to be a typo silently doing nothing than a
deliberately unused extension.

## Environment variables

`[env.<NAME>]` declares one environment variable the app reads at startup.
`<NAME>` must match `^[A-Z][A-Z0-9_]{0,63}$`; names starting with `PALMIMO_`
are reserved for variables the host injects itself (e.g. `PALMIMO_APP_ID`,
the app's own ID — see [App identity](#app-identity)) and cannot be declared
here.

| Key | Type | Required | Rule |
|---|---|---|---|
| `required` | bool | no (default `true`) | Whether the app must have this variable to run |
| `description` | string | yes | 1-200 characters — what the key is for, shown next to it in the catalog |
| `help_url` | string | no | An `http://` or `https://` page where to obtain a value (an API key signup page, say) |

A `required = false` variable that has no value assigned is left entirely
unset — never an empty string.

## Parameters

`[params.<name>]` declares one launch-time parameter, substituted into
`command` and `url` wherever `{name}` appears. `<name>` must match
`^[a-z][a-z0-9_]{0,31}$`. A parameter with no `default` is required input
before the app can launch.

| `type` | Extra keys | Substituted as |
|---|---|---|
| `string` | `default`, `description` (a string), `pattern` (a restricted regular expression, `re.fullmatch`, at most 256 characters), `max_length` (default 256) | the value itself |
| `int` / `float` | `default`, `description` (a string), `min`, `max` | the value's decimal representation |
| `enum` | `choices` (required, non-empty array of strings), `default`, `description` (a string) | the chosen value |
| `bool` | `default`, `description` (a string), `flag` (required) | see below |

A `bool` parameter does not have a substituted value — when true, the whole
`command`/`url` element equal to `{name}` is replaced with `flag`; when
false, that element is dropped entirely. Because of this, `{name}` for a
`bool` parameter must be the *entire* element it appears in — `"--x={verbose}"`
mixing it into a larger string is a validation error, unlike every other
param type, which may share an element with other text or other placeholders
(`"--addr={host}:{port}"` is fine).

Two placeholder names are reserved and never declared as params: `{host}`
(the device's hostname) and `{app_dir}` (the app's own directory, as an
absolute path). Every other placeholder used in `command` or `url` must name
a declared param — an undefined one is a validation error, the same way an
unrecognized top-level key is: a typo in a placeholder name should not
silently pass the literal `{typo}` through to argv instead of a real value.

## Display assets

A schema 2 manifest declares the face-display assets the app plays. The host
converts each file when the app is installed (a file it cannot convert fails
the install) and writes the converted assets to the display before the app
starts. The app plays them by id, `<app ID>/<name>`; see the
[face display protocol](display-protocol.md).

```toml
schema = 2

[assets.welcome]
file = "assets/welcome.json"      # Lottie, GIF, PNG, or JPEG
description = "A welcoming smile for a visitor who just arrived."
tags = ["greeting"]

[assets.welcome.params.eye_color]
description = "Eye colour as RGBA, 0-1 each."
```

| Key | Type | Required | Rule |
|---|---|---|---|
| `[assets.<name>]` | table | — | `<name>` matches `^[a-z0-9][a-z0-9._-]{0,39}$` and is not `include` |
| `file` | string | yes | A path inside the app directory: relative, no `..`, not a symlink. Lottie (`.json`), GIF, PNG, or JPEG |
| `description` | string | yes | 1-200 characters. Tells an agent when to use the asset |
| `tags` | array of strings | no | Each matches `^[a-z0-9-]{1,32}$`; an app filters its vocabulary by tag. Omitted means `[]` |
| `[assets.<name>.params.<sid>]` | table | no | One table per Lottie slot the app changes at run time. `<sid>` matches `^[a-z0-9_]{1,32}$` and must be a slot of that Lottie file; `description` (1-200 characters) is required. Only a Lottie file may have params. Slots of the file that are not declared here stay at their defaults and are not offered to an agent |
| `[assets] include` | string | no | A TOML file in the app directory (relative, no `..`, not a symlink) whose only top-level key is `assets`, holding more `[assets.<name>]` tables in the same form. Its `file` paths are relative to the app directory, like the manifest's. It may not have an `include` of its own, and names must not repeat across the two files |

The host checks that the standard assets plus this app's assets fit the
display's storage and refuses the install otherwise. While the app runs, the
host writes the resolved declarations — includes expanded, translations
attached — to `/run/palmimo/apps/<app ID>/assets.json`, which the app can read
and `palmimo_sdk` reads to build the app's vocabulary. Each asset's kind and
slot dimensions come from the display itself (`ASSET INFO`), not from this file,
so a standard asset the user replaced is described correctly:

```json
{
  "schema": 1,
  "assets": [
    {
      "id": "zip.my-app/welcome",
      "description": "A welcoming smile for a visitor who just arrived.",
      "tags": ["greeting"],
      "params": {"eye_color": {"description": "Eye colour as RGBA, 0-1 each."}},
      "translations": {"ja": {"description": "来場者を迎える笑顔", "params.eye_color": "目の色（RGBA、各 0〜1）"}}
    }
  ]
}
```

The standard assets are described by a file of the same form, installed by the
platform at `/usr/local/share/palmimo/display-assets/catalog.json`. An app can
play standard assets and its own assets; `palmimo_sdk` offers no others.

## Validation rules

A manifest fails validation — and the app cannot be listed or launched —
if any of the following holds:

- `schema` is not `1` or `2`, or `[assets]` appears with `schema = 1`
- an unrecognized top-level key is present
- `name` does not match its pattern
- `command` is empty
- a placeholder in `command` or `url` names neither a reserved placeholder
  nor a declared param — this includes a placeholder whose name doesn't even
  match a param's own pattern (`^[a-z][a-z0-9_]{0,31}$`), such as `{}` or
  `{foo-bar}`: nothing declared could ever have that name, so it is undefined
  the same way a typo'd real name would be
- `devices` contains a value outside `camera`, `audio`, `motor_display`
- an `[env.<NAME>]` table has an unrecognized key (anything other than
  `required`, `description`, `help_url`)
- an `[env.<NAME>]` name collides with the reserved `PALMIMO_` prefix
- an `[env.<NAME>]` table has no `description`
- an `[env.<NAME>]`'s `required` is not a bool, or its `help_url` is not an
  `http://` or `https://` URL
- a `[params.<name>]` table has an unrecognized key for its `type` (e.g.
  `choices` on a `string` param, or `flag` on an `int` one)
- a param's `min`, `max`, or `max_length` is the wrong type (`min`/`max` must
  be numbers; `max_length` must be a positive int), or `choices` is not a
  non-empty array of strings
- a param's `default` does not match its own declared `type` (an `int`
  param's `default` must be an int, not e.g. the string `"8000"`; likewise
  for `float`, `enum`, `bool`, and `string`)
- a param's `default` fails its own constraints: outside `min`/`max` for
  `int`/`float`, not one of `choices` for `enum`, or longer than
  `max_length` / not matching `pattern` for `string`
- a param's `description`, when present, is not a string
- a `string` param's `pattern` is longer than 256 characters or uses anything
  other than anchors (`^`, `$`), literal characters, escapes, character
  classes (`[...]`), and one quantifier (`?`, `*`, `+`, `{m}`, or `{m,n}`)
  applied to one character, escape, or character class. Groups, alternatives,
  backreferences, and lookahead or lookbehind assertions are not supported.
- a `bool` param has no `flag`
- a `bool` param's placeholder is mixed into a larger element instead of
  being the entire element
- an `[assets.<name>]` table has an unrecognized key, a name outside its
  pattern, a missing `file` or `description`, a `file` outside the app
  directory or behind a symlink, or a tag outside its pattern
- an `[assets.<name>.params.<sid>]` table has no `description`
- `[assets] include` names a missing file, a file outside the app directory or
  behind a symlink, a file with a top-level key other than `assets`, a file that
  itself has an `include`, or a file that repeats a name
- an `[assets.<name>.params.<sid>]` names a slot the Lottie file does not have,
  or appears on an asset that is not a Lottie file

## Official catalog

An examples release includes a generated catalog for hosts that list official
apps. Each entry's `source` identifies the app's Git repository, `subdir`,
manifest (when it is not `palmimo.toml`), and release tag. It also includes
`commit`, the commit SHA the release tag pointed to when the catalog was
generated. A host must confirm that the tag checkout resolves to that SHA
before installing an official app; this rejects a tag that was moved after
release.

## Files in this repository

| Path | Role |
|---|---|
| [examples/teleop/palmimo.toml](../../examples/teleop/palmimo.toml) | The example above |
| [examples/agents/companion/palmimo.toml](../../examples/agents/companion/palmimo.toml) | Companion agent manifest |
| [examples/agents/companion/palmimo.realtime.toml](../../examples/agents/companion/palmimo.realtime.toml) | Companion OpenAI Realtime voice runtime, a separate app in the same directory |
| [examples/agents/wakeword/palmimo.toml](../../examples/agents/wakeword/palmimo.toml) | Wake-word agent manifest |
