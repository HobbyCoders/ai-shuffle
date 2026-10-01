import type { ChatMessage } from '$lib/stores/types';

/**
 * A reconnect after a dropped provider connection that is still under way:
 * the engine re-sent the request and no reply has arrived yet. Shown as a
 * short live row; once the reply arrives (or the turn ends) it is settled
 * and goes back to being transient like any other retry.
 */
export function isLiveConnectionDropRetry(message: ChatMessage): boolean {
	const data = message.systemData;
	return (
		(message.type === 'system' || message.role === 'system') &&
		message.systemSubtype === 'provider_retry' &&
		data?.cause === 'connection_dropped' &&
		!data?.exhausted &&
		!data?.settled
	);
}

/** Empty-response retries stay in the event history, not the transcript. */
export function isTransientProviderRetry(message: ChatMessage): boolean {
	return (
		(message.type === 'system' || message.role === 'system') &&
		message.systemSubtype === 'provider_retry' &&
		!message.systemData?.exhausted &&
		!isLiveConnectionDropRetry(message)
	);
}
