/**
 * The actions an error notification offers.
 *
 * Everyone gets "Send report to support" (the HobbyCoders support report
 * dialog). "Fix with AI" opens a chat whose agent edits the app's code, so it
 * is offered only when `GET /support/capabilities` says this install runs from
 * a source checkout (`fix_with_ai: true`); unknown capabilities mean no.
 */
import type { HistoryActions, NotificationAction, ServerNotificationRow } from '$lib/stores/notifications';
import {
	canFixWithAi,
	canSendSupportReport,
	openSupportReport,
	supportCaps
} from '$lib/stores/support-report.svelte';
import type { SupportErrorInput } from '$lib/api/support';
import { startFixChat, type FixWithAiError } from './fix-with-ai';

export const SEND_REPORT_LABEL = 'Send report to support';

const SOURCES = new Set(['frontend', 'backend', 'ws', 'api']);

/** The report API's error shape (drops fields it does not take, like session_id). */
export function toSupportError(err: FixWithAiError): SupportErrorInput {
	const out: SupportErrorInput = { message: err.message };
	if (err.source && SOURCES.has(err.source)) out.source = err.source as SupportErrorInput['source'];
	if (err.detail) out.detail = err.detail;
	if (err.url) out.url = err.url;
	if (typeof err.status_code === 'number') out.status_code = err.status_code;
	return out;
}

export function errorActions(err: FixWithAiError): {
	action?: NotificationAction;
	secondaryAction?: NotificationAction;
} {
	const report: NotificationAction | undefined = canSendSupportReport()
		? { label: SEND_REPORT_LABEL, onClick: () => openSupportReport({ error: toSupportError(err) }) }
		: undefined;
	const fix: NotificationAction | undefined = canFixWithAi()
		? { label: 'Fix with AI', onClick: () => startFixChat(err) }
		: undefined;
	return report ? { action: report, secondaryAction: fix } : { action: fix };
}

/**
 * The actions a server notification gets back on this side. Closures cannot
 * cross the wire, so both the live `notification` WS event and the history
 * loaded after a reload rebuild them here, keyed on `category`: an
 * `api_error` that is not a warning gets "Send report to support" (and "Fix
 * with AI" on source checkouts). A warning (a 429) gets none.
 */
export function serverNotificationActions(
	n: Pick<ServerNotificationRow, 'category' | 'severity' | 'message' | 'session_id' | 'action'>
): { action?: NotificationAction; secondaryAction?: NotificationAction } {
	if (n.category !== 'api_error' || n.severity === 'warning') return {};
	const payload = n.action?.payload ?? {};
	return errorActions({
		source: 'ws',
		message: n.message,
		status_code: typeof payload.status_code === 'number' ? payload.status_code : undefined,
		session_id: n.session_id ?? (typeof payload.session_id === 'string' ? payload.session_id : undefined)
	});
}

/**
 * Rebuilds actions for notifications loaded from history. Waits for the
 * support capabilities first, so a login that may not send reports never
 * sees the button while they are still loading.
 */
export const historyNotificationActions: HistoryActions = {
	ready: () => supportCaps.load(),
	build: serverNotificationActions
};
