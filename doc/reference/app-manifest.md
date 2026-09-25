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
| `schema` | int | yes | Only `1` is accepted today |
| `name` | string | yes | Must match `^[a-z][a-z0-9-]{0,39}$` — see [App identity](#app-identity) for what it is used for |
| `description` | string | yes | 1-200 characters. The catalog's one-line summary |
| `command` | array of strings | yes | The argv to execute. An array, not a shell string, so no shell ever parses it; an element may contain one or more `{param}` placeholders |
| `url` | string | no | A page to open once the app is running. May contain `{host}` and param placeholders. Must start with `http://` or `https://` (`javascript:` and similar are rejected) |
| `devices` | array of strings | no | A subset of `camera`, `audio`, `motor_display`. Omitted (or empty) opens no device at all |
| `[env.<NAME>]` | table | no | One table per environment variable the app reads — see [Environment variables](#environment-variables) |
| `[params.<name>]` | table | no | One table per launch-time parameter — see [Parameters](#parameters) |

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

## Validation rules

A manifest fails validation — and the app cannot be listed or launched —
if any of the following holds:

- `schema` is not `1`
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
