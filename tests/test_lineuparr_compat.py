import os
import tempfile
import unittest

from channel_curator.lineuparr_compat import (
    build_normalized_lineup,
    derive_output_name,
    discover_lineup_files,
    discover_lineup_options,
)


class LineuparrCompatibilityTests(unittest.TestCase):
    def test_discovers_only_lineuparr_lineup_files(self):
        with tempfile.TemporaryDirectory() as directory:
            for filename in (
                "UK_SkyTV_ENG_full_lineup.json",
                "US_Combined_lineup.json",
                "plugin.json",
                "notes.txt",
            ):
                open(os.path.join(directory, filename), "w", encoding="utf-8").close()
            self.assertEqual(
                discover_lineup_files(directory),
                ["UK_SkyTV_ENG_full_lineup.json", "US_Combined_lineup.json"],
            )

    def test_lineup_options_use_package_as_friendly_label(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "UK_SkyTV_ENG_full_lineup.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"package": "Sky TV UK Full"}')
            self.assertEqual(
                discover_lineup_options(directory),
                [{
                    "value": "UK_SkyTV_ENG_full_lineup.json",
                    "label": "Sky TV UK Full — UK_SkyTV_ENG_full_lineup.json",
                }],
            )

    def test_output_name_is_automatic(self):
        self.assertEqual(
            derive_output_name("UK_SkyTV_ENG_full_lineup.json"),
            "UK_SkyTV_ENG_full_tidych_lineup.json",
        )

    def test_clean_name_and_original_alias(self):
        source = {
            "categories": {
                "Entertainment": [
                    {"name": "Sky Atlantic HD", "number": 101}
                ]
            }
        }
        output, changes, collisions = build_normalized_lineup(source, ("HD",))
        entry = output["categories"]["Entertainment"][0]
        self.assertEqual(entry["name"], "Sky Atlantic")
        self.assertEqual(entry["aliases"], ["Sky Atlantic HD"])
        self.assertEqual(len(changes), 1)
        self.assertFalse(collisions)
        self.assertEqual(source["categories"]["Entertainment"][0]["name"], "Sky Atlantic HD")

    def test_collision_is_reported(self):
        source = {
            "categories": {
                "Entertainment": [
                    {"name": "Sky Atlantic"},
                    {"name": "Sky Atlantic HD"},
                ]
            }
        }
        _, changes, collisions = build_normalized_lineup(source, ("HD",))
        self.assertFalse(changes)
        self.assertEqual(len(collisions), 2)


if __name__ == "__main__":
    unittest.main()
