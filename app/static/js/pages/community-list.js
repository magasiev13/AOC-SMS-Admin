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
const selectAllMembers = document.getElementById('selectAllMembers');
const selectedCountEl = document.getElementById('selectedCount');
const bulkDeleteBtn = document.getElementById('bulkDeleteBtn');
const bulkSelectionActions = document.getElementById('bulkSelectionActions');
function isElementVisible(element) {
    if (!element) return false;
    if (element.offsetParent !== null) return true;
    return element.getClientRects().length > 0;
}

function getMemberCheckboxes() {
    return Array.from(document.querySelectorAll('.member-checkbox'))
        .filter(isElementVisible);
}

function updateBulkUi() {
    if (!selectedCountEl) return;
    const boxes = getMemberCheckboxes();
    const selected = boxes.filter(b => b.checked).length;
    selectedCountEl.textContent = selected;
    if (bulkSelectionActions) {
        bulkSelectionActions.classList.toggle('is-visible', selected > 0);
    }

    if (selectAllMembers) {
        selectAllMembers.checked = boxes.length > 0 && boxes.every(b => b.checked);
        selectAllMembers.indeterminate = selected > 0 && selected < boxes.length;
    }
}

if (selectAllMembers) {
    selectAllMembers.addEventListener('change', function() {
        const boxes = getMemberCheckboxes();
        boxes.forEach(b => { b.checked = selectAllMembers.checked; });
        updateBulkUi();
    });
}

const selectAllBtn = document.getElementById('selectAllBtn');
if (selectAllBtn) {
    selectAllBtn.addEventListener('click', function() {
        const boxes = getMemberCheckboxes();
        boxes.forEach(b => { b.checked = true; });
        updateBulkUi();
    });
}

const selectNoneBtn = document.getElementById('selectNoneBtn');
if (selectNoneBtn) {
    selectNoneBtn.addEventListener('click', function() {
        const boxes = getMemberCheckboxes();
        boxes.forEach(b => { b.checked = false; });
        updateBulkUi();
    });
}

document.addEventListener('change', function(event) {
    if (event.target && event.target.classList.contains('member-checkbox')) {
        updateBulkUi();
    }
});

const bulkDeleteModal = document.getElementById('bulkDeleteModal');
if (bulkDeleteModal) {
    bulkDeleteModal.addEventListener('show.bs.modal', function() {
        const boxes = getMemberCheckboxes();
        const selected = boxes.filter(b => b.checked).length;
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
