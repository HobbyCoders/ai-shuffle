import { describe, expect, it, vi } from 'vitest';
import { createResolveWithAgent, RESOLVE_AGENT_ACTION, RESOLVE_NOT_SENT, type ResolveOperations } from './git-resolve-with-agent';

function deferred<T>() {
	let resolve!: (v: T) => void;
	const promise = new Promise<T>((r) => { resolve = r; });
	return { promise, resolve };
}

/** A stand-in for the gitOperations store: one action, a token per operation. */
function fakeOperations() {
	let token = 1;
	let action: { disabledReason?: string | null; note?: string | null } = { disabledReason: null, note: null };
	const ops: ResolveOperations & { replace(): void; action: () => typeof action } = {
		get token() { return token; },
		isCurrent: (t) => t === token,
		setAction: vi.fn((id, patch) => { if (id === RESOLVE_AGENT_ACTION) action = { ...action, ...patch }; }),
		clear: vi.fn(() => { token++; }),
		replace() { token++; action = { disabledReason: null, note: null }; },
		action: () => action
	};
	return ops;
}

describe('Resolve with agent hand-off', () => {
	it('re-checks the originating chat after the sender waits for a restore', async () => {
		// Chat A while a restore is running; the user moves to chat B during it.
		let live = 'A';
		const reason = () => (live === 'A' ? null : 'Open the chat this sync ran in to hand it to the agent.');
		const delivered: string[] = [];
		const send = vi.fn(async (_prompt: string, options: { stillAtDestination: () => boolean }) => {
			await Promise.resolve();
			live = 'B'; // navigation ended the restore on another chat
			if (!options.stillAtDestination()) return false;
			delivered.push(live);
			return true;
		});
		const ops = fakeOperations();
		await createResolveWithAgent({ reason, send, operations: ops, onSent: vi.fn() }, 'merge it')();
		expect(delivered).toEqual([]);
		expect(ops.clear).not.toHaveBeenCalled();
		expect(ops.action().disabledReason).toMatch(/Open the chat/);
	});

	it('leaves a newer operation alone when a delayed send finishes', async () => {
		const pending = deferred<boolean>();
		const ops = fakeOperations();
		const onSent = vi.fn();
		const click = createResolveWithAgent({ reason: () => null, send: () => pending.promise, operations: ops, onSent }, 'p')();
		// The user dismisses the outcome and starts another Git operation.
		ops.replace();
		const newer = ops.token;
		pending.resolve(true);
		await click;
		expect(ops.clear).not.toHaveBeenCalled();
		expect(onSent).not.toHaveBeenCalled();
		expect(ops.isCurrent(newer)).toBe(true);
		expect(ops.action().disabledReason).toBeNull();
	});

	it('keeps the button usable after a failed send, with the failure shown as a note', async () => {
		const ops = fakeOperations();
		const send = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
		const onSent = vi.fn();
		const click = createResolveWithAgent({ reason: () => null, send, operations: ops, onSent }, 'p');
		await click();
		expect(ops.action()).toEqual({ disabledReason: null, note: RESOLVE_NOT_SENT });
		// Connection back: the retry goes out and closes the outcome.
		await click();
		expect(send).toHaveBeenCalledTimes(2);
		expect(ops.clear).toHaveBeenCalledOnce();
		expect(onSent).toHaveBeenCalledOnce();
	});

	it('treats a thrown send as not sent', async () => {
		const ops = fakeOperations();
		await createResolveWithAgent({ reason: () => null, send: () => Promise.reject(new Error('ws')), operations: ops, onSent: vi.fn() }, 'p')();
		expect(ops.action().note).toBe(RESOLVE_NOT_SENT);
	});

	it('refuses in place when the chat cannot take it at click time, and ignores a second click while sending', async () => {
		const ops = fakeOperations();
		const send = vi.fn();
		await createResolveWithAgent({ reason: () => 'Available when the current reply finishes.', send, operations: ops, onSent: vi.fn() }, 'p')();
		expect(send).not.toHaveBeenCalled();
		expect(ops.action().disabledReason).toBe('Available when the current reply finishes.');

		const pending = deferred<boolean>();
		const slow = vi.fn(() => pending.promise);
		const click = createResolveWithAgent({ reason: () => null, send: slow, operations: fakeOperations(), onSent: vi.fn() }, 'p');
		const first = click();
		await click();
		pending.resolve(true);
		await first;
		expect(slow).toHaveBeenCalledOnce();
	});
});
