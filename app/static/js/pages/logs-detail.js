const logStatusMeta = document.getElementById('logStatusMeta');
if (logStatusMeta && logStatusMeta.dataset.status === 'processing') {
    const logId = logStatusMeta.dataset.logId;
    const initial = {
        status: logStatusMeta.dataset.status || 'sent',
        success: Number(logStatusMeta.dataset.successCount || 0),
        failure: Number(logStatusMeta.dataset.failureCount || 0),
    };

    async function checkLogStatus() {
        try {
            const response = await fetch(`/logs/status?ids=${encodeURIComponent(logId)}`);
            if (!response.ok) return;
            const payload = await response.json();
            const current = Array.isArray(payload.logs)
                ? payload.logs.find((entry) => String(entry.id) === String(logId))
                : null;
            if (!current) return;

            const status = current.status || 'sent';
            const success = Number(current.success_count || 0);
            const failure = Number(current.failure_count || 0);

            if (status !== initial.status || success !== initial.success || failure !== initial.failure) {
                location.reload();
            }
        } catch (error) {
            console.error('Log status check failed:', error);
        }
    }

    setTimeout(() => setInterval(checkLogStatus, 3000), 1000);
}
