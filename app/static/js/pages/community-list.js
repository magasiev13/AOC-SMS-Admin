document.getElementById('memberPreview')?.addEventListener('show.bs.offcanvas', function(event) {
    const data = event.relatedTarget?.dataset || {};
    document.getElementById('memberPreviewTitle').textContent = data.memberName || 'Contact Preview';
    document.getElementById('memberPreviewPhone').textContent = data.memberPhone || '';
    document.getElementById('memberPreviewJoined').textContent = data.memberJoined || '';
    document.getElementById('memberPreviewStatus').textContent = data.memberStatus || '';
    const review = document.getElementById('memberPreviewReview');
    if (review) { review.hidden = !data.memberReview; review.href = data.memberReview || '#'; }
});
let formToSubmit = null;
const deleteModal = document.getElementById('deleteModal');

deleteModal.addEventListener('show.bs.modal', function(event) {
    const button = event.relatedTarget;
    const memberName = button.getAttribute('data-member-name') || 'this contact';
    const formId = button.getAttribute('data-form-id');

    document.getElementById('deleteMemberName').textContent = memberName;
    formToSubmit = document.getElementById(formId);
});

document.getElementById('confirmDeleteBtn').addEventListener('click', function() {
    if (formToSubmit) {
        formToSubmit.submit();
    }
});

const bulkDeleteForm = document.getElementById('bulkDeleteForm');
const selectPageBoxes = document.querySelectorAll('[data-select-page]');
const memberBoxes = Array.from(document.querySelectorAll('.member-checkbox'));
const formBoxes = memberBoxes.filter(box => box.form === bulkDeleteForm);
const selectedCountEl = document.getElementById('selectedCount');
const bulkSelectionActions = document.getElementById('bulkSelectionActions');

function updateBulkUi() {
    if (!selectedCountEl) return;
    const selected = formBoxes.filter(box => box.checked).length;
    selectedCountEl.textContent = selected;
    bulkSelectionActions.classList.toggle('is-visible', selected > 0);
    for (const box of selectPageBoxes) {
        box.checked = selected > 0 && selected === formBoxes.length;
        box.indeterminate = selected > 0 && selected < formBoxes.length;
    }
}

function selectPage(checked) {
    for (const box of memberBoxes) box.checked = checked;
    updateBulkUi();
}

for (const box of selectPageBoxes) {
    box.addEventListener('change', () => selectPage(box.checked));
}
document.getElementById('selectNoneBtn')?.addEventListener('click', () => selectPage(false));

document.addEventListener('change', event => {
    if (!event.target.matches('.member-checkbox')) return;
    // Both layouts represent the same contact. Only the table input submits its ID.
    for (const box of memberBoxes) {
        if (box.value === event.target.value) box.checked = event.target.checked;
    }
    updateBulkUi();
});

const bulkDeleteModal = document.getElementById('bulkDeleteModal');
if (bulkDeleteModal) {
    bulkDeleteModal.addEventListener('show.bs.modal', function() {
        const selected = formBoxes.filter(box => box.checked).length;
        const countEl = document.getElementById('bulkDeleteCount');
        if (countEl) countEl.textContent = selected;
    });
}

const confirmBulkDeleteBtn = document.getElementById('confirmBulkDeleteBtn');
if (confirmBulkDeleteBtn) {
    confirmBulkDeleteBtn.addEventListener('click', function() {
        if (bulkDeleteForm) {
            bulkDeleteForm.submit();
        }
    });
}

updateBulkUi();
