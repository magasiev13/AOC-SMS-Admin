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

    def test_dashboard_send_section_precedes_stats_and_charts(self) -> None:
        html = self._read("app/templates/dashboard.html")
        send_index = html.find("dashboard-section-send")
        stats_index = html.find("dashboard-section-stats")
        charts_index = html.find("dashboard-section-charts")

        self.assertNotEqual(send_index, -1, "Dashboard send section is missing")
        self.assertNotEqual(stats_index, -1, "Dashboard stats section is missing")
        self.assertNotEqual(charts_index, -1, "Dashboard charts section is missing")
        self.assertLess(send_index, stats_index, "Send section should appear before stats")
        self.assertLess(send_index, charts_index, "Send section should appear before charts")

    def test_dashboard_breadcrumb_uses_static_overview_label(self) -> None:
        html = self._read("app/templates/dashboard.html")
        match = re.search(r"\{% block breadcrumbs %\}(.*?)\{% endblock %\}", html, re.DOTALL)
        self.assertIsNotNone(match, "Dashboard breadcrumb block is missing")
        breadcrumb_block = match.group(1)
        self.assertIn("<span>Overview</span>", breadcrumb_block)
        self.assertNotIn('url_for(\'main.dashboard\')', breadcrumb_block)

    def test_multi_action_page_headers_define_one_primary_action(self) -> None:
        templates_dir = self.repo_root / "app" / "templates"
        block_pattern = re.compile(r"\{% block page_actions %\}(.*?)\{% endblock %\}", re.DOTALL)
        action_pattern = re.compile(r"^\s*<(a|button|form)\b", re.MULTILINE)

        for template_path in templates_dir.rglob("*.html"):
            html = template_path.read_text(encoding="utf-8")
            match = block_pattern.search(html)
            if not match:
                continue

            block = match.group(1)
            action_count = len(action_pattern.findall(block))
            if action_count <= 1:
                continue

            primary_count = block.count("page-action-primary")
            self.assertEqual(
                primary_count,
                1,
                f"Expected exactly one page-action-primary in multi-action header: {template_path}",
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

    def test_critical_css_selectors_are_declared_once(self) -> None:
        css = self._read("app/static/css/app.css")
        selectors = {
            ".brand-icon": r"^\.brand-icon\s*\{",
            ".brand-text::after": r"^\.brand-text::after\s*\{",
            ".row-actions": r"^\.row-actions\s*\{",
            ".card-list-item": r"^\.card-list-item\s*\{",
            ".stat-card": r"^\.stat-card\s*\{",
            ".table thead th": r"^\.table thead th\s*\{",
        }
        for label, pattern in selectors.items():
            matches = re.findall(pattern, css, re.MULTILINE)
            self.assertEqual(
                len(matches),
                1,
                f"Expected one declaration for {label}, found {len(matches)}",
            )

    def test_deprecated_quick_links_and_live_indicator_styles_removed(self) -> None:
        css = self._read("app/static/css/app.css")
        self.assertNotIn(".quick-link", css)
        self.assertNotIn(".quick-links", css)
        self.assertNotIn(".live-indicator", css)
        self.assertNotIn(".live-indicator__dot", css)

    def test_unsubscribed_bulk_selection_counts_visible_checkboxes_only(self) -> None:
        html = self._read("app/templates/unsubscribed/list.html")
        self.assertIn("function getVisibleEntryCheckboxes()", html)
        self.assertIn("const visibleCheckboxes = getVisibleEntryCheckboxes();", html)
        self.assertIn("setCheckedForVisibleEntries(selectAllCheckbox.checked);", html)
        self.assertIn("setCheckedForVisibleEntries(true);", html)
        self.assertIn("setCheckedForVisibleEntries(false);", html)
        self.assertIn("window.addEventListener('resize', updateBulkUi);", html)
        self.assertNotIn("document.querySelectorAll('.entry-checkbox:checked')", html)

    def test_events_bulk_selection_syncs_hidden_duplicate_checkboxes(self) -> None:
        html = self._read("app/templates/events/list.html")
        self.assertIn('data-event-id="{{ event.id }}"', html)
        self.assertIn('function syncEventCheckboxes(changedCheckbox)', html)
        self.assertIn("syncEventCheckboxes(box);", html)
        self.assertIn("function getVisibleCheckedEventIds()", html)
        self.assertIn("const selectedEventIds = getVisibleCheckedEventIds();", html)
        self.assertIn("document.querySelectorAll('.event-checkbox[form]').forEach((checkbox) => {", html)
        self.assertIn("checkbox.removeAttribute('form');", html)
        self.assertIn("querySelectorAll('input[name=\"event_ids\"]')", html)
        self.assertIn(".querySelectorAll(`.event-checkbox[data-event-id=\"${eventId}\"]`)", html)
        self.assertNotIn('form="bulkDeleteForm" aria-label="Select event"', html)

    def test_scheduled_bulk_selection_syncs_duplicate_checkboxes_and_modal_ids(self) -> None:
        html = self._read("app/templates/scheduled/list.html")
        self.assertIn('data-record-id="{{ msg.id }}"', html)
        self.assertIn("function getVisibleGroupCheckboxes()", html)
        self.assertIn("function setCheckedAcrossDuplicates(sourceCheckbox)", html)
        self.assertIn('function syncGroupCheckboxes(changedCheckbox)', html)
        self.assertIn("setCheckedForVisibleRows(selectAllCheckbox.checked);", html)
        self.assertIn("setCheckedForVisibleRows(true);", html)
        self.assertIn("setCheckedForVisibleRows(false);", html)
        self.assertIn("window.addEventListener('resize', updateBulkUi);", html)
        self.assertIn("const pendingBulkControls = setupBulkControls('pending');", html)
        self.assertIn("const pastBulkControls = setupBulkControls('past');", html)
        self.assertIn("function updatePendingBulkCancelSelection()", html)
        self.assertIn('const ids = pendingBulkControls ? pendingBulkControls.getVisibleCheckedIds() : [];', html)
        self.assertIn("pendingBulkCancelForm.addEventListener('submit', function() {", html)
        self.assertIn('const ids = pastBulkControls ? pastBulkControls.getVisibleCheckedIds() : [];', html)
        self.assertNotIn(".js-select-row[data-bulk-group=\"pending\"]:checked", html)
        self.assertNotIn(".js-select-row[data-bulk-group=\"past\"]:checked", html)


if __name__ == "__main__":
    unittest.main()
