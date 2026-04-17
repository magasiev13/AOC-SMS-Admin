from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_CSS = ROOT / "app" / "static" / "css" / "app.css"
UI_SYSTEM_CSS = ROOT / "app" / "static" / "css" / "ui-system.css"
BASE_TEMPLATE = ROOT / "app" / "templates" / "base.html"
AUTH_BASE_TEMPLATE = ROOT / "app" / "templates" / "auth" / "base.html"
UI_MACROS_TEMPLATE = ROOT / "app" / "templates" / "includes" / "ui_macros.html"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _exact_block_count(css: str, selector: str) -> int:
    pattern = re.compile(rf"(?m)^{re.escape(selector)}\s*\{{")
    return len(pattern.findall(css))


class TestUiSystemCss(unittest.TestCase):
    def test_protected_shared_selectors_have_single_exact_definition(self) -> None:
        app_css = _read(APP_CSS)
        ui_css = _read(UI_SYSTEM_CSS)
        protected_selectors = [
            ".app-topbar",
            ".app-mobile-nav-open .app-topbar",
            ".app-card",
            ".platform-shell",
            ".platform-shell__summary-title",
            ".auth-surface",
        ]

        for selector in protected_selectors:
            total_exact_blocks = _exact_block_count(app_css, selector) + _exact_block_count(ui_css, selector)
            self.assertEqual(
                total_exact_blocks,
                1,
                f"{selector} should have exactly one exact selector block across shared CSS files",
            )

    def test_ui_system_css_owns_workspace_and_collection_primitives(self) -> None:
        app_css = _read(APP_CSS)
        ui_css = _read(UI_SYSTEM_CSS)
        ui_system_selectors = [
            ".workspace-shell",
            ".workspace-summary",
            ".workspace-command-layout",
            ".workspace-detail-layout",
            ".workspace-form-layout",
            ".workspace-panel",
            ".workspace-panel__footer",
            ".workspace-callout",
            ".collection-shell",
            ".collection-panel",
            ".collection-filters-form",
        ]

        for selector in ui_system_selectors:
            self.assertEqual(
                _exact_block_count(app_css, selector),
                0,
                f"{selector} should not be defined in app.css",
            )
            self.assertEqual(
                _exact_block_count(ui_css, selector),
                1,
                f"{selector} should have exactly one exact selector block in ui-system.css",
            )

    def test_base_templates_load_ui_system_bundle(self) -> None:
        expected_fragment = "css/ui-system.css"
        self.assertIn(expected_fragment, _read(BASE_TEMPLATE))
        self.assertIn(expected_fragment, _read(AUTH_BASE_TEMPLATE))

    def test_ui_macros_include_collection_filters_panel(self) -> None:
        macros = _read(UI_MACROS_TEMPLATE)
        self.assertIn("macro collection_filters_panel", macros)
        self.assertIn("workspace-summary__stats--", macros)


if __name__ == "__main__":
    unittest.main()
