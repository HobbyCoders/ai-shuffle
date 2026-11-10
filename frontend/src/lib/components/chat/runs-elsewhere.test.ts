import { afterEach, expect, it, vi } from 'vitest';

import { askRunsElsewhere, notesGoAsReferences } from './runs-elsewhere';

const open = { sessionId: 's1', deviceIntent: null };
const never = () => { throw new Error('no request expected'); };

afterEach(() => {
	vi.unstubAllGlobals();
	vi.useRealTimers();
});

it('follows the "Runs on" pick for a new chat, asking nothing', () => {
	expect(notesGoAsReferences({ sessionId: null, deviceIntent: null }, false, never)).toBe(false);
	expect(notesGoAsReferences({ sessionId: null, deviceIntent: { id: 'dev_b', name: 'B' } }, false, never)).toBe(true);
});

it('sends references for a message held for a queued move, asking nothing', () => {
	expect(notesGoAsReferences(open, true, never)).toBe(true);
});

it('sends paths to an open chat only when the server says it runs here', async () => {
	expect(await notesGoAsReferences(open, false, async () => false)).toBe(false);
	expect(await notesGoAsReferences(open, false, async () => true)).toBe(true);
	expect(await notesGoAsReferences(open, false, async () => null)).toBe(true);
});

function answer(body: unknown, status = 200) {
	const fetch = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }));
	vi.stubGlobal('fetch', fetch);
	return fetch;
}

it('asks /live afresh for the owner fields only', async () => {
	const fetch = answer({ session_meta: { is_remote: false } });
	expect(await askRunsElsewhere('s 1')).toBe(false);
	expect(String(fetch.mock.calls[0][0])).toContain('/sessions/s%201/live?include_messages=false&owner_fields=true');
	expect(fetch.mock.calls[0][1]).toMatchObject({ cache: 'no-store' });
	answer({ session_meta: { is_remote: true } });
	expect(await askRunsElsewhere('s1')).toBe(true);
});

it('has no answer for an error, a refusal, an old worker or no network', async () => {
	answer({ detail: 'x' }, 500);
	expect(await askRunsElsewhere('s1')).toBeNull();
	answer({ detail: 'Forbidden' }, 403);
	expect(await askRunsElsewhere('s1')).toBeNull();
	answer({ session_meta: { title: 'Plan' } });
	expect(await askRunsElsewhere('s1')).toBeNull();
	vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch'); }));
	expect(await askRunsElsewhere('s1')).toBeNull();
});

it('gives up, and aborts the request, at the time limit', async () => {
	vi.useFakeTimers();
	let signal: AbortSignal | undefined;
	vi.stubGlobal('fetch', vi.fn((_: unknown, init?: RequestInit) => {
		signal = init?.signal ?? undefined;
		return new Promise<Response>(() => {});
	}));
	const asked = askRunsElsewhere('s1', 2000);
	await vi.advanceTimersByTimeAsync(1999);
	expect(signal?.aborted).toBe(false);
	await vi.advanceTimersByTimeAsync(1);
	expect(await asked).toBeNull();
	expect(signal?.aborted).toBe(true);
});
