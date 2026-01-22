import unittest

from app.sort_utils import normalize_sort_params


class TestNormalizeSortParams(unittest.TestCase):
    def test_defaults_to_known_key_and_direction(self) -> None:
        key, direction = normalize_sort_params(
            None,
            None,
            allowed_keys={"name", "created_at"},
            default_key="created_at",
            default_dir="desc",
        )

        self.assertEqual(key, "created_at")
        self.assertEqual(direction, "desc")

    def test_rejects_unknown_key(self) -> None:
        key, direction = normalize_sort_params(
            "bogus",
            "asc",
            allowed_keys={"name", "created_at"},
            default_key="name",
        )

        self.assertEqual(key, "name")
        self.assertEqual(direction, "asc")

    def test_normalizes_direction(self) -> None:
        key, direction = normalize_sort_params(
            "name",
            "DESC",
            allowed_keys={"name"},
            default_key="name",
        )

        self.assertEqual(key, "name")
        self.assertEqual(direction, "desc")

    def test_invalid_direction_falls_back(self) -> None:
        key, direction = normalize_sort_params(
            "name",
            "sideways",
            allowed_keys={"name"},
            default_key="name",
            default_dir="asc",
        )

        self.assertEqual(key, "name")
        self.assertEqual(direction, "asc")


if __name__ == "__main__":
    unittest.main()
