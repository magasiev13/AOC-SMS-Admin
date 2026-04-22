"""Unit tests for app.utils.

Run with: python -m unittest tests.test_utils
"""

import unittest
from datetime import datetime, timedelta, timezone

from app.utils import (
    analyze_personalized_sms_blast,
    analyze_sms_body,
    as_utc_datetime,
    escape_like,
    find_invalid_template_tokens,
    is_safe_url,
    normalize_keyword,
    normalize_phone,
    normalize_sms_body,
    parse_recipients_csv,
    parse_phones_csv,
    phone_lookup_variants,
    render_message_template,
    sanitize_csv_cell,
    validate_phone,
)


class TestNormalizePhone(unittest.TestCase):
    def test_us_number_without_country_code(self) -> None:
        self.assertEqual(normalize_phone("720-383-2388"), "+17203832388")

    def test_us_number_with_country_code(self) -> None:
        self.assertEqual(normalize_phone("+1 (720) 383-2388"), "+17203832388")

    def test_number_with_punctuation(self) -> None:
        self.assertEqual(normalize_phone("(310) 555-1212"), "+13105551212")

    def test_non_numeric_input_returns_empty(self) -> None:
        self.assertEqual(normalize_phone("foo"), "")

    def test_ascii_letters_in_phone_are_rejected(self) -> None:
        self.assertEqual(normalize_phone("+1 (415) 555-2671 ext 9"), "")
        self.assertEqual(normalize_phone("+1 415 555 FLOW"), "")

    def test_multiple_plus_prefixes_are_normalized(self) -> None:
        self.assertEqual(normalize_phone("++++"), "")

    def test_numeric_input_is_supported(self) -> None:
        self.assertEqual(normalize_phone(17203832388), "+17203832388")

    def test_non_ascii_digits_are_rejected(self) -> None:
        full_width_digits = "\uff11\uff12\uff13\uff14\uff15\uff16\uff17\uff18\uff19\uff10"
        arabic_indic_digits = "\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669\u0660"

        self.assertEqual(normalize_phone(full_width_digits), "")
        self.assertEqual(normalize_phone(arabic_indic_digits), "")


class TestValidatePhone(unittest.TestCase):
    def test_valid_e164(self) -> None:
        self.assertTrue(validate_phone("+14155552671"))

    def test_invalid_short(self) -> None:
        self.assertFalse(validate_phone("12345"))

    def test_invalid_long(self) -> None:
        self.assertFalse(validate_phone("+1234567890123456"))

    def test_empty(self) -> None:
        self.assertFalse(validate_phone(""))


class TestEscapeLike(unittest.TestCase):
    def test_escapes_backslash_and_wildcards(self) -> None:
        value = "foo\\bar%_"
        self.assertEqual(escape_like(value), "foo\\\\bar\\%\\_")


class TestIsSafeUrl(unittest.TestCase):
    def test_accepts_same_origin_relative_url(self) -> None:
        self.assertTrue(is_safe_url("/dashboard", "https://example.com/"))

    def test_accepts_same_origin_absolute_url(self) -> None:
        self.assertTrue(is_safe_url("https://example.com/account", "https://example.com/"))

    def test_rejects_cross_origin_url(self) -> None:
        self.assertFalse(is_safe_url("https://evil.example/login", "https://example.com/"))

    def test_rejects_non_http_scheme(self) -> None:
        self.assertFalse(is_safe_url("javascript:alert(1)", "https://example.com/"))


class TestAsUtcDatetime(unittest.TestCase):
    def test_none_stays_none(self) -> None:
        self.assertIsNone(as_utc_datetime(None))

    def test_naive_datetime_is_marked_utc(self) -> None:
        value = datetime(2026, 4, 1, 9, 30, 0)
        result = as_utc_datetime(value)
        self.assertEqual(result, datetime(2026, 4, 1, 9, 30, 0, tzinfo=timezone.utc))

    def test_aware_datetime_is_converted_to_utc(self) -> None:
        value = datetime(2026, 4, 1, 9, 30, 0, tzinfo=timezone(timedelta(hours=-6)))
        result = as_utc_datetime(value)
        self.assertEqual(result, datetime(2026, 4, 1, 15, 30, 0, tzinfo=timezone.utc))


class TestSanitizeCsvCell(unittest.TestCase):
    def test_none_becomes_empty_string(self) -> None:
        self.assertEqual(sanitize_csv_cell(None), "")

    def test_formula_prefixes_are_escaped(self) -> None:
        self.assertEqual(sanitize_csv_cell("=SUM(A1:A2)"), "'=SUM(A1:A2)")
        self.assertEqual(sanitize_csv_cell("+12345"), "'+12345")
        self.assertEqual(sanitize_csv_cell("-10"), "'-10")
        self.assertEqual(sanitize_csv_cell("@cmd"), "'@cmd")

    def test_control_character_prefixes_are_escaped(self) -> None:
        self.assertEqual(sanitize_csv_cell("\t=1+1"), "'\t=1+1")
        self.assertEqual(sanitize_csv_cell("\nhello"), "'\nhello")
        self.assertEqual(sanitize_csv_cell("\rhello"), "'\rhello")

    def test_leading_whitespace_before_formula_is_escaped(self) -> None:
        self.assertEqual(sanitize_csv_cell("  =SUM(A1:A2)"), "'  =SUM(A1:A2)")
        self.assertEqual(sanitize_csv_cell(" \t-10"), "' \t-10")

    def test_regular_values_are_not_changed(self) -> None:
        self.assertEqual(sanitize_csv_cell("hello"), "hello")
        self.assertEqual(sanitize_csv_cell("12345"), "12345")


class TestNormalizeKeyword(unittest.TestCase):
    def test_normalizes_case_and_whitespace(self) -> None:
        self.assertEqual(normalize_keyword("  join   now "), "JOIN NOW")

    def test_empty_input_stays_empty(self) -> None:
        self.assertEqual(normalize_keyword("   "), "")


class TestSmsBodyAnalysis(unittest.TestCase):
    def test_normalize_sms_body_rewrites_deterministic_high_cost_characters(self) -> None:
        raw = '“Hello” — world…\u00a0•'
        self.assertEqual(normalize_sms_body(raw), '"Hello" - world... -')

    def test_analyze_sms_body_uses_gsm7_extended_character_units(self) -> None:
        analysis = analyze_sms_body(("a" * 159) + "^", apply_normalization=False)

        self.assertEqual(analysis["encoding"], "gsm-7")
        self.assertEqual(analysis["characters_used"], 161)
        self.assertEqual(analysis["segment_count"], 2)

    def test_analyze_sms_body_normalization_can_save_segments(self) -> None:
        analysis = analyze_sms_body("—" * 71)

        self.assertEqual(analysis["original_encoding"], "ucs-2")
        self.assertEqual(analysis["original_segment_count"], 2)
        self.assertEqual(analysis["encoding"], "gsm-7")
        self.assertEqual(analysis["segment_count"], 1)
        self.assertEqual(analysis["segments_saved"], 1)

    def test_analyze_sms_body_counts_stop_footer(self) -> None:
        analysis = analyze_sms_body("Hello\n\nReply STOP to unsubscribe.", apply_normalization=False)

        self.assertEqual(analysis["encoding"], "gsm-7")
        self.assertEqual(analysis["segment_count"], 1)
        self.assertGreater(analysis["characters_used"], len("Hello"))

    def test_analyze_personalized_sms_blast_tracks_segment_variance(self) -> None:
        analysis = analyze_personalized_sms_blast(
            "Hello {first_name}, " + ("A" * 145),
            [
                {"name": "Al", "phone": "+15550000001"},
                {"name": "Alexandria Cassandra", "phone": "+15550000002"},
            ],
        )

        self.assertEqual(analysis["unique_recipients"], 2)
        self.assertEqual(analysis["min_segment_count"], 1)
        self.assertEqual(analysis["max_segment_count"], 2)
        self.assertEqual(analysis["total_segments"], 3)


class TestParseRecipientsCsv(unittest.TestCase):
    def test_single_column_phone_only(self) -> None:
        content = "720-383-2388\n\n123\n"
        self.assertEqual(
            parse_recipients_csv(content),
            [{"name": None, "phone": "+17203832388"}],
        )

    def test_two_column_name_phone_and_phone_name(self) -> None:
        content = "Name,Phone\nAlice,720-383-2388\n720-555-1212,Bob\nNope,StillNo\n"
        self.assertEqual(
            parse_recipients_csv(content),
            [
                {"name": "Alice", "phone": "+17203832388"},
                {"name": "Bob", "phone": "+17205551212"},
            ],
        )

    def test_three_column_first_last_phone_with_header(self) -> None:
        content = "First,Last,Phone\nVardan,Hovsepyan,(323) 630-0201\n,,\nBad,Data,123\n"
        self.assertEqual(
            parse_recipients_csv(content),
            [{"name": "Vardan Hovsepyan", "phone": "+13236300201"}],
        )


class TestParsePhonesCsv(unittest.TestCase):
    def test_multiple_numbers_per_row_mixed_formatting(self) -> None:
        content = "720-383-2388,(310) 555-1212\ninvalid,123\n\n+1 415 555 2671\n"
        self.assertEqual(
            parse_phones_csv(content),
            ["+17203832388", "+13105551212", "+14155552671"],
        )


class TestPhoneLookupVariants(unittest.TestCase):
    def test_eleven_digit_number_includes_ten_digit_variant(self) -> None:
        self.assertEqual(phone_lookup_variants("+17205550102"), ["17205550102", "7205550102"])

    def test_ten_digit_number_includes_us_country_code_variant(self) -> None:
        self.assertEqual(phone_lookup_variants("720-555-0102"), ["17205550102", "7205550102"])

    def test_invalid_input_returns_no_variants(self) -> None:
        self.assertEqual(phone_lookup_variants("not-a-phone"), [])


class TestRenderMessageTemplate(unittest.TestCase):
    def test_first_name_placeholder(self) -> None:
        template = "Hello {first_name}, thanks!"
        recipient = {"name": "Michael Jordan"}
        self.assertEqual(
            render_message_template(template, recipient),
            "Hello Michael, thanks!",
        )

    def test_name_placeholder(self) -> None:
        template = "Hello {name}, welcome!"
        recipient = {"name": "John Doe"}
        self.assertEqual(
            render_message_template(template, recipient),
            "Hello John Doe, welcome!",
        )

    def test_full_name_placeholder(self) -> None:
        template = "Hello {full_name}, welcome!"
        recipient = {"name": "John Doe"}
        self.assertEqual(
            render_message_template(template, recipient),
            "Hello John Doe, welcome!",
        )

    def test_missing_name_uses_fallback(self) -> None:
        template = "Hello {first_name}!"
        recipient = {"phone": "+15551234567"}
        self.assertEqual(
            render_message_template(template, recipient),
            "Hello there!",
        )


class TestTemplateTokenValidation(unittest.TestCase):
    def test_invalid_tokens(self) -> None:
        template = "Hello {first name}, {lastname}!"
        self.assertEqual(
            find_invalid_template_tokens(template),
            ["{first name}", "{lastname}"],
        )

    def test_valid_tokens(self) -> None:
        template = "Hello {first_name} {full_name} {name}!"
        self.assertEqual(find_invalid_template_tokens(template), [])


if __name__ == "__main__":
    unittest.main()
