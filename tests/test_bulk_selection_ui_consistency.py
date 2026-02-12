import re
import unittest
from pathlib import Path


class TestBulkSelectionUiConsistency(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parents[1]

    def _read(self, relative_path: str) -> str:
        return (self.repo_root / relative_path).read_text(encoding="utf-8")

    def test_bulk_selection_sections_include_primary_action(self) -> None:
        templates_dir = self.repo_root / "app" / "templates"
        pattern = re.compile(
            r'<div[^>]*class="[^"]*bulk-selection-actions[^"]*"[^>]*>(.*?)</div>',
            re.DOTALL,
        )

        for template_path in templates_dir.rglob("*.html"):
            html = template_path.read_text(encoding="utf-8")
            blocks = pattern.findall(html)
            for block in blocks:
                self.assertIn(
                    "<button",
                    block,
                    f"Template has a bulk selection state without an action button: {template_path}",
                )

    def test_bulk_selection_counts_are_live_regions(self) -> None:
        checks = [
            ("app/templates/community/list.html", "selectedCount"),
            ("app/templates/events/list.html", "selectedCount"),
            ("app/templates/unsubscribed/list.html", "selectedCount"),
            ("app/templates/scheduled/list.html", "pendingSelectedCount"),
            ("app/templates/scheduled/list.html", "pastSelectedCount"),
        ]
        pattern_template = (
            r'<span[^>]*role="status"[^>]*aria-live="polite"[^>]*aria-atomic="true"[^>]*>'
            r'\s*<span id="{count_id}">'
        )
        for relative_path, count_id in checks:
            html = self._read(relative_path)
            pattern = re.compile(pattern_template.format(count_id=re.escape(count_id)), re.DOTALL)
            self.assertRegex(
                html,
                pattern,
                f"Expected live-region selected count for {count_id} in {relative_path}",
            )

    def test_bulk_related_modals_have_aria_labelledby(self) -> None:
        checks = [
            ("app/templates/community/list.html", "deleteModal", "deleteModalTitle"),
            ("app/templates/community/list.html", "bulkDeleteModal", "bulkDeleteModalTitle"),
            ("app/templates/events/list.html", "deleteEventModal", "deleteEventModalTitle"),
            ("app/templates/events/list.html", "bulkDeleteModal", "bulkDeleteModalTitle"),
            ("app/templates/unsubscribed/list.html", "bulkDeleteModal", "bulkDeleteModalTitle"),
            ("app/templates/scheduled/list.html", "actionModal", "actionModalTitle"),
            ("app/templates/scheduled/list.html", "pendingBulkCancelModal", "pendingBulkCancelModalTitle"),
            ("app/templates/scheduled/list.html", "pastBulkDeleteModal", "pastBulkDeleteModalTitle"),
        ]
        for relative_path, modal_id, title_id in checks:
            html = self._read(relative_path)
            self.assertRegex(
                html,
                re.compile(
                    rf'<div class="modal fade" id="{re.escape(modal_id)}"[^>]*aria-labelledby="{re.escape(title_id)}"'
                ),
                f"Missing aria-labelledby for modal {modal_id} in {relative_path}",
            )
            self.assertRegex(
                html,
                re.compile(rf'<h5 class="modal-title" id="{re.escape(title_id)}"'),
                f"Missing modal title id {title_id} in {relative_path}",
            )

    def test_bulk_selection_actions_use_display_toggle_not_visibility_toggle(self) -> None:
        css = self._read("app/static/css/app.css")
        base_match = re.search(r"\.bulk-selection-actions\s*\{(?P<body>.*?)\}", css, re.DOTALL)
        self.assertIsNotNone(base_match, "Missing .bulk-selection-actions CSS rule")
        base_body = base_match.group("body")
        self.assertIn("display: none;", base_body)
        self.assertNotIn("opacity", base_body)
        self.assertNotIn("visibility", base_body)

        visible_match = re.search(r"\.bulk-selection-actions\.is-visible\s*\{(?P<body>.*?)\}", css, re.DOTALL)
        self.assertIsNotNone(visible_match, "Missing .bulk-selection-actions.is-visible CSS rule")
        self.assertIn("display: flex;", visible_match.group("body"))

    def test_unsubscribed_bulk_selection_counts_visible_checkboxes_only(self) -> None:
        html = self._read("app/templates/unsubscribed/list.html")
        self.assertIn("function getVisibleEntryCheckboxes()", html)
        self.assertIn("const visibleCheckboxes = getVisibleEntryCheckboxes();", html)
        self.assertIn("setCheckedForVisibleEntries(selectAllCheckbox.checked);", html)
        self.assertIn("setCheckedForVisibleEntries(true);", html)
        self.assertIn("setCheckedForVisibleEntries(false);", html)
        self.assertIn("window.addEventListener('resize', updateBulkUi);", html)
        self.assertNotIn("document.querySelectorAll('.entry-checkbox:checked')", html)


if __name__ == "__main__":
    unittest.main()
