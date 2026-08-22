from __future__ import annotations

import re
from collections.abc import Iterable
from enum import Enum
from html import escape
from xml.etree.ElementTree import Element

import css_inline
import nh3
import tinycss2
import tinyhtml5

SAFE_HTML_TAGS = {
    "a", "abbr", "address", "article", "b", "bdi", "bdo", "blockquote", "body",
    "br", "caption", "center", "cite", "code", "col", "colgroup", "dd", "del",
    "div", "dl", "dt", "em", "figcaption", "figure", "font", "footer", "h1",
    "h2", "h3", "h4", "h5", "h6", "head", "header", "hr", "html", "i", "ins",
    "li", "main", "mark", "ol", "p", "pre", "q", "rp", "rt", "ruby", "s",
    "section", "small", "span", "strike", "strong", "style", "sub", "sup",
    "table", "tbody", "td", "tfoot", "th", "thead", "title", "tr", "u", "ul",
    "wbr",
}
OUTPUT_HTML_TAGS = SAFE_HTML_TAGS - {"html", "head", "body", "style", "title"}
REJECTED_HTML_TAGS = {
    "audio", "base", "button", "canvas", "embed", "form", "iframe", "img",
    "input", "link", "math", "meta", "object", "option", "picture", "script",
    "select", "source", "svg", "template", "textarea", "video",
}
SAFE_CSS_PROPERTIES = {
    "background-color", "border", "border-bottom", "border-bottom-color",
    "border-bottom-style", "border-bottom-width", "border-collapse", "border-color",
    "border-left", "border-left-color", "border-left-style", "border-left-width",
    "border-radius", "border-right", "border-right-color", "border-right-style",
    "border-right-width", "border-spacing", "border-style", "border-top",
    "border-top-color", "border-top-style", "border-top-width", "border-width",
    "color", "display", "font-family", "font-size", "font-style", "font-weight",
    "height", "line-height", "margin", "margin-bottom", "margin-left", "margin-right",
    "margin-top", "max-width", "padding", "padding-bottom", "padding-left",
    "padding-right", "padding-top", "text-align", "text-decoration",
    "vertical-align", "white-space", "width",
}


class AuthoredHtmlError(ValueError):
    """Authored-draft rejection whose message describes only caller-submitted HTML."""


_SAFE_DETAIL = re.compile(r"[^A-Za-z0-9 :;,\.\-_()#%/]+")


def _safe_detail(message: str) -> str:
    return _SAFE_DETAIL.sub("?", message)[:200] or "authored HTML rejected"


class HtmlPolicy(Enum):
    INBOUND = "inbound"
    AUTHORED = "authored"


INBOUND_POLICY = HtmlPolicy.INBOUND
AUTHORED_POLICY = HtmlPolicy.AUTHORED
_AUTHORED_BLOCKED_PROPERTIES = {
    "position", "top", "right", "bottom", "left", "z-index", "transform", "content",
}
_ZERO_LENGTH = re.compile(r"^[-+]?0(?:\.0+)?(?:[a-z%]+)?$", re.IGNORECASE)
_LARGE_NEGATIVE_LENGTH = re.compile(r"^\s*(-\d+(?:\.\d+)?)(?:px|pt|pc|em|rem|in|cm|mm|%)?\s*$", re.IGNORECASE)
SAFE_ATTRIBUTES = {
    "*": {"dir", "lang", "style"},
    "a": {"href", "target", "title"},
    "font": {"color", "face", "size"},
    "ol": {"start", "type"},
    "table": {"align", "bgcolor", "border", "cellpadding", "cellspacing", "height", "role", "width"},
    "td": {"align", "bgcolor", "colspan", "height", "nowrap", "rowspan", "valign", "width"},
    "th": {"align", "bgcolor", "colspan", "height", "nowrap", "rowspan", "scope", "valign", "width"},
    "tr": {"align", "bgcolor", "valign"},
    "col": {"span", "width"},
    "colgroup": {"span", "width"},
    "p": {"align"},
    "div": {"align"},
}
_PREPARE_ATTRIBUTES = {
    **SAFE_ATTRIBUTES,
    "*": SAFE_ATTRIBUTES["*"] | {"class", "id"},
}
# Values here are validated then dropped by _PREPARE_CLEANER; _OUTPUT_CLEANER sets rel
# itself. Tolerating them only unblocks read->draft round-trips.
_AUTHORED_TOLERATED_ATTRIBUTES = {
    "a": {"name", "rel"},
    "td": {"abbr", "headers"},
    "th": {"abbr", "headers"},
}
_CLEAN_CONTENT_TAGS = REJECTED_HTML_TAGS | {"style", "title"}
_SAFE_FUNCTIONS = {"hsl", "hsla", "rgb", "rgba"}
_SAFE_DISPLAY_VALUES = {"block", "inline", "inline-block", "table", "table-cell", "table-row"}
_HTML_NAMESPACE = "http://www.w3.org/1999/xhtml"


def _local_name(element: Element) -> str | None:
    if not isinstance(element.tag, str):
        return None
    namespace, separator, name = (
        element.tag[1:].partition("}")
        if element.tag.startswith("{")
        else ("", "", element.tag)
    )
    if separator and namespace != _HTML_NAMESPACE:
        raise ValueError("unsupported draft HTML namespace")
    return name.casefold()


_LANG_VALUE = re.compile(r"^[A-Za-z0-9]{1,8}(?:-[A-Za-z0-9]{1,8})*$")


def _safe_href(value: str) -> str | None:
    normalized = value.strip("\t\n\r\f ")
    if not normalized or normalized.startswith("//"):
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        return None
    scheme, separator, _ = normalized.partition(":")
    if not separator or scheme.casefold() not in {"http", "https", "mailto", "tel"}:
        return None
    return normalized


def _attribute_filter(tag: str, attribute: str, value: str) -> str | None:
    if attribute == "dir":
        return value.casefold() if value.casefold() in {"ltr", "rtl", "auto"} else None
    if attribute == "lang":
        return value if _LANG_VALUE.fullmatch(value) else None
    if attribute == "href":
        return _safe_href(value)
    if attribute == "target":
        return "_blank" if value.casefold() == "_blank" else None
    return value


def _sanitize_inbound_style(value: str) -> str | None:
    if any(marker in value for marker in ("\\", "<", ">")):
        return None
    safe: list[object] = []
    declarations = tinycss2.parse_declaration_list(value, skip_comments=True, skip_whitespace=True)
    for declaration in declarations:
        if declaration.type != "declaration" or declaration.lower_name not in SAFE_CSS_PROPERTIES:
            continue
        try:
            _validate_css_declaration(declaration, policy=INBOUND_POLICY)
        except ValueError:
            continue
        safe.append(declaration)
    serialized = tinycss2.serialize(safe).strip()
    return serialized or None


def _inbound_attribute_filter(tag: str, attribute: str, value: str) -> str | None:
    if attribute == "style":
        return _sanitize_inbound_style(value)
    return _attribute_filter(tag, attribute, value)


def _iter_component_values(tokens: Iterable[object]) -> Iterable[object]:
    for token in tokens:
        yield token
        content = getattr(token, "arguments", None)
        if content is None:
            content = getattr(token, "content", None)
        if content is not None:
            yield from _iter_component_values(content)


def _validate_css_declarations(tokens: Iterable[object], *, policy: HtmlPolicy) -> None:
    declarations = tinycss2.parse_declaration_list(tokens, skip_comments=True, skip_whitespace=True)
    for declaration in declarations:
        if declaration.type == "error":
            raise ValueError("invalid draft CSS declaration")
        if declaration.type != "declaration":
            raise ValueError("CSS at-rules are not supported")
        _validate_css_declaration(declaration, policy=policy)


def _validate_css_declaration(declaration: object, *, policy: HtmlPolicy) -> None:
    name = declaration.lower_name
    if policy is INBOUND_POLICY and (name.startswith("--") or name not in SAFE_CSS_PROPERTIES):
        raise ValueError(f"unsupported draft CSS property: {name}")
    serialized = tinycss2.serialize(declaration.value)
    if len(serialized) > 4096:
        raise ValueError("draft CSS value is too large")
    collapsed = " ".join(serialized.split())
    folded = "".join(serialized.split()).casefold()
    if any(marker in folded for marker in (
        "url(", "@import", "image-set(", "expression(", "javascript:",
        "vbscript:", "data:", "-moz-binding",
    )) or any(marker in collapsed for marker in ("\\", ";", "<", ">")):
        raise ValueError(f"unsafe draft CSS property: {name}")
    if name == "behavior":
        raise ValueError("unsafe draft CSS property: behavior")
    for token in _iter_component_values(declaration.value):
        token_type = getattr(token, "type", "")
        if token_type == "error" or token_type == "url":
            raise ValueError("unsafe draft CSS value")
        if policy is INBOUND_POLICY and token_type == "function" and token.lower_name not in _SAFE_FUNCTIONS:
            raise ValueError(f"unsupported draft CSS function: {token.lower_name}")
    if policy is INBOUND_POLICY and name == "display" and folded not in _SAFE_DISPLAY_VALUES:
        raise ValueError("unsupported draft CSS display value")
    if policy is AUTHORED_POLICY:
        if folded in {"fixed", "sticky"}:
            raise ValueError(f"authored HTML CSS value is not allowed: {name}")
        if name in _AUTHORED_BLOCKED_PROPERTIES:
            raise ValueError(f"authored HTML CSS property is not allowed: {name}")
        if name == "display" and folded == "none":
            raise ValueError("authored HTML must not hide content with display")
        if name == "visibility" and folded == "hidden":
            raise ValueError("authored HTML must not hide content with visibility")
        if name == "opacity":
            try:
                if float(folded) < 0.1:
                    raise ValueError("authored HTML opacity must be at least 0.1")
            except ValueError as exc:
                if "at least" in str(exc):
                    raise
        if name == "font-size" and _ZERO_LENGTH.fullmatch(folded):
            raise ValueError("authored HTML font-size must not be zero")
        if name == "text-indent":
            match = _LARGE_NEGATIVE_LENGTH.fullmatch(folded)
            if match and float(match.group(1)) <= -100:
                raise ValueError("authored HTML text-indent is too negative")
        if name == "float" and folded not in {"left", "right", "none"}:
            raise ValueError("authored HTML float must be left, right, or none")


def _validate_stylesheet(value: str, *, policy: HtmlPolicy) -> None:
    rules = tinycss2.parse_stylesheet(value, skip_comments=True, skip_whitespace=True)
    for rule in rules:
        if rule.type == "error":
            raise ValueError("invalid draft stylesheet")
        if rule.type != "qualified-rule":
            raise ValueError("CSS at-rules are not supported")
        if not tinycss2.serialize(rule.prelude).strip():
            raise ValueError("empty draft CSS selector")
        _validate_css_declarations(rule.content, policy=policy)


def _validate_document(value: str, *, policy: HtmlPolicy) -> None:
    root = tinyhtml5.parse(value)
    for element in root.iter():
        tag = _local_name(element)
        if tag is None:
            continue
        if tag in REJECTED_HTML_TAGS or tag not in SAFE_HTML_TAGS:
            raise ValueError(f"unsupported draft HTML tag: {tag}")
        for raw_name, attribute_value in element.attrib.items():
            name = raw_name.casefold()
            if name.startswith("on"):
                raise ValueError(f"unsupported draft HTML attribute: {name}")
            allowed = _PREPARE_ATTRIBUTES.get("*", set()) | _PREPARE_ATTRIBUTES.get(tag, set())
            if policy is AUTHORED_POLICY:
                allowed = allowed | _AUTHORED_TOLERATED_ATTRIBUTES.get(tag, set())
            if name not in allowed:
                raise ValueError(f"unsupported draft HTML attribute: {name}")
            if name == "href" and _safe_href(attribute_value) is None:
                raise ValueError("unsafe draft HTML link")
            if name == "style":
                if "\\" in attribute_value:
                    raise ValueError("draft CSS backslash escapes are not supported")
                _validate_css_declarations(attribute_value, policy=policy)
        if tag == "style":
            if "\\" in (element.text or ""):
                raise ValueError("draft CSS backslash escapes are not supported")
            _validate_stylesheet(element.text or "", policy=policy)


_PREPARE_CLEANER = nh3.Cleaner(
    tags=SAFE_HTML_TAGS,
    attributes=_PREPARE_ATTRIBUTES,
    clean_content_tags=REJECTED_HTML_TAGS,
    attribute_filter=_attribute_filter,
    filter_style_properties=None,
    url_schemes={"http", "https", "mailto", "tel"},
    url_relative="deny",
    link_rel=None,
    strip_comments=True,
)
_OUTPUT_CLEANER = nh3.Cleaner(
    tags=OUTPUT_HTML_TAGS,
    attributes=SAFE_ATTRIBUTES,
    clean_content_tags=_CLEAN_CONTENT_TAGS,
    attribute_filter=_attribute_filter,
    filter_style_properties=None,
    url_schemes={"http", "https", "mailto", "tel"},
    url_relative="deny",
    link_rel="noopener noreferrer",
    strip_comments=True,
)
_INBOUND_CLEANER = nh3.Cleaner(
    tags=OUTPUT_HTML_TAGS,
    attributes=SAFE_ATTRIBUTES,
    clean_content_tags=_CLEAN_CONTENT_TAGS,
    attribute_filter=_inbound_attribute_filter,
    filter_style_properties=SAFE_CSS_PROPERTIES,
    url_schemes={"http", "https", "mailto", "tel"},
    url_relative="deny",
    link_rel="noopener noreferrer",
    strip_comments=True,
)
_INLINER = css_inline.CSSInliner(
    inline_style_tags=True,
    keep_style_tags=False,
    keep_link_tags=False,
    keep_at_rules=False,
    minify_css=True,
    load_remote_stylesheets=False,
)


def _prepare_authored_html(value: str, *, maximum_bytes: int) -> str:
    if len(value.encode("utf-8")) > maximum_bytes:
        raise ValueError("draft HTML body exceeds the 2 MB limit")
    _validate_document(value, policy=AUTHORED_POLICY)
    sanitized = _PREPARE_CLEANER.clean(value)
    inlined = _INLINER.inline(sanitized)
    inlined_root = tinyhtml5.parse(inlined)
    body_style = next(
        (
            element.attrib.get("style")
            for element in inlined_root.iter()
            if _local_name(element) == "body"
        ),
        None,
    )
    if body_style is not None:
        _validate_css_declarations(body_style, policy=AUTHORED_POLICY)
    prepared = _OUTPUT_CLEANER.clean(inlined)
    _validate_inline_styles(prepared, policy=AUTHORED_POLICY)
    body_attribute = f' style="{escape(body_style, quote=True)}"' if body_style else ""
    result = f"<!DOCTYPE html><html><head></head><body{body_attribute}>{prepared}</body></html>"
    if len(result.encode("utf-8")) > maximum_bytes:
        raise ValueError("prepared draft HTML body exceeds the 2 MB limit")
    return result


def prepare_authored_html(value: str, *, maximum_bytes: int) -> str:
    try:
        return _prepare_authored_html(value, maximum_bytes=maximum_bytes)
    except AuthoredHtmlError:
        raise
    except ValueError as exc:
        raise AuthoredHtmlError(_safe_detail(str(exc))) from exc


def _validate_inline_styles(value: str, *, policy: HtmlPolicy) -> None:
    root = tinyhtml5.parse(value)
    for element in root.iter():
        style = element.attrib.get("style")
        if style is not None:
            _validate_css_declarations(style, policy=policy)


def prepare_html(value: str, *, maximum_bytes: int) -> str:
    """Backward-compatible authored-HTML entry point."""
    return prepare_authored_html(value, maximum_bytes=maximum_bytes)


def sanitize_inbound_html(value: str, *, maximum_bytes: int) -> str:
    if len(value.encode("utf-8")) > maximum_bytes:
        raise ValueError("HTML body exceeds the 2 MB limit")
    result = _INBOUND_CLEANER.clean(value)
    _validate_inline_styles(result, policy=INBOUND_POLICY)
    if len(result.encode("utf-8")) > maximum_bytes:
        raise ValueError("HTML body exceeds the 2 MB limit")
    return result
