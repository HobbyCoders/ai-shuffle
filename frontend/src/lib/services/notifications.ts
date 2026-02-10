/**
 * Browser Notifications Service
 *
 * Handles requesting permission and showing browser notifications
 * for long-running task completion.
 *
 * Notifications are always active when permission is granted — no toggle needed.
 * Permission is requested automatically when visiting the main page.
 */

function isSupported(): boolean {
	if (typeof window === 'undefined') return false;
	return 'Notification' in window;
}

async function requestPermission(): Promise<NotificationPermission | 'unsupported'> {
	if (!isSupported()) {
		return 'unsupported';
	}

	try {
		return await Notification.requestPermission();
	} catch (error) {
		console.error('Failed to request notification permission:', error);
		return 'denied';
	}
}

/**
 * Request notification permission if not yet determined.
 * Called automatically on main page visit.
 */
export async function requestPermissionIfNeeded(): Promise<void> {
	// Clean up legacy toggle preference from localStorage
	if (typeof localStorage !== 'undefined') {
		localStorage.removeItem('notifications_enabled');
	}

	if (!isSupported()) return;
	if (Notification.permission === 'default') {
		await requestPermission();
	}
}

/**
 * Show a browser notification.
 * Returns null silently if notifications are unsupported or permission is not granted.
 */
function showNotification(
	title: string,
	options?: {
		body?: string;
		icon?: string;
		tag?: string;
		requireInteraction?: boolean;
		onClick?: () => void;
	}
): Notification | null {
	if (!isSupported() || Notification.permission !== 'granted') {
		return null;
	}

	try {
		const notification = new Notification(title, {
			body: options?.body,
			icon: options?.icon || '/favicon.svg',
			tag: options?.tag,
			requireInteraction: options?.requireInteraction || false
		});

		notification.onclick = () => {
			window.focus();
			notification.close();
			options?.onClick?.();
		};

		return notification;
	} catch (error) {
		console.error('Failed to show notification:', error);
		return null;
	}
}

export interface SessionCompleteInfo {
	sessionTitle?: string | null;
	sessionId: string;
	durationSeconds?: number;
	totalCostUsd?: number;
	tokensIn?: number;
	tokensOut?: number;
	model?: string;
}

function formatDuration(seconds: number): string {
	if (seconds < 60) {
		return `${Math.round(seconds)}s`;
	}
	const minutes = Math.floor(seconds / 60);
	const secs = Math.round(seconds % 60);
	if (minutes < 60) {
		return secs > 0 ? `${minutes}m ${secs}s` : `${minutes}m`;
	}
	const hours = Math.floor(minutes / 60);
	const mins = minutes % 60;
	return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`;
}

function formatCost(cost: number): string {
	if (cost < 0.01) {
		return `$${cost.toFixed(4)}`;
	}
	if (cost < 1) {
		return `$${cost.toFixed(3)}`;
	}
	return `$${cost.toFixed(2)}`;
}

/**
 * Show a notification for session completion.
 */
export function notifySessionComplete(info: SessionCompleteInfo): Notification | null {
	const { sessionTitle, sessionId, durationSeconds, totalCostUsd } = info;

	const details: string[] = [];
	if (durationSeconds !== undefined && durationSeconds > 0) {
		details.push(formatDuration(durationSeconds));
	}
	if (totalCostUsd !== undefined && totalCostUsd > 0) {
		details.push(formatCost(totalCostUsd));
	}

	const detailStr = details.join(' \u00b7 ');
	let body: string;
	if (sessionTitle && detailStr) {
		body = `${sessionTitle} (${detailStr})`;
	} else if (sessionTitle) {
		body = sessionTitle;
	} else if (detailStr) {
		body = `Completed in ${detailStr}`;
	} else {
		body = 'Your AI session has completed';
	}

	return showNotification('Session Complete', {
		body,
		tag: `session-${sessionId}-${Date.now()}`,
		onClick: () => {
			window.location.hash = '';
		}
	});
}

/**
 * Show a notification when AI is waiting for user response (permission or question).
 * Only shows if the window is not focused (user is away).
 */
export function notifyWaitingForResponse(
	type: 'permission' | 'question',
	details?: {
		toolName?: string;
		questionCount?: number;
		sessionTitle?: string;
	}
): Notification | null {
	if (typeof document !== 'undefined' && document.hasFocus()) {
		return null;
	}

	const title = type === 'permission'
		? 'Permission Required'
		: 'Claude Has a Question';

	let body: string;
	if (type === 'permission') {
		body = details?.toolName
			? `AI wants to use: ${details.toolName}`
			: 'AI is waiting for permission to proceed';
	} else {
		const count = details?.questionCount || 1;
		body = count > 1
			? `AI has ${count} questions for you`
			: 'AI needs your input to continue';
	}

	return showNotification(title, {
		body,
		tag: `waiting-${type}-${Date.now()}`,
		requireInteraction: true,
		onClick: () => {
			window.focus();
		}
	});
}
