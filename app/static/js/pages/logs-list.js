const logListStatusMeta = document.getElementById('logListStatusMeta');
let processingLogs = [];
if (logListStatusMeta && logListStatusMeta.dataset.processingLogs) {
    try {
        processingLogs = JSON.parse(logListStatusMeta.dataset.processingLogs);
    } catch (error) {
        processingLogs = [];
    }
}

const processingIds = processingLogs.map((log) => log.id).filter(Boolean);
const baseline = new Map(
    processingLogs.map((log) => [
        String(log.id),
        {
            status: log.status || 'sent',
            success: Number(log.success_count || 0),
            failure: Number(log.failure_count || 0),
        },
    ])
);

async function checkLogListStatus() {
    if (!processingIds.length) return;
    try {
        const response = await fetch(`/logs/status?ids=${processingIds.join(',')}`);
        if (!response.ok) return;
        const payload = await response.json();
        const logs = Array.isArray(payload.logs) ? payload.logs : [];
        const currentMap = new Map(
            logs.map((log) => [
                String(log.id),
                {
                    status: log.status || 'sent',
                    success: Number(log.success_count || 0),
                    failure: Number(log.failure_count || 0),
                },
            ])
        );

        for (const [id, initial] of baseline.entries()) {
            const current = currentMap.get(id);
            if (!current) {
                location.reload();
                return;
            }
            if (
                current.status !== initial.status ||
                current.success !== initial.success ||
                current.failure !== initial.failure
            ) {
                location.reload();
                return;
            }
        }
    } catch (error) {
        console.error('Log list status check failed:', error);
    }
}

if (processingIds.length > 0) {
    setTimeout(() => setInterval(checkLogListStatus, 3000), 1000);
}
