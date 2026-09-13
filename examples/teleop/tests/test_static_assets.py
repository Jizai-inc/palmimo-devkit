"""Cross-checks between `app.js` and `style.css` that no test against either file alone
can catch: an element JS hides via `el.hidden = true` needs a CSS rule that actually
hides it, since an ID-selector `display` rule (any of `#foo { display: ... }`)
otherwise beats the UA's `[hidden] { display: none }` default in specificity."""

import re
from pathlib import Path


STATIC_DIR = Path(__file__).parent.parent / "palmimo_teleop" / "static"

_GET_ELEMENT_BY_ID = re.compile(r'const (\w+)\s*=\s*document\.getElementById\("([\w-]+)"\)')
_SET_HIDDEN_TRUE = re.compile(r"\b(\w+)\.hidden\s*=\s*true\b")
_CSS_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")


def _ids_hidden_via_js(app_js: str) -> set[str]:
    """Every element id that some code path sets `.hidden = true` on."""
    id_by_var = dict(_GET_ELEMENT_BY_ID.findall(app_js))
    hidden_vars = set(_SET_HIDDEN_TRUE.findall(app_js))
    return {id_by_var[var] for var in hidden_vars if var in id_by_var}


def _ids_with_a_hidden_rule(style_css: str) -> set[str]:
    """Every element id with a `#id[hidden] { ... display: none ... }` CSS rule."""
    ids: set[str] = set()
    for selectors, body in _CSS_RULE.findall(style_css):
        if "display" not in body or "none" not in body:
            continue
        for selector in selectors.split(","):
            match = re.fullmatch(r"\s*#([\w-]+)\[hidden\]\s*", selector)
            if match:
                ids.add(match.group(1))
    return ids


def test_every_js_hidden_element_has_a_css_rule_that_hides_it() -> None:
    # Without this, an element JS sets `.hidden = true` on (e.g. `#controls`
    # when a viewer connects) can stay visually shown despite the `hidden`
    # attribute being set, because a same-specificity or stronger ID-selector
    # `display` rule (`#controls { display: flex }`) beats the UA's
    # `[hidden] { display: none }` default -- a regression pytest against
    # app.js or style.css alone would never see, since each file is correct
    # in isolation.
    app_js = (STATIC_DIR / "app.js").read_text()
    style_css = (STATIC_DIR / "style.css").read_text()
    hidden_by_js = _ids_hidden_via_js(app_js)
    assert hidden_by_js, "expected at least one element hidden via JS"
    assert hidden_by_js <= _ids_with_a_hidden_rule(style_css)
