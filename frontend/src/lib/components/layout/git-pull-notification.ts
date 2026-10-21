import type { PullResponse } from '$lib/api/git';
import type { NotificationSeverity } from '$lib/stores/notifications';

/** Keep backend recovery details intact; a completed pull can still need attention. */
export function pullNotification(response: Pick<PullResponse, 'success' | 'message' | 'warning'>): {
    message: string;
    severity: NotificationSeverity;
} {
    if (!response.success) {
        return { message: response.message || 'Pull stopped. Nothing was changed.', severity: 'error' };
    }
    if (response.warning) {
        return { message: response.message || 'Pull completed, but stashed changes could not be restored.', severity: 'warning' };
    }
    return { message: response.message || 'Pulled latest changes', severity: 'success' };
}
