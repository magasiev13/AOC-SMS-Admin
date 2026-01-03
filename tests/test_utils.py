"""Unit tests for app.utils.

Run with: python -m unittest tests.test_utils
"""

import unittest

from app.utils import (
    normalize_phone,
    validate_phone,
    parse_recipients_csv,
    parse_phones_csv,
)


class TestNormalizePhone(unittest.TestCase):
    def test_us_number_without_country_code(self) -> None:
        self.assertEqual(normalize_phone("720-383-2388"), "+17203832388")

    def test_us_number_with_country_code(self) -> None:
        self.assertEqual(normalize_phone("+1 (720) 383-2388"), "+17203832388")

    def test_number_with_punctuation(self) -> None:
        self.assertEqual(normalize_phone("(310) 555-1212"), "+13105551212")


class TestValidatePhone(unittest.TestCase):
    def test_valid_e164(self) -> None:
        self.assertTrue(validate_phone("+14155552671"))

    def test_invalid_short(self) -> None:
        self.assertFalse(validate_phone("12345"))

    def test_invalid_long(self) -> None:
        self.assertFalse(validate_phone("+1234567890123456"))

    def test_empty(self) -> None:
        self.assertFalse(validate_phone(""))


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


if __name__ == "__main__":
    unittest.main()
