const toastEls = document.querySelectorAll('.app-toast');
toastEls.forEach((toastEl) => {
    const toast = new bootstrap.Toast(toastEl);
    toast.show();
});

const tableLoadingForms = document.querySelectorAll('form[data-table-loading-target]');
tableLoadingForms.forEach((form) => {
    form.addEventListener('submit', () => {
        const targetIds = form.dataset.tableLoadingTarget || '';
        targetIds.split(',').map((id) => id.trim()).filter(Boolean).forEach((id) => {
            const target = document.getElementById(id);
            if (target) {
                target.classList.add('is-loading');
            }
        });
    });
});

const iconEls = document.querySelectorAll('i.bi');
iconEls.forEach((iconEl) => {
    iconEl.setAttribute('aria-hidden', 'true');
});
