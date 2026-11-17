let userFormToSubmit = null;
const deleteUserModal = document.getElementById('deleteUserModal');

if (deleteUserModal) {
    deleteUserModal.addEventListener('show.bs.modal', function(event) {
        const button = event.relatedTarget;
        const username = button.getAttribute('data-username') || 'this user';
        const formId = button.getAttribute('data-form-id');

        document.getElementById('deleteUsername').textContent = username;
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
