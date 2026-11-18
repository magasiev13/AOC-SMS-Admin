(() => {
    const registrationPathInput = document.getElementById('registration_path');
    const businessTypeInput = document.getElementById('business_type');
    const taxIdInput = document.getElementById('has_business_tax_id');
    const websiteCheckbox = document.getElementById('has_public_website');
    const registrationIdentifierInput = document.getElementById('business_registration_identifier');
    const registrationNumberInput = document.getElementById('business_registration_number');
    const externalFields = Array.from(document.querySelectorAll('[data-external-site-field]'));
    if (!registrationPathInput || !businessTypeInput || !taxIdInput || !websiteCheckbox || !registrationIdentifierInput || !registrationNumberInput) {
        return;
    }

    const syncRegistrationDefaults = () => {
        const hasTaxId = taxIdInput.checked;
        const businessType = businessTypeInput.value;
        if (businessType === 'Sole Proprietor' && !hasTaxId) {
            registrationPathInput.value = 'sole_proprietor';
        } else if (registrationPathInput.value === 'sole_proprietor' && hasTaxId) {
            registrationPathInput.value = 'low_volume_standard';
        }

        const requiresRegistrationDetails = registrationPathInput.value !== 'sole_proprietor';
        taxIdInput.required = requiresRegistrationDetails;
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

    businessTypeInput.addEventListener('change', syncRegistrationDefaults);
    taxIdInput.addEventListener('change', syncRegistrationDefaults);
    registrationPathInput.addEventListener('change', syncRegistrationDefaults);
    websiteCheckbox.addEventListener('change', syncWebsiteFields);
    syncRegistrationDefaults();
    syncWebsiteFields();
})();
