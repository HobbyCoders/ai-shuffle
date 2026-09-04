/**
 * In-app notification sound hooks.
 *
 * Browser OS notifications are intentionally not requested or constructed here.
 * Session navigation remains an in-app concern: the notification bell dispatches
 * the `open-session` event handled by the main page.
 */

import { notificationSound } from './notificationSound';

export interface SessionCompleteInfo {
	sessionTitle?: string | null;
	sessionId: string;
	durationSeconds?: number;
	totalCostUsd?: number;
	tokensIn?: number;
	tokensOut?: number;
	model?: string;
}

/** Play the completion sound without creating an OS notification. */
export function notifySessionComplete(_info: SessionCompleteInfo): null {
	notificationSound.playComplete();
	return null;
}

/** Play the attention sound without creating an OS notification. */
export function notifyWaitingForResponse(
	type: 'permission' | 'question',
	_details?: {
		toolName?: string;
		questionCount?: number;
		sessionTitle?: string;
	}
): null {
	notificationSound.play(type);
	return null;
}
