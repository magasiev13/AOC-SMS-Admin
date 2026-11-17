(() => {
    const nameInput = document.getElementById('name');
    const slugInput = document.getElementById('slug');
    const previewEl = document.querySelector('[data-org-slug-preview]');
    if (!nameInput || !slugInput || !previewEl) {
        return;
    }

    let slugTouched = slugInput.value.trim().length > 0;

    const slugify = (value) => (
        (value || '')
            .toLowerCase()
            .trim()
            .replace(/[^a-z0-9]+/g, '-')
            .replace(/^-+|-+$/g, '')
            .slice(0, 64)
    );

    const updatePreview = () => {
        const value = slugInput.value.trim() || 'your-business';
        previewEl.textContent = value;
    };

    nameInput.addEventListener('input', () => {
        if (!slugTouched) {
            slugInput.value = slugify(nameInput.value);
        }
        updatePreview();
    });

    slugInput.addEventListener('input', () => {
        slugTouched = slugInput.value.trim().length > 0;
        slugInput.value = slugify(slugInput.value);
        updatePreview();
    });

    slugInput.addEventListener('blur', () => {
        slugInput.value = slugify(slugInput.value);
        slugTouched = slugInput.value.trim().length > 0;
        updatePreview();
    });

    updatePreview();
})();
