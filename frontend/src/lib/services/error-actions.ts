/**
 * The actions an error notification offers.
 *
 * Everyone gets "Send report to support" (the HobbyCoders support report
 * dialog). "Fix with AI" opens a chat whose agent edits the app's code, so it
 * is offered only when `GET /support/capabilities` says this install runs from
 * a source checkout (`fix_with_ai: true`); unknown capabilities mean no.
 */
import type { NotificationAction } from '$lib/stores/notifications';
import { canFixWithAi, canSendSupportReport, openSupportReport } from '$lib/stores/support-report.svelte';
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
