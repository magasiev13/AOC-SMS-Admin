let formToSubmit = null;
const deleteEventModal = document.getElementById('deleteEventModal');

if (deleteEventModal) {
    deleteEventModal.addEventListener('show.bs.modal', function(event) {
        const button = event.relatedTarget;
        const eventTitle = button.getAttribute('data-event-title') || 'this event';
        const formId = button.getAttribute('data-form-id');
        const registrationCount = button.getAttribute('data-registration-count') || '0';

        document.getElementById('deleteEventName').textContent = eventTitle;
        document.getElementById('deleteEventImpact').textContent =
            `Deleting will remove ${registrationCount} registration(s).`;
        formToSubmit = document.getElementById(formId);
    });

    const confirmDeleteEventBtn = document.getElementById('confirmDeleteEventBtn');
    confirmDeleteEventBtn.addEventListener('click', function() {
        if (formToSubmit) {
            formToSubmit.submit();
        }
    });
}

const selectedCountEl = document.getElementById('selectedCount');
const selectAllEvents = document.getElementById('selectAllEvents');
const bulkSelectionActions = document.getElementById('bulkSelectionActions');
const bulkDeleteForm = document.getElementById('bulkDeleteForm');

document.querySelectorAll('.event-checkbox[form]').forEach((checkbox) => {
    checkbox.removeAttribute('form');
});

function isVisible(element) {
    return element.offsetParent !== null;
}

function getEventCheckboxes() {
    return Array.from(document.querySelectorAll('.event-checkbox')).filter(isVisible);
}

function getVisibleCheckedEventIds() {
    return Array.from(new Set(
        getEventCheckboxes()
            .filter((box) => box.checked)
            .map((box) => box.value)
            .filter(Boolean)
    ));
}

function syncEventCheckboxes(changedCheckbox) {
    const eventId = changedCheckbox.dataset.eventId;
    if (!eventId) return;
    document
        .querySelectorAll(`.event-checkbox[data-event-id="${eventId}"]`)
        .forEach((checkbox) => {
            checkbox.checked = changedCheckbox.checked;
        });
}

function updateBulkUi() {
    if (!selectedCountEl) return;
    const boxes = getEventCheckboxes();
    const selected = boxes.filter(b => b.checked).length;
    selectedCountEl.textContent = selected;
    if (bulkSelectionActions) {
        bulkSelectionActions.classList.toggle('is-visible', selected > 0);
    }
    if (selectAllEvents) {
        selectAllEvents.checked = boxes.length > 0 && boxes.every(b => b.checked);
        selectAllEvents.indeterminate = selected > 0 && selected < boxes.length;
    }
}

if (selectAllEvents) {
    selectAllEvents.addEventListener('change', function() {
        const boxes = getEventCheckboxes();
        boxes.forEach((box) => {
            box.checked = selectAllEvents.checked;
            syncEventCheckboxes(box);
        });
        updateBulkUi();
    });
}

const selectAllBtn = document.getElementById('selectAllBtn');
if (selectAllBtn) {
    selectAllBtn.addEventListener('click', function() {
        const boxes = getEventCheckboxes();
        boxes.forEach((box) => {
            box.checked = true;
            syncEventCheckboxes(box);
        });
        updateBulkUi();
    });
}

const selectNoneBtn = document.getElementById('selectNoneBtn');
if (selectNoneBtn) {
    selectNoneBtn.addEventListener('click', function() {
        const boxes = getEventCheckboxes();
        boxes.forEach((box) => {
            box.checked = false;
            syncEventCheckboxes(box);
        });
        updateBulkUi();
    });
}

document.querySelectorAll('.js-select-row').forEach((box) => {
    box.addEventListener('change', function() {
        syncEventCheckboxes(box);
        updateBulkUi();
    });
});

const bulkDeleteModal = document.getElementById('bulkDeleteModal');
if (bulkDeleteModal) {
    bulkDeleteModal.addEventListener('show.bs.modal', function() {
        const selected = getEventCheckboxes().filter((box) => box.checked).length;
        const countEl = document.getElementById('bulkDeleteCount');
        if (countEl) countEl.textContent = selected;
    });
}

const confirmBulkDeleteBtn = document.getElementById('confirmBulkDeleteBtn');
if (confirmBulkDeleteBtn) {
    confirmBulkDeleteBtn.addEventListener('click', function() {
        if (bulkDeleteForm) {
            const selectedEventIds = getVisibleCheckedEventIds();
            bulkDeleteForm
                .querySelectorAll('input[name="event_ids"]')
                .forEach((input) => input.remove());
            selectedEventIds.forEach((eventId) => {
                const hiddenInput = document.createElement('input');
                hiddenInput.type = 'hidden';
                hiddenInput.name = 'event_ids';
                hiddenInput.value = eventId;
                bulkDeleteForm.appendChild(hiddenInput);
            });
            bulkDeleteForm.submit();
        }
    });
}

window.addEventListener('resize', updateBulkUi);
updateBulkUi();
