import unittest

from channel_curator.name_normalizer import normalize_channel_name, parse_name_rules


class NameNormalizerTests(unittest.TestCase):
    def test_removes_only_trailing_separate_marker(self):
        self.assertEqual(
            normalize_channel_name("Sky Atlantic HD", ("HD",)),
            ("Sky Atlantic", "HD"),
        )
        self.assertEqual(
            normalize_channel_name("Sky HD News", ("HD",)),
            ("Sky HD News", ""),
        )
        self.assertEqual(
            normalize_channel_name("ChannelHD", ("HD",)),
            ("ChannelHD", ""),
        )

    def test_parentheses_and_dash_are_supported(self):
        self.assertEqual(
            normalize_channel_name("Sky Atlantic (HD)", ("HD",)),
            ("Sky Atlantic", "HD"),
        )
        self.assertEqual(
            normalize_channel_name("Sky Atlantic - HD", ("HD",)),
            ("Sky Atlantic", "HD"),
        )

    def test_rule_parser(self):
        rules, errors = parse_name_rules("UK Profile | HD, FHD")
        self.assertFalse(errors)
        self.assertEqual(rules[0]["suffixes"], ("FHD", "HD"))


if __name__ == "__main__":
    unittest.main()
