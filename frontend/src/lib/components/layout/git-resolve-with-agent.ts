import type { GitOperationAction } from '$lib/stores/gitOperations.svelte';

/** The action id "Resolve with agent" uses in the Git panel's outcome. */
export const RESOLVE_AGENT_ACTION = 'resolve-agent';

/** Shown under the button when the hand-off message did not go out. */
export const RESOLVE_NOT_SENT = "The message wasn't sent. Try again.";

/** The parts of the gitOperations store the hand-off touches. */
export interface ResolveOperations {
	readonly token: number;
	isCurrent(token: number): boolean;
	setAction(id: string, patch: Partial<Omit<GitOperationAction, 'id'>>): void;
	clear(): void;
}

export interface ResolveWithAgentDeps {
	/** Why the originating chat can't take the message right now, or null. */
	reason: () => string | null;
	/** Sends the prompt to the live chat (activeSession.sendMessage). */
	send: (prompt: string, options: { stillAtDestination: () => boolean }) => Promise<boolean>;
	operations: ResolveOperations;
	/** Runs once the message went out and its outcome is still on screen. */
	onSent: () => void;
}

/**
 * The click handler for "Resolve with agent". Three races it guards:
 *  - the send can wait for a session restore or a reconnect; the user may
 *    switch chats meanwhile, so the sender re-checks the originating chat
 *    after that wait (`stillAtDestination`) instead of sending to whatever
 *    chat is live by then;
 *  - the outcome may be dismissed or replaced by a newer Git operation while
 *    the send waits; then the result touches nothing (it would otherwise
 *    clear the newer operation and drop its resume handle);
 *  - a failed send is reported as a note, not a disabled reason, so the
 *    button stays usable for the retry once the connection is back.
 */
export function createResolveWithAgent(deps: ResolveWithAgentDeps, prompt: string): () => Promise<void> {
	let sending = false;
	return async () => {
		if (sending) return;
		const { operations } = deps;
		const token = operations.token;
		const blocked = deps.reason();
		if (blocked) {
			operations.setAction(RESOLVE_AGENT_ACTION, { disabledReason: blocked, note: null });
			return;
		}
		sending = true;
		operations.setAction(RESOLVE_AGENT_ACTION, { disabledReason: 'Sending…', note: null });
		let sent = false;
		try {
			sent = await deps.send(prompt, { stillAtDestination: () => deps.reason() === null });
		} catch {
			sent = false;
		} finally {
			sending = false;
		}
		if (!operations.isCurrent(token)) return;
		if (sent) {
			operations.clear();
			deps.onSent();
			return;
		}
		operations.setAction(RESOLVE_AGENT_ACTION, { disabledReason: deps.reason(), note: RESOLVE_NOT_SENT });
	};
}
