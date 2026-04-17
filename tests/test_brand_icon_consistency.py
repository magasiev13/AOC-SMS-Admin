import re
import unittest
from pathlib import Path


class TestBrandIconConsistency(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parents[1]

    def _read(self, relative_path: str) -> str:
        return (self.repo_root / relative_path).read_text(encoding="utf-8")

    def test_favicon_uses_same_airplane_path_as_shared_brand_mark(self) -> None:
        brand_mark = self._read("app/static/brand-mark.svg")
        favicon = self._read("app/static/favicon.svg")
        path_match = re.search(r'd="([^"]+)"', brand_mark)
        self.assertIsNotNone(path_match, "Shared brand mark is missing its path data")
        plane_path = path_match.group(1)

        self.assertIn(plane_path, favicon)
        self.assertIn('fill="#1e293b"', favicon)

    def test_shared_brand_surfaces_use_svg_mark_instead_of_font_icon(self) -> None:
        templates = [
            "app/templates/base.html",
            "app/templates/auth/login.html",
            "app/templates/auth/signup.html",
            "app/templates/auth/accept_invitation.html",
            "app/templates/auth/change_password.html",
            "app/templates/auth/security_contact.html",
        ]
        for relative_path in templates:
            template = self._read(relative_path)
            self.assertIn("brand-mark.svg", template, f"Expected shared brand mark in {relative_path}")
            self.assertNotIn("brand-icon__pulse", template, f"Legacy brand pulse should be removed in {relative_path}")
            self.assertNotIn("bi bi-send-fill", template, f"Legacy font icon should be removed in {relative_path}")

    def test_brand_icon_css_targets_svg_geometry(self) -> None:
        css = self._read("app/static/css/app.css")
        self.assertIn(".app-sidebar-brand:hover .brand-icon__image", css)
        self.assertIn(".brand-icon__image {\n  width: 18px;", css)
        self.assertIn(".brand-icon--sm .brand-icon__image", css)
        self.assertNotIn(".brand-icon i {", css)
        self.assertNotIn(".brand-icon--sm i {", css)


if __name__ == "__main__":
    unittest.main()
