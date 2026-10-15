import unittest

from logo_matcher import match_channel, normalize_name


class LogoMatcherTests(unittest.TestCase):
    def test_exclamation_mark_distinguishes_channel_names(self):
        filenames = ["movies-us.png", "movies!-us.png"]

        self.assertEqual(normalize_name("Movies", "us"), "movies")
        self.assertEqual(
            normalize_name("Movies!", "us"),
            "movies exclamation",
        )
        self.assertEqual(
            match_channel("Movies", filenames, "us", 90),
            ("movies-us.png", 100.0),
        )
        self.assertEqual(
            match_channel("Movies!", filenames, "us", 90),
            ("movies!-us.png", 100.0),
        )


if __name__ == "__main__":
    unittest.main()
