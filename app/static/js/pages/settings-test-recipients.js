const testRecipientsForm = document.getElementById('testRecipientsForm');
const recipientRows = document.getElementById('recipientRows');
const addRecipientRowBtn = document.getElementById('addRecipientRowBtn');
const recipientRowTemplate = document.getElementById('recipientRowTemplate');
const recipientLimit = Number(testRecipientsForm?.dataset.recipientLimit || 25);

function refreshRecipientRowIds() {
    recipientRows.querySelectorAll('.recipient-row').forEach((row, index) => {
        const labelInput = row.querySelector('[data-recipient-field="label"]');
        const phoneInput = row.querySelector('[data-recipient-field="phone"]');
        const labelLabel = row.querySelector('[data-recipient-label="label"]');
        const phoneLabel = row.querySelector('[data-recipient-label="phone"]');

        if (labelInput && labelLabel) {
            const labelId = `recipientLabel${index}`;
            labelInput.id = labelId;
            labelLabel.setAttribute('for', labelId);
        }

        if (phoneInput && phoneLabel) {
            const phoneId = `recipientPhone${index}`;
            phoneInput.id = phoneId;
            phoneLabel.setAttribute('for', phoneId);
        }
    });
}

function wireRecipientRow(row) {
    const removeButton = row.querySelector('[data-remove-recipient]');
    if (!removeButton) {
        return;
    }
    removeButton.addEventListener('click', function () {
        const rows = recipientRows.querySelectorAll('.recipient-row');
        if (rows.length <= 1) {
            row.querySelectorAll('input').forEach((input) => {
                input.value = '';
            });
            refreshRecipientRowIds();
            return;
        }
        row.remove();
        refreshRecipientRowIds();
    });
}

function addRecipientRow() {
    const rows = recipientRows.querySelectorAll('.recipient-row');
    if (rows.length >= recipientLimit) {
        return;
    }
    const fragment = recipientRowTemplate.content.cloneNode(true);
    const row = fragment.querySelector('.recipient-row');
    wireRecipientRow(row);
    recipientRows.appendChild(fragment);
    refreshRecipientRowIds();
}

recipientRows.querySelectorAll('.recipient-row').forEach(wireRecipientRow);
refreshRecipientRowIds();
addRecipientRowBtn?.addEventListener('click', addRecipientRow);
