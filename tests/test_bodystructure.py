from __future__ import annotations

import pytest

from readndraft_imap_mcp.imap.bodystructure import BodyStructureError, parse_bodystructure
from readndraft_imap_mcp.imap.client import ImapClient, _response_bytes


def test_bodystructure_maps_preorder_part_ids_sections_and_encoded_attachment_size() -> None:
    structure = parse_bodystructure(
        b'1 (UID 7 BODYSTRUCTURE (("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "QUOTED-PRINTABLE" 12 1 NIL NIL NIL NIL) '
        b'("APPLICATION" "OCTET-STREAM" ("NAME" "a.txt") NIL NIL "BASE64" 16 NIL '
        b'("ATTACHMENT" ("FILENAME" "a.txt")) NIL NIL) "MIXED"))'
    )

    assert structure.part_id == "part-1"
    assert [part.part_id for part in structure.children] == ["part-2", "part-3"]
    assert [part.section for part in structure.children] == ["1", "2"]
    attachment = structure.children[1]
    assert attachment.is_attachment
    assert attachment.filename == "a.txt"
    assert attachment.encoded_size == 16
    assert structure.children[0].content_type_params == (("charset", "utf-8"),)


def test_bodystructure_maps_a_singlepart_root_to_imap_section_one() -> None:
    structure = parse_bodystructure(b'BODYSTRUCTURE ("TEXT" "PLAIN" NIL NIL NIL "7BIT" 4 1 NIL NIL NIL NIL)')

    assert structure.part_id == "part-1"
    assert structure.section == "1"


def test_bodystructure_accepts_escaped_quoted_values_and_literals() -> None:
    structure = parse_bodystructure(
        b'BODYSTRUCTURE ("APPLICATION" "OCTET-STREAM" ("NAME" {8}\r\nfile.txt) NIL NIL "BASE64" 16 NIL '
        b'("ATTACHMENT" ("FILENAME" "a\\\\b.txt")) NIL NIL)'
    )

    assert structure.filename == "a\\b.txt"
    assert structure.encoded_size == 16


def test_bodystructure_ignores_attribute_markers_in_quoted_and_literal_values() -> None:
    structure = parse_bodystructure(
        b'1 (UID 7 X "BODYSTRUCTURE (bad)" {25}\r\nBODYSTRUCTURE (also bad) '
        b'BODYSTRUCTURE ("TEXT" "PLAIN" NIL NIL NIL "7BIT" 4 1 NIL NIL NIL NIL) FLAGS ())'
    )
    assert structure.content_type == "text/plain"


def test_bodystructure_supports_rfc2231_continuations_and_extensions() -> None:
    structure = parse_bodystructure(
        b'BODYSTRUCTURE ("APPLICATION" "PDF" ("NAME*0*" "utf-8\'\'a%20" "NAME*1*" "file.pdf") '
        b'NIL NIL "BASE64" 12 NIL ("ATTACHMENT" NIL) ("en") "loc" "extension")'
    )
    assert structure.filename == "a file.pdf"
    assert structure.encoded_size == 12


def test_bodystructure_accepts_message_rfc822_outer_attachment_without_descending() -> None:
    structure = parse_bodystructure(
        b'BODYSTRUCTURE ("MESSAGE" "RFC822" ("NAME" "forwarded.eml") NIL NIL "7BIT" 123 '
        b'(NIL NIL NIL NIL NIL NIL NIL NIL NIL NIL) '
        b'("TEXT" "PLAIN" NIL NIL NIL "7BIT" 4 1 NIL NIL NIL NIL) 1 NIL '
        b'("ATTACHMENT" ("FILENAME" "forwarded.eml")) NIL NIL)'
    )

    assert structure.content_type == "message/rfc822"
    assert structure.section == "1"
    assert structure.filename == "forwarded.eml"
    assert structure.disposition == "attachment"
    assert structure.children == ()


@pytest.mark.parametrize(
    "raw",
    [
        b'BODYSTRUCTURE ("MESSAGE" "RFC822" NIL NIL NIL "7BIT" 1 NIL NIL 1 NIL NIL)',
        b'BODYSTRUCTURE ("MESSAGE" "RFC822" NIL NIL NIL "7BIT" 1 (NIL) '
        b'("TEXT" "PLAIN" NIL NIL NIL "7BIT" 1 1 NIL NIL NIL NIL) 1 NIL NIL)',
        b'BODYSTRUCTURE ("MESSAGE" "RFC822" NIL NIL NIL "7BIT" 1 '
        b'(NIL NIL NIL NIL NIL NIL NIL NIL NIL NIL) NIL 1 NIL NIL)',
        b'BODYSTRUCTURE ("MESSAGE" "RFC822" NIL NIL NIL "7BIT" 1 '
        b'(NIL NIL NIL NIL NIL NIL NIL NIL NIL NIL) '
        b'("TEXT" "PLAIN" NIL NIL NIL "7BIT" 1 1 NIL NIL NIL NIL) nope NIL NIL)',
    ],
)
def test_bodystructure_rejects_invalid_message_rfc822_fields(raw: bytes) -> None:
    with pytest.raises(BodyStructureError):
        parse_bodystructure(raw)


@pytest.mark.parametrize(
    "raw",
    [
        b'BODYSTRUCTURE ("TEXT" "PLAIN" NIL NIL NIL "7BIT" {x}\r\n4 1 NIL NIL NIL NIL)',
        b'BODYSTRUCTURE ("TEXT" "PLAIN" NIL NIL NIL "7BIT" {4}\r\n12',
        b'BODYSTRUCTURE ("TEXT" "PLAIN" NIL NIL NIL "7BIT" 4 1 NIL NIL NIL NIL',
        b'BODYSTRUCTURE ("TEXT" "PLAIN" NIL NIL NIL "7BIT" 4 1 NIL NIL NIL NIL) trailing',
    ],
)
def test_bodystructure_rejects_malformed_or_unclosed_values(raw: bytes) -> None:
    if raw.endswith(b"trailing"):
        assert parse_bodystructure(raw).content_type == "text/plain"
    else:
        with pytest.raises(BodyStructureError):
            parse_bodystructure(raw)


def test_bodystructure_enforces_depth_and_token_limits() -> None:
    too_deep = b"BODYSTRUCTURE " + b"(" * 65 + b"NIL" + b")" * 65
    too_many = b"BODYSTRUCTURE (" + b" ".join(b"NIL" for _ in range(10_001)) + b")"
    with pytest.raises(BodyStructureError):
        parse_bodystructure(too_deep)
    with pytest.raises(BodyStructureError):
        parse_bodystructure(too_many)


def test_imaplib_fragment_reassembly_preserves_literal_bytes_and_order() -> None:
    fragments = [
        (b"1 (UID 7 BODYSTRUCTURE (\"APPLICATION\" \"OCTET-STREAM\" (\"NAME\" {8}\r\n", b"file.txt"),
        b") NIL NIL \"BASE64\" 16 NIL (\"ATTACHMENT\" NIL) NIL NIL) FLAGS ())",
    ]
    raw = _response_bytes(fragments)
    assert b"{8}\r\nfile.txt)" in raw
    assert parse_bodystructure(raw).filename == "file.txt"


def test_related_start_uses_normalized_content_id_and_only_root() -> None:
    root = parse_bodystructure(
        b'BODYSTRUCTURE (("TEXT" "HTML" NIL "<root>" NIL "7BIT" 4 1 NIL NIL NIL NIL) '
        b'("TEXT" "PLAIN" NIL "<other>" NIL "7BIT" 4 1 NIL NIL NIL NIL) "RELATED" '
        b'("START" "root"))'
    )
    plain, html, attachments = ImapClient._body_candidates(root)
    assert not plain and not attachments
    assert [part.section for part in html] == ["1"]
