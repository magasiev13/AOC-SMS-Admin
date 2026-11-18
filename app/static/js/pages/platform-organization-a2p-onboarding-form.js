(() => {
    const registrationPathInput = document.getElementById('registration_path');
    const businessTypeInput = document.getElementById('business_type');
    const taxIdInput = document.getElementById('has_business_tax_id');
    const websiteCheckbox = document.getElementById('has_public_website');
    const campaignUseCaseInput = document.getElementById('campaign_use_case');
    const registrationIdentifierInput = document.getElementById('business_registration_identifier');
    const registrationNumberInput = document.getElementById('business_registration_number');
    const messageSamplesInput = document.getElementById('message_samples');
    const externalFields = Array.from(document.querySelectorAll('[data-external-site-field]'));
    if (!registrationPathInput || !businessTypeInput || !taxIdInput || !websiteCheckbox || !campaignUseCaseInput || !registrationIdentifierInput || !registrationNumberInput || !messageSamplesInput) {
        return;
    }

    const forcedBusinessTypeByPath = {
        nonprofit: 'Non-profit Corporation',
        government: 'Non-profit Corporation',
        sole_proprietor: 'Sole Proprietor',
    };

    const syncRegistrationRequirements = () => {
        if (businessTypeInput.value === 'Sole Proprietor' && !taxIdInput.checked) {
            registrationPathInput.value = 'sole_proprietor';
        } else if (registrationPathInput.value === 'sole_proprietor' && taxIdInput.checked) {
            registrationPathInput.value = 'low_volume_standard';
        }

        const path = registrationPathInput.value;
        const forcedBusinessType = forcedBusinessTypeByPath[path] || '';
        const usesSelectableBusinessType = path === 'standard' || path === 'low_volume_standard';
        const requiresRegistrationDetails = path !== 'sole_proprietor';

        if (forcedBusinessType) {
            businessTypeInput.value = forcedBusinessType;
        } else if (!usesSelectableBusinessType) {
            businessTypeInput.value = '';
        }

        businessTypeInput.disabled = !usesSelectableBusinessType;
        businessTypeInput.required = usesSelectableBusinessType;
        registrationIdentifierInput.required = requiresRegistrationDetails;
        registrationNumberInput.required = requiresRegistrationDetails;

        if (requiresRegistrationDetails && !registrationIdentifierInput.value.trim()) {
            registrationIdentifierInput.value = 'EIN';
        }
    };

    const syncWebsiteFields = () => {
        const enabled = websiteCheckbox.checked;
        externalFields.forEach((wrapper) => {
            wrapper.hidden = !enabled;
            wrapper.querySelectorAll('input, textarea, select').forEach((field) => {
                if (!field.name.startsWith('external_')) {
                    return;
                }
                field.disabled = !enabled;
            });
        });
    };

    const syncMessageSampleValidation = () => {
        const count = messageSamplesInput.value
            .split(/\r?\n/)
            .map((line) => line.trim())
            .filter(Boolean)
            .length;

        messageSamplesInput.setCustomValidity(
            count < 2
                ? 'Provide at least two real message samples.'
                : ''
        );
    };

    registrationPathInput.addEventListener('change', syncRegistrationRequirements);
    businessTypeInput.addEventListener('change', syncRegistrationRequirements);
    taxIdInput.addEventListener('change', syncRegistrationRequirements);
    websiteCheckbox.addEventListener('change', syncWebsiteFields);
    campaignUseCaseInput.addEventListener('change', syncMessageSampleValidation);
    messageSamplesInput.addEventListener('input', syncMessageSampleValidation);
    syncRegistrationRequirements();
    syncWebsiteFields();
    syncMessageSampleValidation();
})();

(() => {
    const autoRefreshEnabled = document.currentScript.dataset.pending === 'true';
    if (!autoRefreshEnabled) {
        return;
    }

    window.setInterval(() => {
        const activeTag = document.activeElement?.tagName || '';
        if (['INPUT', 'TEXTAREA', 'SELECT'].includes(activeTag)) {
            return;
        }
        window.location.reload();
    }, 15000);
})();
