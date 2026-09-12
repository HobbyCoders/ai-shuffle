import type { ChatMessage } from '$lib/stores/types';

/** Empty-response retries stay in the event history, not the transcript. */
export function isTransientProviderRetry(message: ChatMessage): boolean {
	return (
		(message.type === 'system' || message.role === 'system') &&
		message.systemSubtype === 'provider_retry' &&
		!message.systemData?.exhausted
	);
}
