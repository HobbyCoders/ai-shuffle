import { notify } from '$lib/stores/notifications';
import type { VoiceCallStoreState } from './voiceCall';

type CallOutcome = Pick<VoiceCallStoreState, 'status' | 'sessionId' | 'error' | 'notice'>;

/**
 * Reports how a call ended when it ends by itself (a failed start, a transport
 * error, the length cap). The call controls that showed the reason are gone by
 * then, so the reason has to appear where the user is looking.
 *
 * It is a page banner, never only a bell item. A call started from the
 * new-session composer creates its conversation first, so a start that fails
 * (microphone, provider, credential) leaves the user on that empty
 * conversation. With the reason only in the notification inbox, nothing on
 * screen said why no call opened. The banner stays until dismissed and is
 * kept in the inbox as well.
 *
 * Keyed on the message, so the same ending is reported once however often the
 * store ticks (level updates, captions).
 */
export function createCallOutcomeReporter(): (call: CallOutcome) => void {
	let last: string | null = null;
	return (call) => {
		const message = call.status === 'error' ? call.error : call.status === 'ended' ? call.notice : null;
		const key = message ? `${call.status}:${call.sessionId}:${message}` : null;
		if (key === last) return;
		last = key;
		if (!message) return;
		notify.banner(message, {
			severity: call.status === 'error' ? 'error' : 'warning',
			persist: true,
			session_id: call.sessionId ?? undefined,
		});
	};
}
