if (!window.__dataConfirmHandlerBound) {
    window.__dataConfirmHandlerBound = true;

    document.addEventListener('click', (event) => {
        const confirmTarget = event.target.closest('[data-confirm]');
        if (!confirmTarget) {
            return;
        }

        const message = confirmTarget.dataset.confirm || 'Are you sure?';
        if (!window.confirm(message)) {
            event.preventDefault();
        }
    });
}

if (!window.__signupStepperBound) {
    window.__signupStepperBound = true;

    const signupForms = document.querySelectorAll('[data-signup-form]');

    signupForms.forEach((form) => {
        const steps = Array.from(form.querySelectorAll('[data-signup-step]'));
        if (steps.length < 2) {
            return;
        }

        const progressRoot = form.parentElement?.querySelector('[data-signup-progress]');
        const indicators = Array.from(progressRoot?.querySelectorAll('[data-signup-indicator]') || []);
        const maxStep = steps.length;

        const clampStep = (value) => Math.min(Math.max(value, 1), maxStep);

        const syncIndicators = (currentStep) => {
            indicators.forEach((indicator, index) => {
                const stepIndex = index + 1;
                indicator.classList.toggle('is-current', stepIndex === currentStep);
                indicator.classList.toggle('is-complete', stepIndex < currentStep);
                indicator.setAttribute('aria-current', stepIndex === currentStep ? 'step' : 'false');
            });
        };

        const showStep = (requestedStep, { focus = false } = {}) => {
            const currentStep = clampStep(requestedStep);
            form.dataset.currentStep = String(currentStep);

            steps.forEach((step, index) => {
                const isActive = index + 1 === currentStep;
                step.hidden = !isActive;
                step.setAttribute('aria-hidden', isActive ? 'false' : 'true');
            });

            syncIndicators(currentStep);

            if (focus) {
                const firstField = steps[currentStep - 1].querySelector('input, select, textarea');
                if (firstField) {
                    window.requestAnimationFrame(() => firstField.focus());
                }
            }
        };

        const validateCurrentStep = () => {
            const currentStep = clampStep(Number(form.dataset.currentStep || '1'));
            const fields = Array.from(steps[currentStep - 1].querySelectorAll('input, select, textarea'));
            const invalidField = fields.find((field) => typeof field.checkValidity === 'function' && !field.checkValidity());
            if (!invalidField) {
                return true;
            }

            invalidField.reportValidity();
            invalidField.focus();
            return false;
        };

        form.dataset.enhanced = 'true';

        form.querySelectorAll('[data-signup-next]').forEach((button) => {
            button.addEventListener('click', () => {
                if (!validateCurrentStep()) {
                    return;
                }
                const currentStep = clampStep(Number(form.dataset.currentStep || '1'));
                showStep(currentStep + 1, { focus: true });
            });
        });

        form.querySelectorAll('[data-signup-back]').forEach((button) => {
            button.addEventListener('click', () => {
                const currentStep = clampStep(Number(form.dataset.currentStep || '1'));
                showStep(currentStep - 1, { focus: true });
            });
        });

        showStep(clampStep(Number(form.dataset.currentStep || '1')));
    });
}
