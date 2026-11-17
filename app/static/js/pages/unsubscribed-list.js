const backfillForm = document.querySelector('[data-backfill-form]');
const backfillAlert = document.getElementById('backfillAlert');

if (backfillForm) {
    const backfillButton = backfillForm.querySelector('[data-backfill-button]');

    backfillForm.addEventListener('submit', async (event) => {
        event.preventDefault();

        if (!confirm('Review past message failures for opt-outs and blocks?')) {
            return;
        }

        if (backfillButton) {
            backfillButton.disabled = true;
            backfillButton.classList.add('disabled');
            backfillButton.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Reviewing History...';
        }

        if (backfillAlert) {
            backfillAlert.classList.add('d-none');
            backfillAlert.textContent = '';
        }

        try {
            const response = await fetch(backfillForm.action, {
                method: 'POST',
                headers: {
                    'X-Requested-With': 'XMLHttpRequest',
                },
                credentials: 'same-origin',
                body: new FormData(backfillForm),
            });

            const payload = await response.json().catch(() => ({}));
            if (!response.ok) {
                throw new Error(payload?.error || 'The message history review could not be started.');
            }

            if (backfillAlert) {
                backfillAlert.textContent = payload.message + ' Refreshing...';
                backfillAlert.classList.remove('d-none');
                backfillAlert.classList.remove('alert-danger');
                backfillAlert.classList.add('alert-success');
            }
            setTimeout(function() { window.location.reload(); }, 1000);
        } catch (error) {
            const message = error instanceof Error ? error.message : 'The message history review could not be started.';
            if (backfillAlert) {
                backfillAlert.textContent = message;
                backfillAlert.classList.remove('d-none');
                backfillAlert.classList.remove('alert-success');
                backfillAlert.classList.add('alert-danger');
            } else {
                alert(message);
            }
        } finally {
            if (backfillButton) {
                backfillButton.disabled = false;
                backfillButton.classList.remove('disabled');
                backfillButton.innerHTML = '<i class="bi bi-arrow-repeat me-1" aria-hidden="true"></i>Review Message History';
            }
        }
    });
}

// Bulk selection functionality
const allEntryCheckboxes = Array.from(document.querySelectorAll('.entry-checkbox'));
const selectAllCheckbox = document.getElementById('selectAllEntries');
const selectAllBtn = document.getElementById('selectAllBtn');
const selectNoneBtn = document.getElementById('selectNoneBtn');
const selectedCount = document.getElementById('selectedCount');
const bulkDeleteBtn = document.getElementById('bulkDeleteBtn');
const bulkSelectionActions = document.getElementById('bulkSelectionActions');
const bulkDeleteCount = document.getElementById('bulkDeleteCount');
const confirmBulkDeleteBtn = document.getElementById('confirmBulkDeleteBtn');
const bulkDeleteForm = document.getElementById('bulkDeleteForm');

function isElementVisible(element) {
    if (!element) return false;
    if (element.offsetParent !== null) return true;
    return element.getClientRects().length > 0;
}

function getVisibleEntryCheckboxes() {
    return allEntryCheckboxes.filter(isElementVisible);
}

function syncMatchingCheckboxes(sourceCheckbox, checked) {
    allEntryCheckboxes.forEach(function(cb) {
        if (cb.name === sourceCheckbox.name && cb.value === sourceCheckbox.value) {
            cb.checked = checked;
        }
    });
}

function setCheckedForVisibleEntries(checked) {
    getVisibleEntryCheckboxes().forEach(function(cb) {
        syncMatchingCheckboxes(cb, checked);
    });
}

function updateBulkUi() {
    const visibleCheckboxes = getVisibleEntryCheckboxes();
    const count = visibleCheckboxes.filter(function(cb) { return cb.checked; }).length;
    if (selectedCount) selectedCount.textContent = count;
    if (bulkSelectionActions) {
        bulkSelectionActions.classList.toggle('is-visible', count > 0);
    }
    if (bulkDeleteCount) bulkDeleteCount.textContent = count;
    if (selectAllCheckbox) {
        selectAllCheckbox.checked = visibleCheckboxes.length > 0 && count === visibleCheckboxes.length;
        selectAllCheckbox.indeterminate = count > 0 && count < visibleCheckboxes.length;
    }
}

allEntryCheckboxes.forEach(function(cb) {
    cb.addEventListener('change', function() {
        syncMatchingCheckboxes(cb, cb.checked);
        updateBulkUi();
    });
});

if (selectAllCheckbox) {
    selectAllCheckbox.addEventListener('change', function() {
        setCheckedForVisibleEntries(selectAllCheckbox.checked);
        updateBulkUi();
    });
}

if (selectAllBtn) {
    selectAllBtn.addEventListener('click', function() {
        setCheckedForVisibleEntries(true);
        updateBulkUi();
    });
}

if (selectNoneBtn) {
    selectNoneBtn.addEventListener('click', function() {
        setCheckedForVisibleEntries(false);
        updateBulkUi();
    });
}

const bulkDeleteModal = document.getElementById('bulkDeleteModal');
if (bulkDeleteModal) {
    bulkDeleteModal.addEventListener('show.bs.modal', function() {
        const selected = getVisibleEntryCheckboxes().filter(function(cb) { return cb.checked; }).length;
        if (bulkDeleteCount) bulkDeleteCount.textContent = selected;
    });
}

if (confirmBulkDeleteBtn && bulkDeleteForm) {
    confirmBulkDeleteBtn.addEventListener('click', function() {
        bulkDeleteForm.submit();
    });
}

window.addEventListener('resize', updateBulkUi);
updateBulkUi();
