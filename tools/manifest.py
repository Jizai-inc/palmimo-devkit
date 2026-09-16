"""Reference validator for `palmimo.toml` app manifests (schema 1).

Validates the rules in [doc/reference/app-manifest.md](../doc/reference/app-manifest.md).
Portal (palmimo-portal) owns the one real implementation that runs on-device;
this module exists so this repository's own tests can assert its example
manifests and invalid fixtures agree with the published spec.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


#: Matches `palmimo.toml` (the default manifest) or `palmimo.<variant>.toml`,
#: where `<variant>` is the same shape as an app `name`. Anything else in an
#: app directory -- a stray `palmimo_backup.toml`, a differently-cased
#: `palmimo.TOML` -- is not a manifest.
MANIFEST_FILENAME_RE = re.compile(r"^palmimo(?:\.[a-z][a-z0-9-]{0,39})?\.toml$")

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
PARAM_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
RESERVED_ENV_PREFIX = "PALMIMO_"
KNOWN_DEVICES = frozenset({"camera", "audio", "motor_display"})
PARAM_TYPES = frozenset({"string", "int", "float", "enum", "bool"})
RESERVED_PLACEHOLDERS = frozenset({"host", "app_dir"})
DESCRIPTION_MAX_LENGTH = 200
DEFAULT_MAX_LENGTH = 256
PATTERN_MAX_LENGTH = 200
TOP_LEVEL_KEYS = frozenset({"schema", "name", "description", "command", "url", "devices", "env", "params"})
ENV_TABLE_KEYS = frozenset({"required", "description", "help_url"})
# Extra keys each param type accepts beyond {type, default, description} (both
# always allowed: default is optional-required by every type, description is
# free-form documentation the example manifests use throughout).
PARAM_TYPE_EXTRA_KEYS: dict[str, frozenset[str]] = {
    "string": frozenset({"pattern", "max_length"}),
    "int": frozenset({"min", "max"}),
    "float": frozenset({"min", "max"}),
    "enum": frozenset({"choices"}),
    "bool": frozenset({"flag"}),
}

_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")

# Numbers, but not bool: TOML/Python's bool is a subtype of int, and a
# `default = true` on an int/float param would otherwise pass isinstance(x, int).
_NUMBER_TYPES = (int, float)


def _is_number(value: object) -> bool:
    return isinstance(value, _NUMBER_TYPES) and not isinstance(value, bool)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class ManifestError(Exception):
    """One or more `palmimo.toml` validation failures, collected rather than raised one at a time.

    Mirrors Portal's own error shape (a list, so an API caller sees every
    problem in one response) without depending on Portal's code.
    """

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class EnvVar:
    required: bool
    description: str
    help_url: str | None


@dataclass(frozen=True)
class Param:
    type: str
    default: object | None
    min: float | None = None
    max: float | None = None
    choices: tuple[str, ...] | None = None
    pattern: str | None = None
    max_length: int | None = None
    flag: str | None = None


@dataclass(frozen=True)
class AppManifest:
    name: str
    description: str
    command: tuple[str, ...]
    url: str | None
    devices: tuple[str, ...]
    env: dict[str, EnvVar]
    params: dict[str, Param]


def is_manifest_filename(name: str) -> bool:
    """Whether *name* is a `palmimo.toml` manifest filename (see MANIFEST_FILENAME_RE)."""
    return MANIFEST_FILENAME_RE.fullmatch(name) is not None


def discover_manifests(app_dir: Path) -> list[Path]:
    """Return every manifest file directly in *app_dir*, sorted for deterministic output.

    Non-recursive: an app's own package directories (e.g.
    `palmimo_companion_agent/`) are never searched.
    """
    return sorted(path for path in app_dir.iterdir() if path.is_file() and is_manifest_filename(path.name))


def load_manifest(path: Path) -> AppManifest:
    """Parse and validate one `palmimo.toml` file.

    Raises:
        ManifestError: if the file violates any rule in
            doc/reference/app-manifest.md's validation table.
    """
    with path.open("rb") as f:
        raw = tomllib.load(f)
    errors: list[str] = []
    _check_top_level_keys(raw, errors)
    _check_schema(raw, errors)
    name = raw.get("name")
    _check_name(name, errors)
    description = raw.get("description")
    if not isinstance(description, str) or not (1 <= len(description) <= DESCRIPTION_MAX_LENGTH):
        errors.append("'description' must be a string of 1-200 characters")
    command = _check_command(raw, errors)
    devices = _check_devices(raw, errors)
    env = _check_env(raw, errors)
    params = _check_params(raw, errors)
    url = raw.get("url")
    if url is not None and (not isinstance(url, str) or not url.startswith(("http://", "https://"))):
        errors.append("'url' must start with http:// or https://")
    _check_placeholders(command, url if isinstance(url, str) else None, params, errors)

    if errors:
        raise ManifestError(errors)

    # Every append above that could leave name/description invalid also
    # populated errors, so reaching here means both are well-formed strings.
    assert isinstance(name, str)
    assert isinstance(description, str)

    return AppManifest(
        name=name,
        description=description,
        command=tuple(command),
        url=url,
        devices=tuple(devices),
        env=env,
        params=params,
    )


def _check_top_level_keys(raw: dict, errors: list[str]) -> None:
    unknown = set(raw) - TOP_LEVEL_KEYS
    if unknown:
        errors.append(f"unknown top-level key(s): {sorted(unknown)}")


def _check_schema(raw: dict, errors: list[str]) -> None:
    if raw.get("schema") != 1:
        errors.append(f"'schema' must be 1, got {raw.get('schema')!r}")


def _check_name(name: object, errors: list[str]) -> None:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        errors.append(f"'name' must match {NAME_RE.pattern!r}, got {name!r}")


def _check_command(raw: dict, errors: list[str]) -> list[str]:
    command = raw.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(c, str) for c in command):
        errors.append("'command' must be a non-empty array of strings")
        return []
    return command


def _check_devices(raw: dict, errors: list[str]) -> list[str]:
    devices = raw.get("devices", [])
    if not isinstance(devices, list) or not all(isinstance(d, str) for d in devices):
        errors.append("'devices' must be an array of strings")
        return []
    unknown = [d for d in devices if d not in KNOWN_DEVICES]
    if unknown:
        errors.append(f"unknown device(s): {unknown}, expected a subset of {sorted(KNOWN_DEVICES)}")
    return devices


def _check_env(raw: dict, errors: list[str]) -> dict[str, EnvVar]:
    env_table = raw.get("env", {})
    result: dict[str, EnvVar] = {}
    if not isinstance(env_table, dict):
        errors.append("'env' must be a table of tables")
        return result
    for name, table in env_table.items():
        if not ENV_NAME_RE.fullmatch(name):
            errors.append(f"env name {name!r} must match {ENV_NAME_RE.pattern!r}")
        if name.startswith(RESERVED_ENV_PREFIX):
            errors.append(f"env name {name!r} is reserved (the '{RESERVED_ENV_PREFIX}' prefix is runtime-injected)")
        if not isinstance(table, dict):
            errors.append(f"[env.{name}] must be a table")
            continue
        unknown = set(table) - ENV_TABLE_KEYS
        if unknown:
            errors.append(f"[env.{name}] has unknown key(s): {sorted(unknown)}")
        description = table.get("description")
        if not isinstance(description, str) or not (1 <= len(description) <= DESCRIPTION_MAX_LENGTH):
            errors.append(f"[env.{name}] must have a 'description' of 1-200 characters")
            description = ""
        required = table.get("required", True)
        if not isinstance(required, bool):
            errors.append(f"[env.{name}] 'required' must be a bool, got {required!r}")
            required = True
        help_url = table.get("help_url")
        if help_url is not None and not isinstance(help_url, str):
            errors.append(f"[env.{name}] 'help_url' must be a string, got {help_url!r}")
            help_url = None
        result[name] = EnvVar(required=required, description=description, help_url=help_url)
    return result


def _check_params(raw: dict, errors: list[str]) -> dict[str, Param]:
    params_table = raw.get("params", {})
    result: dict[str, Param] = {}
    if not isinstance(params_table, dict):
        errors.append("'params' must be a table of tables")
        return result
    for name, table in params_table.items():
        if not PARAM_NAME_RE.fullmatch(name):
            errors.append(f"param name {name!r} must match {PARAM_NAME_RE.pattern!r}")
        if not isinstance(table, dict):
            errors.append(f"[params.{name}] must be a table")
            continue
        param_type = table.get("type")
        if param_type not in PARAM_TYPES:
            errors.append(f"[params.{name}] 'type' must be one of {sorted(PARAM_TYPES)}, got {param_type!r}")
            continue
        allowed = {"type", "default", "description"} | PARAM_TYPE_EXTRA_KEYS[param_type]
        unknown = set(table) - allowed
        if unknown:
            errors.append(f"[params.{name}] has unknown key(s) for type {param_type!r}: {sorted(unknown)}")
        result[name] = _check_param_by_type(name, param_type, table, errors)
    return result


def _check_param_by_type(name: str, param_type: str, table: dict, errors: list[str]) -> Param:
    default = table.get("default")
    if param_type == "bool":
        flag = table.get("flag")
        if not isinstance(flag, str) or not flag:
            errors.append(f"[params.{name}] type 'bool' requires a non-empty 'flag'")
            flag = None
        if default is not None and not isinstance(default, bool):
            errors.append(f"[params.{name}] default {default!r} must be a bool, matching type 'bool'")
        return Param(type=param_type, default=default, flag=flag)

    if param_type == "enum":
        choices = table.get("choices")
        if not isinstance(choices, list) or not choices or not all(isinstance(c, str) for c in choices):
            errors.append(f"[params.{name}] type 'enum' requires a non-empty 'choices' array of strings")
            choices = []
        elif default is not None and not isinstance(default, str):
            errors.append(f"[params.{name}] default {default!r} must be a string, matching type 'enum'")
        elif default is not None and default not in choices:
            errors.append(f"[params.{name}] default {default!r} is not one of its choices {choices!r}")
        return Param(type=param_type, default=default, choices=tuple(choices))

    if param_type in ("int", "float"):
        checker = _is_int if param_type == "int" else _is_number
        min_value = table.get("min")
        max_value = table.get("max")
        if min_value is not None and not _is_number(min_value):
            errors.append(f"[params.{name}] 'min' must be a number, got {min_value!r}")
            min_value = None
        if max_value is not None and not _is_number(max_value):
            errors.append(f"[params.{name}] 'max' must be a number, got {max_value!r}")
            max_value = None
        if default is not None and not checker(default):
            errors.append(
                f"[params.{name}] default {default!r} must be a {param_type} value, matching type {param_type!r}"
            )
        elif default is not None and min_value is not None and default < min_value:
            errors.append(f"[params.{name}] default {default!r} is below its 'min' {min_value!r}")
        elif default is not None and max_value is not None and default > max_value:
            errors.append(f"[params.{name}] default {default!r} is above its 'max' {max_value!r}")
        return Param(type=param_type, default=default, min=min_value, max=max_value)

    # string
    pattern = table.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str) or len(pattern) > PATTERN_MAX_LENGTH:
            errors.append(f"[params.{name}] 'pattern' must be a string of at most {PATTERN_MAX_LENGTH} characters")
            pattern = None
        else:
            try:
                re.compile(pattern)
            except re.error as exc:
                errors.append(f"[params.{name}] 'pattern' is not a valid regular expression: {exc}")
                pattern = None
    max_length = table.get("max_length", DEFAULT_MAX_LENGTH)
    if not _is_int(max_length) or max_length <= 0:
        errors.append(f"[params.{name}] 'max_length' must be a positive int, got {max_length!r}")
        max_length = DEFAULT_MAX_LENGTH
    if default is not None:
        if not isinstance(default, str):
            errors.append(f"[params.{name}] default {default!r} must be a string, matching type 'string'")
        else:
            if len(default) > max_length:
                errors.append(f"[params.{name}] default {default!r} exceeds 'max_length' {max_length}")
            if pattern is not None and re.fullmatch(pattern, default) is None:
                errors.append(f"[params.{name}] default {default!r} does not match 'pattern' {pattern!r}")
    return Param(type=param_type, default=default, pattern=pattern, max_length=max_length)


def _check_placeholders(command: list[str], url: str | None, params: dict[str, Param], errors: list[str]) -> None:
    """Validate every `{placeholder}` in *command* and *url*.

    Two rules from doc/reference/app-manifest.md: every placeholder name must
    be reserved (`host`, `app_dir`) or a declared param, and a `bool` param's
    placeholder may only appear as an element on its own -- mixed into a
    larger string (`"--x={verbose}"`) it can't be replaced with a whole flag
    or removed.
    """
    elements = list(command) + ([url] if url is not None else [])
    for element in elements:
        names = _PLACEHOLDER_RE.findall(element)
        for placeholder_name in names:
            if placeholder_name in RESERVED_PLACEHOLDERS:
                continue
            param = params.get(placeholder_name)
            if param is None:
                errors.append(f"undefined placeholder {{{placeholder_name}}} in {element!r}")
                continue
            if param.type == "bool" and element != f"{{{placeholder_name}}}":
                errors.append(f"bool param {{{placeholder_name}}} must be its own element, not mixed into {element!r}")
