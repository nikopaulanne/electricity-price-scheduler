"""Home Assistant localization contract for the supported languages."""

import json
from pathlib import Path
from string import Formatter
from unittest import TestCase


_TRANSLATIONS = (
    Path(__file__).resolve().parents[1]
    / "custom_components" / "electricity_price_scheduler" / "translations"
)


def _messages(data, prefix=()):
    for key, value in data.items():
        if isinstance(value, dict):
            yield from _messages(value, (*prefix, key))
        else:
            yield (*prefix, key), value


def _placeholders(message):
    return {field for _, field, _, _ in Formatter().parse(message) if field is not None}


class TranslationTests(TestCase):
    def test_locales_cover_english_keys_with_matching_placeholders(self):
        english = dict(_messages(json.loads((_TRANSLATIONS / "en.json").read_text())))
        for language in ("fi", "sv", "de"):
            with self.subTest(language=language):
                translated = dict(_messages(json.loads(
                    (_TRANSLATIONS / f"{language}.json").read_text()
                )))
                self.assertEqual(translated.keys(), english.keys())
                for key, message in english.items():
                    self.assertTrue(translated[key].strip(), key)
                    self.assertEqual(_placeholders(translated[key]), _placeholders(message), key)
