(() => {
    const providerModeInput = document.getElementById('provider_mode');
    const numberStrategyInput = document.getElementById('number_strategy');
    const existingSubaccountNumberInput = document.getElementById('existing_subaccount_phone_number_sid');
    const manualPhoneNumberSidInput = document.getElementById('manual_phone_number_sid');
    const finalizeNumberStrategyInput = document.getElementById('finalize_number_strategy');
    const finalizeExistingSubaccountNumberInput = document.getElementById('finalize_existing_subaccount_phone_number_sid');
    const finalizeManualPhoneNumberSidInput = document.getElementById('finalize_manual_phone_number_sid');
    const platformFields = document.querySelector('[data-platform-managed-fields]');
    const customerFields = document.querySelector('[data-customer-managed-fields]');
    if (!providerModeInput || !platformFields || !customerFields) {
        return;
    }

    const existingSubaccountNumberField = document.querySelector('[data-existing-subaccount-number-field]');
    const manualPhoneNumberSidField = document.querySelector('[data-manual-phone-number-sid-field]');

    const syncFinalizeSenderFields = () => {
        if (finalizeNumberStrategyInput && numberStrategyInput) {
            finalizeNumberStrategyInput.value = numberStrategyInput.value || '';
        }
        if (finalizeExistingSubaccountNumberInput && existingSubaccountNumberInput) {
            finalizeExistingSubaccountNumberInput.value = existingSubaccountNumberInput.value || '';
        }
        if (finalizeManualPhoneNumberSidInput && manualPhoneNumberSidInput) {
            finalizeManualPhoneNumberSidInput.value = manualPhoneNumberSidInput.value || '';
        }
    };

    const syncNumberStrategy = () => {
        if (!numberStrategyInput) {
            return;
        }
        const customerManaged = providerModeInput.value === 'customer_managed';
        const selectedStrategy = numberStrategyInput.value;
        const showExistingSubaccountNumberField = selectedStrategy === 'existing_subaccount_number';
        const showManualPhoneNumberSidField = selectedStrategy === 'transfer_parent_number' || selectedStrategy === 'platform_assign';
        if (existingSubaccountNumberField) {
            existingSubaccountNumberField.hidden = !showExistingSubaccountNumberField;
            existingSubaccountNumberField.querySelectorAll('input, select, textarea').forEach((field) => {
                field.disabled = customerManaged || !showExistingSubaccountNumberField;
            });
        }
        if (manualPhoneNumberSidField) {
            manualPhoneNumberSidField.hidden = !showManualPhoneNumberSidField;
            manualPhoneNumberSidField.querySelectorAll('input, select, textarea').forEach((field) => {
                field.disabled = customerManaged || !showManualPhoneNumberSidField;
            });
        }
        syncFinalizeSenderFields();
    };

    const syncProviderMode = () => {
        const customerManaged = providerModeInput.value === 'customer_managed';
        platformFields.hidden = customerManaged;
        customerFields.hidden = !customerManaged;
        platformFields.querySelectorAll('input, select, textarea').forEach((field) => {
            field.disabled = customerManaged;
        });
        customerFields.querySelectorAll('input, select, textarea').forEach((field) => {
            field.disabled = !customerManaged;
        });
        syncNumberStrategy();
    };

    providerModeInput.addEventListener('change', syncProviderMode);
    if (numberStrategyInput) {
        numberStrategyInput.addEventListener('change', syncNumberStrategy);
    }
    if (existingSubaccountNumberInput) {
        existingSubaccountNumberInput.addEventListener('change', syncFinalizeSenderFields);
    }
    if (manualPhoneNumberSidInput) {
        manualPhoneNumberSidInput.addEventListener('input', syncFinalizeSenderFields);
    }
    syncProviderMode();
})();

(() => {
    const textarea = document.getElementById('platform_test_body');
    const stats = document.getElementById('platformTestBodyStats');
    if (!textarea || !stats || !window.TwineviaSmsAnalysis) {
        return;
    }

    const updateStats = () => {
        const analysis = window.TwineviaSmsAnalysis.analyzeBody(textarea.value || '');
        const normalizationNote = analysis.normalizationApplied
            ? ` Normalizes ${analysis.replacementCount} punctuation/spacing character(s) before send.`
            : '';
        stats.textContent = `Encoding: ${window.TwineviaSmsAnalysis.formatEncodingLabel(analysis.encoding)}. ${analysis.segmentCount} segment(s). ${analysis.charactersToNextSegment} characters until the next segment.${normalizationNote}`;
    };

    textarea.addEventListener('input', updateStats);
    updateStats();
})();
