"""Validate optional display translations alongside a schema-1 app manifest."""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path


MAX_TRANSLATIONS_BYTES = 65_536
LOCALE_RE = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]{2,8})*$")


def normalize_tag(value: str) -> str:
    """Return a display tag following the manifest's ordered Unicode rules."""
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r" +", " ", "".join(" " if unicodedata.category(c) == "Zs" else c for c in value)).strip(" ")
    if (
        not 1 <= len(value) <= 32
        or "." in value
        or any(unicodedata.category(c) in {"Cc", "Cf", "Zl", "Zp", "Cs", "Co"} for c in value)
    ):
        raise ValueError("tag must contain 1-32 code points without dots or forbidden Unicode categories")
    return value


def tag_key(value: str) -> str:
    """Lowercase only ASCII A-Z in an already normalized tag."""
    return value.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))


def load_translations(
    path: Path, env: set[str], params: set[str], tags: tuple[str, ...] = ()
) -> dict[str, dict[str, str]]:
    """Reject invalid author input; older hosts ignore the companion file entirely."""
    companion = path.with_name(f"{path.name}.i18n.json")
    if companion.is_symlink():
        raise ValueError(f"translation file must not be a symlink: {companion}")
    if not companion.exists():
        return {}
    with companion.open("rb") as stream:
        content = stream.read(MAX_TRANSLATIONS_BYTES + 1)
    if len(content) > MAX_TRANSLATIONS_BYTES:
        raise ValueError(f"translation file exceeds {MAX_TRANSLATIONS_BYTES} bytes: {companion}")
    raw = json.loads(content)
    if (
        not isinstance(raw, dict)
        or set(raw) != {"schema", "translations"}
        or type(raw["schema"]) is not int
        or raw["schema"] != 1
    ):
        raise ValueError("translation file must contain schema 1 and translations")
    translations = raw["translations"]
    if not isinstance(translations, dict) or not 1 <= len(translations) <= 32:
        raise ValueError("translations must contain 1-32 locales")
    keys = {"description"} | {f"env.{name}" for name in env} | {f"params.{name}" for name in params}
    keys |= {f"tags.{tag_key(tag)}" for tag in tags}
    result: dict[str, dict[str, str]] = {}
    for locale, messages in translations.items():
        if not LOCALE_RE.fullmatch(locale) or not isinstance(messages, dict):
            raise ValueError("translation locale must be a lowercase language tag with a message object")
        result[locale] = {}
        for key, value in messages.items():
            if key.startswith("tags."):
                key = f"tags.{tag_key(normalize_tag(key[5:]))}"
            if key not in keys or not isinstance(value, str) or not 1 <= len(value.strip()) <= 200:
                raise ValueError(f"invalid translation key or text: {locale}.{key}")
            result[locale][key] = value
    return result
