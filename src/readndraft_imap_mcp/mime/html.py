from __future__ import annotations

from collections.abc import Iterable
from html import escape
from xml.etree.ElementTree import Element

import css_inline
import nh3
import tinycss2
import tinyhtml5


SAFE_HTML_TAGS = {
    "a", "address", "article", "b", "blockquote", "body", "br", "caption",
    "center", "code", "col", "colgroup", "dd", "div", "dl", "dt", "em",
    "footer", "h1", "h2", "h3", "h4", "h5", "h6", "head", "header", "hr",
    "html", "i", "li", "main", "ol", "p", "pre", "s", "section", "small",
    "span", "strike", "strong", "style", "sub", "sup", "table", "tbody", "td",
    "tfoot", "th", "thead", "title", "tr", "u", "ul",
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
SAFE_ATTRIBUTES = {
    "*": {"style"},
    "a": {"href", "target", "title"},
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
_CLEAN_CONTENT_TAGS = REJECTED_HTML_TAGS | {"style", "title"}
_SAFE_FUNCTIONS = {"hsl", "hsla", "rgb", "rgba"}
_SAFE_DISPLAY_VALUES = {"block", "inline", "inline-block", "table", "table-cell", "table-row"}
_HTML_NAMESPACE = "http://www.w3.org/1999/xhtml"


def _local_name(element: Element) -> str | None:
    if not isinstance(element.tag, str):
        return None
    namespace, separator, name = element.tag[1:].partition("}") if element.tag.startswith("{") else ("", "", element.tag)
    if separator and namespace != _HTML_NAMESPACE:
        raise ValueError("unsupported draft HTML namespace")
    return name.casefold()


def _safe_href(value: str) -> str | None:
    normalized = value.strip("\t\n\r\f ")
    if not normalized or normalized.startswith("//"):
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        return None
    scheme, separator, _ = normalized.partition(":")
    if not separator or scheme.casefold() not in {"http", "https", "mailto"}:
        return None
    return normalized


def _attribute_filter(tag: str, attribute: str, value: str) -> str | None:
    if attribute == "href":
        return _safe_href(value)
    if attribute == "target":
        return "_blank" if value.casefold() == "_blank" else None
    return value


def _sanitize_inbound_style(value: str) -> str | None:
    safe: list[object] = []
    declarations = tinycss2.parse_declaration_list(value, skip_comments=True, skip_whitespace=True)
    for declaration in declarations:
        if declaration.type != "declaration" or declaration.lower_name not in SAFE_CSS_PROPERTIES:
            continue
        try:
            _validate_css_declaration(declaration)
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


def _validate_css_declarations(tokens: Iterable[object]) -> None:
    declarations = tinycss2.parse_declaration_list(tokens, skip_comments=True, skip_whitespace=True)
    for declaration in declarations:
        if declaration.type == "error":
            raise ValueError("invalid draft CSS declaration")
        if declaration.type != "declaration":
            raise ValueError("CSS at-rules are not supported")
        _validate_css_declaration(declaration)


def _validate_css_declaration(declaration: object) -> None:
    name = declaration.lower_name
    if name.startswith("--") or name not in SAFE_CSS_PROPERTIES:
        raise ValueError(f"unsupported draft CSS property: {name}")
    serialized = tinycss2.serialize(declaration.value)
    if len(serialized) > 4096:
        raise ValueError("draft CSS value is too large")
    for token in _iter_component_values(declaration.value):
        token_type = getattr(token, "type", "")
        if token_type == "error" or token_type == "url":
            raise ValueError("unsafe draft CSS value")
        if token_type == "function" and token.lower_name not in _SAFE_FUNCTIONS:
            raise ValueError(f"unsupported draft CSS function: {token.lower_name}")
    if name == "display" and serialized.strip().casefold() not in _SAFE_DISPLAY_VALUES:
        raise ValueError("unsupported draft CSS display value")


def _validate_stylesheet(value: str) -> None:
    rules = tinycss2.parse_stylesheet(value, skip_comments=True, skip_whitespace=True)
    for rule in rules:
        if rule.type == "error":
            raise ValueError("invalid draft stylesheet")
        if rule.type != "qualified-rule":
            raise ValueError("CSS at-rules are not supported")
        if not tinycss2.serialize(rule.prelude).strip():
            raise ValueError("empty draft CSS selector")
        _validate_css_declarations(rule.content)


def _validate_document(value: str) -> None:
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
            if name not in allowed:
                raise ValueError(f"unsupported draft HTML attribute: {name}")
            if name == "href" and _safe_href(attribute_value) is None:
                raise ValueError("unsafe draft HTML link")
            if name == "target" and attribute_value.casefold() != "_blank":
                raise ValueError("unsupported draft HTML link target")
            if name == "style":
                _validate_css_declarations(attribute_value)
        if tag == "style":
            _validate_stylesheet(element.text or "")


_PREPARE_CLEANER = nh3.Cleaner(
    tags=SAFE_HTML_TAGS,
    attributes=_PREPARE_ATTRIBUTES,
    clean_content_tags=REJECTED_HTML_TAGS,
    attribute_filter=_attribute_filter,
    filter_style_properties=SAFE_CSS_PROPERTIES,
    url_schemes={"http", "https", "mailto"},
    url_relative="deny",
    link_rel=None,
    strip_comments=True,
)
_OUTPUT_CLEANER = nh3.Cleaner(
    tags=OUTPUT_HTML_TAGS,
    attributes=SAFE_ATTRIBUTES,
    clean_content_tags=_CLEAN_CONTENT_TAGS,
    attribute_filter=_attribute_filter,
    filter_style_properties=SAFE_CSS_PROPERTIES,
    url_schemes={"http", "https", "mailto"},
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
    url_schemes={"http", "https", "mailto"},
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


def prepare_html(value: str, *, maximum_bytes: int) -> str:
    if len(value.encode("utf-8")) > maximum_bytes:
        raise ValueError("draft HTML body exceeds the 2 MB limit")
    _validate_document(value)
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
        _validate_css_declarations(body_style)
    prepared = _OUTPUT_CLEANER.clean(inlined)
    _validate_inline_styles(prepared)
    body_attribute = f' style="{escape(body_style, quote=True)}"' if body_style else ""
    result = f"<!DOCTYPE html><html><head></head><body{body_attribute}>{prepared}</body></html>"
    if len(result.encode("utf-8")) > maximum_bytes:
        raise ValueError("prepared draft HTML body exceeds the 2 MB limit")
    return result


def _validate_inline_styles(value: str) -> None:
    root = tinyhtml5.parse(value)
    for element in root.iter():
        style = element.attrib.get("style")
        if style is not None:
            _validate_css_declarations(style)


def sanitize_inbound_html(value: str, *, maximum_bytes: int) -> str:
    if len(value.encode("utf-8")) > maximum_bytes:
        raise ValueError("HTML body exceeds the 2 MB limit")
    result = _INBOUND_CLEANER.clean(value)
    _validate_inline_styles(result)
    if len(result.encode("utf-8")) > maximum_bytes:
        raise ValueError("HTML body exceeds the 2 MB limit")
    return result
