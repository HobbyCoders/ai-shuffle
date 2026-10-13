import type { ChatMessage } from '$lib/stores/types';

function isConnectionDropRetry(message: ChatMessage): boolean {
	return (
		(message.type === 'system' || message.role === 'system') &&
		message.systemSubtype === 'provider_retry' &&
		message.systemData?.cause === 'connection_dropped'
	);
}

/**
 * A reconnect after a dropped provider connection that is still under way:
 * the engine re-sent the request and no reply has arrived yet. Shown as a
 * short live row; once the reply arrives (or the turn ends) it is settled.
 */
export function isLiveConnectionDropRetry(message: ChatMessage): boolean {
	const data = message.systemData;
	return isConnectionDropRetry(message) && !data?.exhausted && !data?.settled;
}

/**
 * The provider failed before sending any reply and the request was re-sent:
 * an error sent in place of content (`provider_error`), or only heartbeats for
 * too long (`no_content`). The wait before it can be minutes, so the card
 * stays in the transcript as the record of where that time went (#1992).
 */
export function isPreContentRetry(message: ChatMessage): boolean {
	const cause = message.systemData?.cause;
	return (
		(message.type === 'system' || message.role === 'system') &&
		message.systemSubtype === 'provider_retry' &&
		(cause === 'provider_error' || cause === 'no_content')
	);
}

/** A pre-content retry whose re-sent request has not answered yet. */
export function isLivePreContentRetry(message: ChatMessage): boolean {
	const data = message.systemData;
	return isPreContentRetry(message) && !data?.exhausted && !data?.settled;
}

/** What a dropped attempt had streamed before its retry superseded it. */
export interface SupersededAttempt {
	text: string;
	tools: string[];
}

/** The dropped attempt's output a connection-drop retry card holds, if any. */
export function supersededAttempt(message: ChatMessage): SupersededAttempt | null {
	if (!isConnectionDropRetry(message)) return null;
	const raw = message.systemData?.superseded as Partial<SupersededAttempt> | undefined;
	if (!raw) return null;
	const text = typeof raw.text === 'string' ? raw.text : '';
	const tools = Array.isArray(raw.tools) ? raw.tools.map(String) : [];
	return text || tools.length ? { text, tools } : null;
}

/**
 * Retries stay in the event history, not the transcript: empty-response
 * retries always, and a connection-drop retry once its reply arrived, unless
 * it holds the output of the attempt it replaced. A pre-content retry always
 * stays in the transcript.
 */
export function isTransientProviderRetry(message: ChatMessage): boolean {
	return (
		(message.type === 'system' || message.role === 'system') &&
		message.systemSubtype === 'provider_retry' &&
		!message.systemData?.exhausted &&
		!isLiveConnectionDropRetry(message) &&
		!isPreContentRetry(message) &&
		!supersededAttempt(message)
	);
}
