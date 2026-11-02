(() => {
    const shell = document.querySelector('[data-setup-shell]');
    if (!shell || !['launch', 'provider'].includes(shell.dataset.currentStep)) return;

    const providerMode = shell.dataset.providerMode || 'platform_managed';
    const waiting = providerMode === 'customer_managed'
        ? ['pending', 'provisioning'].includes(shell.dataset.providerStatus)
        : ['queued', 'processing', 'pending', 'approved'].includes(shell.dataset.onboardingStatus);
    if (!waiting || shell.dataset.messagingReady === 'true') return;

    const snapshot = ({ currentStep, onboardingStatus, providerStatus, messagingReady, lastError }) => JSON.stringify({
        currentStep: currentStep || '',
        onboardingStatus: onboardingStatus || '',
        providerStatus: providerStatus || '',
        messagingReady: Boolean(messagingReady),
        lastError: lastError || '',
    });
    const initial = snapshot({
        currentStep: shell.dataset.statusCurrentStep || shell.dataset.currentStep,
        onboardingStatus: shell.dataset.onboardingStatus,
        providerStatus: shell.dataset.providerStatus,
        messagingReady: shell.dataset.messagingReady === 'true',
        lastError: shell.dataset.lastError,
    });
    const notice = shell.querySelector('[data-setup-status-update]');
    const message = shell.querySelector('[data-setup-status-message]');
    const refresh = shell.querySelector('[data-setup-status-refresh]');
    let dirty = false;
    let checking = false;

    const protectChanges = (event) => {
        const field = event.target;
        if (field.closest('form') && !['hidden', 'button', 'submit', 'reset'].includes(field.type)) dirty = true;
    };
    document.addEventListener('input', protectChanges);
    document.addEventListener('change', protectChanges);
    const editing = () => Boolean(document.activeElement?.closest('form'));

    const showUpdate = () => {
        if (!notice || !message) return;
        notice.hidden = false;
        message.textContent = dirty
            ? 'Setup status changed. Your changes are still here. Save your work before reopening this step.'
            : 'Setup status changed. Refresh when you are ready to see the latest details.';
    };

    const checkStatus = async (keepPage = false) => {
        if (checking) return;
        checking = true;
        try {
            const response = await fetch(shell.dataset.setupStatusUrl, {
                headers: { 'X-Requested-With': 'XMLHttpRequest' },
                credentials: 'same-origin',
            });
            if (!response.ok) return;
            const payload = await response.json();
            const current = snapshot({
                currentStep: payload.current_step,
                onboardingStatus: payload.onboarding?.status,
                providerStatus: payload.messaging?.provider_status,
                messagingReady: payload.messaging?.can_send,
                lastError: payload.onboarding?.last_error,
            });
            if (current === initial) return;
            if (dirty || editing() || keepPage) showUpdate();
            else window.location.reload();
        } catch (_) {
            // A temporary status-check failure must not interrupt setup work.
        } finally {
            checking = false;
        }
    };

    refresh?.addEventListener('click', () => {
        if (dirty || editing()) checkStatus(true);
        else window.location.reload();
    });
    let timer = window.setInterval(checkStatus, 8000);
    window.addEventListener('pagehide', () => {
        window.clearInterval(timer);
        timer = null;
    });
    window.addEventListener('pageshow', (event) => {
        if (event.persisted && timer === null) timer = window.setInterval(checkStatus, 8000);
    });
})();
