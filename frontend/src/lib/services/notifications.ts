/**
 * Browser Notifications Service
 *
 * Handles requesting permission and showing browser notifications
 * for long-running task completion.
 */

// Notification permission state
let permissionState: NotificationPermission | 'unsupported' = 'default';

/**
 * Check if browser notifications are supported
 */
export function isSupported(): boolean {
	return 'Notification' in window;
}

/**
 * Get current notification permission state
 */
export function getPermission(): NotificationPermission | 'unsupported' {
	if (!isSupported()) {
		return 'unsupported';
	}
	return Notification.permission;
}

/**
 * Request notification permission from the user
 * Returns the permission state after the request
 */
export async function requestPermission(): Promise<NotificationPermission | 'unsupported'> {
	if (!isSupported()) {
		permissionState = 'unsupported';
		return permissionState;
	}

	try {
		const result = await Notification.requestPermission();
		permissionState = result;
		return result;
	} catch (error) {
		console.error('Failed to request notification permission:', error);
		return 'denied';
	}
}

/**
 * Show a browser notification
 *
 * @param title - Notification title
 * @param options - Notification options (body, icon, etc.)
 * @returns The Notification object if successful, null otherwise
 */
export function showNotification(
	title: string,
	options?: {
		body?: string;
		icon?: string;
		tag?: string;
		requireInteraction?: boolean;
		onClick?: () => void;
	}
): Notification | null {
	if (!isSupported()) {
		console.warn('Browser notifications not supported');
		return null;
	}

	if (Notification.permission !== 'granted') {
		console.warn('Notification permission not granted');
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

/**
 * Format duration in a human-readable way
 */
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

/**
 * Format cost in a readable way
 */
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
 * Show a notification for session completion
 *
 * @param info - Session completion information including title, duration, cost, etc.
 */
export function notifySessionComplete(info: SessionCompleteInfo): Notification | null {
	const { sessionTitle, sessionId, durationSeconds, totalCostUsd } = info;

	const title = 'Session Complete';

	// Build body with session title, duration, and cost
	const parts: string[] = [];

	if (sessionTitle) {
		parts.push(sessionTitle);
	}

	const details: string[] = [];
	if (durationSeconds !== undefined && durationSeconds > 0) {
		details.push(formatDuration(durationSeconds));
	}
	if (totalCostUsd !== undefined && totalCostUsd > 0) {
		details.push(formatCost(totalCostUsd));
	}

	let body: string;
	if (parts.length > 0 && details.length > 0) {
		body = `${parts.join('')} (${details.join(' · ')})`;
	} else if (parts.length > 0) {
		body = parts.join('');
	} else if (details.length > 0) {
		body = `Completed in ${details.join(' · ')}`;
	} else {
		body = 'Your AI session has completed';
	}

	return showNotification(title, {
		body,
		tag: `session-${sessionId}-${Date.now()}`,
		onClick: () => {
			// Navigate to the session
			window.location.hash = '';
			// The session should already be active if using WebSocket
		}
	});
}

/**
 * Show a notification for session error
 *
 * @param sessionTitle - The title of the session
 * @param errorMessage - Brief error description
 */
export function notifySessionError(
	sessionTitle: string | null,
	errorMessage?: string
): Notification | null {
	const title = 'Session Error';
	const body = errorMessage || `Error in session: ${sessionTitle || 'Unknown session'}`;

	return showNotification(title, {
		body,
		requireInteraction: true
	});
}

/**
 * Show a notification when AI is waiting for user response (permission or question)
 * Only shows if window is not focused (user is away)
 *
 * @param type - Type of waiting notification ('permission' or 'question')
 * @param details - Additional context about what's being requested
 */
export function notifyWaitingForResponse(
	type: 'permission' | 'question',
	details?: {
		toolName?: string;
		questionCount?: number;
		sessionTitle?: string;
	}
): Notification | null {
	// Don't notify if window is focused - user can already see the request
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
		requireInteraction: true, // Keep notification visible until user interacts
		onClick: () => {
			// Focus the window when notification is clicked
			window.focus();
		}
	});
}

/**
 * Check if notifications are enabled in localStorage
 */
export function isNotificationsEnabled(): boolean {
	if (typeof localStorage === 'undefined') return false;
	return localStorage.getItem('notifications_enabled') === 'true';
}

/**
 * Set notifications enabled state in localStorage
 */
export function setNotificationsEnabled(enabled: boolean): void {
	if (typeof localStorage === 'undefined') return;
	localStorage.setItem('notifications_enabled', enabled ? 'true' : 'false');
}

/**
 * Initialize notifications (call on app start)
 * Returns true if notifications are ready to use
 */
export async function initializeNotifications(): Promise<boolean> {
	if (!isSupported()) {
		return false;
	}

	// If user has enabled notifications but hasn't granted permission yet, request it
	if (isNotificationsEnabled() && Notification.permission === 'default') {
		await requestPermission();
	}

	return Notification.permission === 'granted' && isNotificationsEnabled();
}
