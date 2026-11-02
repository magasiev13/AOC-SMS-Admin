(() => {
    'use strict';
    const form = document.querySelector('[data-organization-setup]');
    if (!form) return;

    const steps = Array.from(form.querySelectorAll('[data-organization-step]'));
    const progress = form.querySelector('[data-organization-progress]');
    const links = Array.from(form.querySelectorAll('[data-organization-go]'));
    const navigation = form.querySelector('[data-organization-navigation]');
    const back = form.querySelector('[data-organization-back]');
    const next = form.querySelector('[data-organization-next]');
    const save = form.querySelector('[data-organization-save]');
    const status = form.querySelector('[data-organization-step-status]');
    if (!steps.length || !progress || !navigation || !back || !next || !save || !status) return;

    let current = 0;
    const visited = new Set([0]);
    const fields = (section) => Array.from(section.querySelectorAll('input, select, textarea'));
    const firstInvalid = (section) => fields(section).find(field => field.willValidate && !field.validity.valid);
    const regions = Array.from(form.querySelectorAll('input[name="business_regions"]'));
    const checkRegions = () => {
        if (regions.length) regions[0].setCustomValidity(
            regions.some(field => field.checked) ? '' : 'Choose at least one business region of operation.'
        );
    };
    regions.forEach(field => field.addEventListener('change', checkRegions));
    checkRegions();
    const openDetails = (field) => {
        let details = field.closest('details');
        while (details && form.contains(details)) {
            details.open = true;
            details = details.parentElement?.closest('details');
        }
    };

    const showStep = (index, focusHeading = false) => {
        if (index < 0 || index >= steps.length) return;
        current = index;
        visited.add(index);
        steps.forEach((section, position) => { section.hidden = position !== index; });
        links.forEach((link, position) => {
            if (position === index) link.setAttribute('aria-current', 'step');
            else link.removeAttribute('aria-current');
            link.classList.toggle('is-current', position === index);
            link.classList.toggle('is-complete', visited.has(position) && position !== index && !firstInvalid(steps[position]));
        });
        back.disabled = index === 0;
        next.hidden = index === steps.length - 1;
        save.hidden = index !== steps.length - 1;
        const title = links[index].querySelector('.organization-progress__label')?.textContent.trim() || '';
        status.textContent = `Part ${index + 1} of ${steps.length}: ${title}`;
        if (focusHeading) {
            form.scrollIntoView({ block: 'start' });
            steps[index].querySelector('legend')?.focus({ preventScroll: true });
        }
    };

    const revealField = (field, report = false) => {
        if (!field || !form.contains(field)) return;
        const section = field.closest('[data-organization-step]');
        const index = steps.indexOf(section);
        if (index < 0) return;
        showStep(index);
        openDetails(field);
        field.focus();
        if (report && typeof field.reportValidity === 'function') field.reportValidity();
    };

    const validateStep = () => {
        const invalid = firstInvalid(steps[current]);
        if (!invalid) return true;
        revealField(invalid, true);
        return false;
    };

    next.addEventListener('click', () => {
        if (validateStep()) showStep(current + 1, true);
    });
    back.addEventListener('click', () => showStep(current - 1, true));
    links.forEach(link => link.addEventListener('click', () => {
        const destination = steps.findIndex(section => section.dataset.organizationStep === link.dataset.organizationGo);
        if (destination <= current || validateStep()) showStep(destination, true);
    }));
    form.addEventListener('invalid', event => revealField(event.target), true);
    form.addEventListener('submit', event => {
        if (current < steps.length - 1) {
            event.preventDefault();
            if (validateStep()) showStep(current + 1, true);
            return;
        }
        const invalid = firstInvalid(form);
        if (!invalid) return;
        event.preventDefault();
        revealField(invalid, true);
    });
    form.querySelectorAll('[data-organization-error-link]').forEach(link => link.addEventListener('click', event => {
        const id = link.getAttribute('href')?.slice(1);
        const field = id ? document.getElementById(id) : null;
        if (!field || !form.contains(field)) return;
        event.preventDefault();
        revealField(field);
    }));

    // Keep the native complete form as the fallback if enhancement cannot start.
    // Validation is performed explicitly so hidden steps never trap browser focus.
    form.noValidate = true;
    form.dataset.enhanced = 'true';
    progress.hidden = false;
    navigation.hidden = false;
    status.hidden = false;
    const note = form.querySelector('[data-organization-save-note]');
    if (note) note.hidden = false;
    showStep(0);
    const initialError = form.dataset.initialErrorField;
    if (initialError) revealField(document.getElementById(initialError));
})();
