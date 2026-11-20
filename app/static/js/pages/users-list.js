let userFormToSubmit = null;
const deleteUserModal = document.getElementById('deleteUserModal');

if (deleteUserModal) {
    deleteUserModal.addEventListener('show.bs.modal', function(event) {
        const button = event.relatedTarget;
        const username = button.dataset.username;
        const name = button.dataset.displayName || username;
        const formId = button.getAttribute('data-form-id');

        document.getElementById('deleteUserName').textContent = name;
        document.getElementById('deleteUserLogin').textContent = username && username !== name ? ` (${username})` : '';
        userFormToSubmit = document.getElementById(formId);
    });
}

const confirmDeleteUserBtn = document.getElementById('confirmDeleteUserBtn');
if (confirmDeleteUserBtn) {
    confirmDeleteUserBtn.addEventListener('click', function() {
        if (userFormToSubmit) {
            userFormToSubmit.submit();
        }
    });
}
