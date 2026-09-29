import pytest

from contact_form_backend.config import FieldSpec
from contact_form_backend.validation import clean_text, normalize_email, valid_phone, validate


@pytest.mark.parametrize(
    "value, expected",
    [
        ("dana@example.ca", "dana@example.ca"),
        ("Dana.LeBlanc+quote@Example.CA", "Dana.LeBlanc+quote@example.ca"),
        ("h\u00e9l\u00e8ne@example.ca", None),
        ("helene@caf\u00e9.example", "helene@xn--caf-dma.example"),
        ("a@b", None),
        ("a@b.c0m", "a@b.c0m"),
        ("a@1.2.3.4", None),
        ("no-at-sign.ca", None),
        ("two@@example.ca", None),
        (".dot@example.ca", None),
        ("a..b@example.ca", None),
        ("Dana <dana@example.ca>", None),
        ("dana@example.ca\r\nBcc: x@example.com", None),
        ("dana @example.ca", None),
        ("a@-bad.ca", None),
        ("x" * 65 + "@example.ca", None),
    ],
)
def test_normalize_email(value, expected):
    assert normalize_email(value) == expected


@pytest.mark.parametrize(
    "value, ok",
    [
        ("(506) 555-0142", True),
        ("+1 506 555 0142", True),
        ("506.555.0142 ext. 12", True),
        ("506-555-0142 x3", True),
        ("555-01", False),
        ("call me maybe", False),
        ("+1 (506) 555-0142-0142-0142-0142", False),
        ("506<script>", False),
    ],
)
def test_valid_phone(value, ok):
    assert valid_phone(value) is ok


def test_clean_text_strips_controls_and_normalizes_newlines():
    assert clean_text("  a\r\nb\x00c\u202e ", multiline=True) == "a\nbc"
    assert clean_text("a\r\n  b\tc", multiline=False) == "a b c"


def test_validate_mixed():
    fields = (
        FieldSpec("name", required=True, max_length=5),
        FieldSpec("email", type="email", required=True),
        FieldSpec("phone", type="phone"),
        FieldSpec("message", type="textarea", required=True),
    )
    clean, errors = validate(fields, {"name": "Toolong", "email": " A@Example.CA ", "phone": "",
                                      "message": "   ", "other": "ignored"})
    assert clean == {"email": "A@example.ca"}
    assert errors == {"name": "too_long", "message": "required"}


def test_default_max_lengths():
    assert FieldSpec("x").limit == 200
    assert FieldSpec("x", type="textarea").limit == 5000
    _, errors = validate((FieldSpec("x"),), {"x": "a" * 201})
    assert errors == {"x": "too_long"}


def test_non_string_values_are_treated_as_missing():
    _, errors = validate((FieldSpec("x", required=True),), {"x": 5})
    assert errors == {"x": "required"}
