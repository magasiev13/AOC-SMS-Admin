// Modal handling for delete/cancel actions
let formToSubmit = null;
const actionModal = document.getElementById('actionModal');

if (actionModal) {
    actionModal.addEventListener('show.bs.modal', function(event) {
        const button = event.relatedTarget;
        const action = button.getAttribute('data-action') || 'Delete';
        const formId = button.getAttribute('data-form-id');

        document.getElementById('actionModalTitle').textContent = action === 'Cancel remaining sends' ? 'Cancel Remaining Sends' : 'Confirm ' + action;
        document.getElementById('actionModalText').textContent = action === 'Cancel remaining sends' ? 'Stop the remaining sends? Already accepted messages cannot be recalled.' : action + ' this scheduled message?';
        formToSubmit = document.getElementById(formId);
    });

    document.getElementById('confirmActionBtn').addEventListener('click', function() {
        if (formToSubmit) {
            formToSubmit.submit();
        }
    });
}

const pendingIdsContainer = document.getElementById('pendingIdsContainer');
let currentPendingIds = [];
const statusEndpoint = `/scheduled/status${window.location.search || ''}`;
if (pendingIdsContainer && pendingIdsContainer.dataset && pendingIdsContainer.dataset.pendingIds) {
    try {
        currentPendingIds = JSON.parse(pendingIdsContainer.dataset.pendingIds);
    } catch (e) {
        currentPendingIds = [];
    }
}

async function checkStatus() {
    try {
        const response = await fetch(statusEndpoint);
        if (!response.ok) return;
        const data = await response.json();
        if (!Array.isArray(data.pending_ids)) return;

        const currentSorted = currentPendingIds.slice().sort((a,b) => a-b).join(',');
        const newSorted = data.pending_ids.slice().sort((a,b) => a-b).join(',');

        if (currentSorted !== newSorted) {
            location.reload();
        }
    } catch (e) {
        console.error('Status check failed:', e);
    }
}

if (Array.isArray(currentPendingIds) && currentPendingIds.length > 0) {
    setTimeout(() => setInterval(checkStatus, 3000), 1000);
}

function setupBulkControls(group) {
    const selectedCountEl = document.getElementById(`${group}SelectedCount`);
    const selectAllCheckbox = document.getElementById(`selectAll${group.charAt(0).toUpperCase()}${group.slice(1)}`);
    const bulkSelectionActions = document.getElementById(`${group}BulkSelectionActions`);
    const groupSelector = `.js-select-row[data-bulk-group="${group}"]`;

    function getAllGroupCheckboxes() {
        return Array.from(document.querySelectorAll(groupSelector));
    }

    function getVisibleGroupCheckboxes() {
        return getAllGroupCheckboxes().filter(b => b.offsetParent !== null);
    }

    function setCheckedAcrossDuplicates(sourceCheckbox) {
        const sourceValue = sourceCheckbox.value;
        if (!sourceValue) return;
        getAllGroupCheckboxes()
            .filter(b => b.value === sourceValue)
            .forEach(b => {
                b.checked = sourceCheckbox.checked;
            });
    }

    function setCheckedForVisibleRows(isChecked) {
        getVisibleGroupCheckboxes().forEach((checkbox) => {
            checkbox.checked = isChecked;
            syncGroupCheckboxes(checkbox);
        });
    }

    function syncGroupCheckboxes(changedCheckbox) {
        const recordId = changedCheckbox.dataset.recordId;
        if (recordId) {
            document
                .querySelectorAll(`.js-select-row[data-bulk-group="${group}"][data-record-id="${recordId}"]`)
                .forEach((checkbox) => {
                    checkbox.checked = changedCheckbox.checked;
                });
            return;
        }
        setCheckedAcrossDuplicates(changedCheckbox);
    }

    function updateBulkUi() {
        if (!selectedCountEl) return;
        const boxes = getVisibleGroupCheckboxes();
        const selected = boxes.filter(b => b.checked).length;
        selectedCountEl.textContent = selected;
        if (bulkSelectionActions) {
            bulkSelectionActions.classList.toggle('is-visible', selected > 0);
        }
        if (selectAllCheckbox) {
            selectAllCheckbox.checked = boxes.length > 0 && boxes.every(b => b.checked);
            selectAllCheckbox.indeterminate = selected > 0 && selected < boxes.length;
        }
    }

    if (selectAllCheckbox) {
        selectAllCheckbox.addEventListener('change', function() {
            setCheckedForVisibleRows(selectAllCheckbox.checked);
            updateBulkUi();
        });
    }

    const selectAllBtn = document.getElementById(`${group}SelectAllBtn`);
    if (selectAllBtn) {
        selectAllBtn.addEventListener('click', function() {
            setCheckedForVisibleRows(true);
            updateBulkUi();
        });
    }

    const selectNoneBtn = document.getElementById(`${group}SelectNoneBtn`);
    if (selectNoneBtn) {
        selectNoneBtn.addEventListener('click', function() {
            setCheckedForVisibleRows(false);
            updateBulkUi();
        });
    }

    getAllGroupCheckboxes().forEach((checkbox) => {
        checkbox.addEventListener('change', function() {
            syncGroupCheckboxes(checkbox);
            updateBulkUi();
        });
    });
    window.addEventListener('resize', updateBulkUi);
    updateBulkUi();

    return {
        getVisibleCheckedIds() {
            return Array.from(new Set(
                getVisibleGroupCheckboxes()
                    .filter((box) => box.checked)
                    .map((box) => box.value)
                    .filter(Boolean)
            ));
        },
    };
}

const pendingBulkControls = setupBulkControls('pending');
const pastBulkControls = setupBulkControls('past');

function updatePendingBulkCancelSelection() {
    const ids = pendingBulkControls ? pendingBulkControls.getVisibleCheckedIds() : [];
    document.getElementById('pendingBulkCancelCount').textContent = ids.length;
    document.getElementById('pendingBulkCancelIds').value = ids.join(',');
}

// Bulk cancel modal handler for pending messages
const pendingBulkCancelModal = document.getElementById('pendingBulkCancelModal');
if (pendingBulkCancelModal) {
    pendingBulkCancelModal.addEventListener('show.bs.modal', function() {
        updatePendingBulkCancelSelection();
    });
}

const pendingBulkCancelForm = document.getElementById('pendingBulkCancelForm');
if (pendingBulkCancelForm) {
    pendingBulkCancelForm.addEventListener('submit', function() {
        updatePendingBulkCancelSelection();
    });
}

// Bulk delete modal handler for past messages
const pastBulkDeleteModal = document.getElementById('pastBulkDeleteModal');
if (pastBulkDeleteModal) {
    pastBulkDeleteModal.addEventListener('show.bs.modal', function() {
        const ids = pastBulkControls ? pastBulkControls.getVisibleCheckedIds() : [];
        document.getElementById('pastBulkDeleteCount').textContent = ids.length;
        document.getElementById('pastBulkDeleteIds').value = ids.join(',');
    });
}
