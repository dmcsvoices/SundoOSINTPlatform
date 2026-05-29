"""
Tests for sundo/ingest/author_extractor.py
"""

import unittest
from sundo.ingest.author_extractor import (
    extract_author, normalize_author, make_author_id, make_handle, detect_language
)


class TestNormalizeAuthor(unittest.TestCase):

    def test_strips_by_prefix(self):
        result = normalize_author("By Bilal Shbair")
        self.assertEqual(result['display_name'], "Bilal Shbair")

    def test_strips_by_prefix_lowercase(self):
        result = normalize_author("by bilal shbair")
        self.assertIsNotNone(result)

    def test_email_byline_converted(self):
        result = normalize_author("bilal.shbair@972mag.com")
        self.assertIsNotNone(result)
        self.assertNotIn('@', result['display_name'])

    def test_generic_staff_discarded(self):
        self.assertIsNone(normalize_author("Staff Writer"))
        self.assertIsNone(normalize_author("staff"))
        self.assertIsNone(normalize_author("The Editors"))

    def test_wire_services_discarded(self):
        self.assertIsNone(normalize_author("Reuters"))
        self.assertIsNone(normalize_author("AP"))
        self.assertIsNone(normalize_author("Associated Press"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(normalize_author(""))
        self.assertIsNone(normalize_author(" "))

    def test_single_char_returns_none(self):
        self.assertIsNone(normalize_author("X"))

    def test_arabic_name_preserved(self):
        result = normalize_author("محمد الشيخ")
        self.assertIsNotNone(result)
        self.assertEqual(result['display_name'], "محمد الشيخ")

    def test_byline_variant_stored(self):
        result = normalize_author("Bilal Shbair")
        self.assertIn("Bilal Shbair", result['byline_variants'])


class TestMakeAuthorId(unittest.TestCase):

    def test_slug_format(self):
        author_id = make_author_id("Bilal Shbair")
        self.assertRegex(author_id, r'^[a-z0-9\-]+-[a-f0-9]{4}$')

    def test_special_chars_removed(self):
        author_id = make_author_id("O'Brien & Sons")
        self.assertNotIn("'", author_id)
        self.assertNotIn("&", author_id)

    def test_same_name_same_id(self):
        self.assertEqual(
            make_author_id("Bilal Shbair"),
            make_author_id("Bilal Shbair")
        )

    def test_different_names_different_ids(self):
        self.assertNotEqual(
            make_author_id("Bilal Shbair"),
            make_author_id("Mohammed Al-Sheikh")
        )


class TestMakeHandle(unittest.TestCase):

    def test_spaces_removed(self):
        self.assertEqual(make_handle("Bilal Shbair"), "bilalshbair")

    def test_lowercase(self):
        self.assertEqual(make_handle("BILAL"), "bilal")

    def test_special_chars_removed(self):
        self.assertEqual(make_handle("O'Brien"), "obrien")


class TestExtractAuthor(unittest.TestCase):

    def _make_entry(self, **kwargs):
        """Create a minimal feedparser-like entry object."""
        class Entry:
            pass
        e = Entry()
        for k, v in kwargs.items():
            setattr(e, k, v)
        return e

    def test_extracts_from_author_field(self):
        entry = self._make_entry(author="Bilal Shbair")
        result = extract_author(entry)
        self.assertIsNotNone(result)
        self.assertEqual(result['display_name'], "Bilal Shbair")

    def test_returns_none_when_no_author(self):
        entry = self._make_entry()
        result = extract_author(entry)
        self.assertIsNone(result)

    def test_returns_none_for_generic_author(self):
        entry = self._make_entry(author="Staff Writer")
        result = extract_author(entry)
        self.assertIsNone(result)


class TestDetectLanguage(unittest.TestCase):

    def test_english_default(self):
        self.assertEqual(detect_language("Hello world"), "en")

    def test_arabic_detected(self):
        self.assertEqual(detect_language("مرحبا بالعالم"), "ar")

    def test_hebrew_detected(self):
        self.assertEqual(detect_language("שלום עולם"), "he")

    def test_empty_returns_en(self):
        self.assertEqual(detect_language(""), "en")


if __name__ == '__main__':
    unittest.main()
