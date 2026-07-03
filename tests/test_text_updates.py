import pytest

from backend.text_patch import try_normalized_patch
from frontend.cli import _resolve_text


def test_resolve_text_appends_to_empty_and_existing_fields():
    assert _resolve_text(
        old=None,
        old_file=None,
        new=None,
        new_file=None,
        append="first",
        append_file=None,
        field_name="content",
        current_value=None,
    ) == "first"

    assert _resolve_text(
        old=None,
        old_file=None,
        new=None,
        new_file=None,
        append="second",
        append_file=None,
        field_name="content",
        current_value="first",
    ) == "first\nsecond"


def test_resolve_text_rejects_mixed_patch_and_append_modes():
    with pytest.raises(ValueError, match="choose one mode"):
        _resolve_text(
            old="old",
            old_file=None,
            new="new",
            new_file=None,
            append="extra",
            append_file=None,
            field_name="content",
            current_value="old",
        )


def test_resolve_text_requires_patch_target_to_be_unique():
    with pytest.raises(ValueError, match="matched 2 times"):
        _resolve_text(
            old="repeat",
            old_file=None,
            new="replace",
            new_file=None,
            append=None,
            append_file=None,
            field_name="content",
            current_value="repeat and repeat",
        )


def test_resolve_text_converts_literal_newlines_for_patch_mode():
    result = _resolve_text(
        old="line one\\nline two",
        old_file=None,
        new="line one\\nchanged",
        new_file=None,
        append=None,
        append_file=None,
        field_name="content",
        current_value="before\nline one\nline two\nafter",
    )

    assert result == "before\nline one\nchanged\nafter"


def test_resolve_text_reads_patch_content_from_files(tmp_path):
    old_file = tmp_path / "old.txt"
    new_file = tmp_path / "new.txt"
    old_file.write_text("old block\n", encoding="utf-8")
    new_file.write_text("new block\n", encoding="utf-8")

    result = _resolve_text(
        old=None,
        old_file=str(old_file),
        new=None,
        new_file=str(new_file),
        append=None,
        append_file=None,
        field_name="content",
        current_value="before\nold block\nafter",
    )

    assert result == "before\nnew block\nafter"


def test_resolve_text_reads_append_content_from_file(tmp_path):
    append_file = tmp_path / "append.txt"
    append_file.write_text("from file\n", encoding="utf-8")

    result = _resolve_text(
        old=None,
        old_file=None,
        new=None,
        new_file=None,
        append=None,
        append_file=str(append_file),
        field_name="content",
        current_value="before",
    )

    assert result == "before\nfrom file"


def test_resolve_text_rejects_string_and_file_for_same_patch_side(tmp_path):
    old_file = tmp_path / "old.txt"
    old_file.write_text("old", encoding="utf-8")

    with pytest.raises(ValueError, match="Cannot use both --old and --old-file"):
        _resolve_text(
            old="old",
            old_file=str(old_file),
            new="new",
            new_file=None,
            append=None,
            append_file=None,
            field_name="content",
            current_value="old",
        )


def test_normalized_patch_handles_quote_dash_and_space_drift():
    content = 'Use “quoted” text — then  two spaces.'

    result = try_normalized_patch(
        content,
        'Use "quoted" text - then two spaces.',
        "Use plain text.",
    )

    assert result == "Use plain text."


def test_normalized_patch_preserves_original_newline_style():
    content = "alpha\r\nbeta  gamma\r\nomega"

    result = try_normalized_patch(
        content,
        "beta gamma",
        "beta\nchanged",
    )

    assert result == "alpha\r\nbeta\r\nchanged\r\nomega"


def test_normalized_patch_rejects_ambiguous_matches():
    content = "same “quote”\nother\nsame \"quote\""

    assert try_normalized_patch(content, 'same "quote"', "changed") is None
