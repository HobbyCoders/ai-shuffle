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

		if (options?.onClick) {
			notification.onclick = () => {
				window.focus();
				notification.close();
				options.onClick?.();
			};
		}

		return notification;
	} catch (error) {
		console.error('Failed to show notification:', error);
		return null;
	}
}

/**
 * Show a notification for session completion
 *
 * @param sessionTitle - The title of the completed session
 * @param sessionId - The session ID (for navigation)
 * @param duration - How long the session took (in seconds)
 */
export function notifySessionComplete(
	sessionTitle: string | null,
	sessionId: string,
	duration?: number
): Notification | null {
	const title = 'Session Complete';
	let body = sessionTitle || 'Your AI session has completed';

	if (duration && duration > 60) {
		const minutes = Math.floor(duration / 60);
		body += ` (${minutes}m ${Math.round(duration % 60)}s)`;
	}

	return showNotification(title, {
		body,
		tag: `session-${sessionId}`,
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
