/** Stop must release automatic following before any queued layout callback can
 * reveal the taller Not-sent rows. Execute the rendered component's own code. */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const source = readFileSync(resolve(process.cwd(), 'src/lib/components/chat/MessageArea.svelte'), 'utf8');
const start = source.indexOf('async function stayWhereNotSentRowsLeftTheReader()');
const stop = source.indexOf('const notSentRowsArrival =', start);
if (start < 0 || stop < 0) throw new Error('Missing Stop reading-position implementation');
const transaction = source.slice(start, stop);

function probe({ gap = 240, error = null as string | null } = {}) {
	const env = { gap, error, ticks: [] as (() => void)[], calls: [] as string[] };
	const create = new Function('env', `
		let $activeSession = { sessionId: 'stopped', error: env.error };
		let shouldAutoScroll = true, pendingScrollToBottom = false, windowShiftVersion = 0;
		const tick = () => new Promise(resolve => env.ticks.push(resolve));
		const cancelCurrentTurnAnchor = () => env.calls.push('cancel');
		const refreshLatestTurnTailReserve = () => env.calls.push('reserve');
		const followGrowthNow = () => env.calls.push('follow');
		const updateJumpToLatestVisibility = () => {};
		const updateCurrentTurnIndex = () => {};
		const scheduleReadingAnchorCapture = () => env.calls.push('reader');
		const readScrollMetrics = () => ({ scrollTop: 1000, clientHeight: 600, scrollHeight: 1600 + env.gap });
		${transaction}
		return {
			stop: () => stayWhereNotSentRowsLeftTheReader(),
			following: () => shouldAutoScroll,
			switchSession() { $activeSession = { sessionId: 'other', error: null }; shouldAutoScroll = true; }
		};
	`);
	const state = create(env) as { stop(): Promise<void>; following(): boolean; switchSession(): void };
	return { env, ...state, async flush() {
		for (let i = 0; i < 3; i++) { env.ticks.splice(0).forEach(resolve => resolve()); await Promise.resolve(); }
	} };
}

describe('MessageArea Stop reading-position transaction', () => {
	it('pauses following before its first layout tick and keeps overflowing rows below the reader', async () => {
		const p = probe();
		const stopped = p.stop();
		expect(p.following()).toBe(false);
		await p.flush(); await stopped;
		expect(p.following()).toBe(false);
		expect(p.env.calls).not.toContain('follow');
		expect(p.env.calls).toContain('reader');
	});

	it('resumes following when the completed rows fit at the same bottom position', async () => {
		const p = probe({ gap: 0 });
		const stopped = p.stop();
		expect(p.following()).toBe(false);
		await p.flush(); await stopped;
		expect(p.following()).toBe(true);
	});

	it('does not pause an already visible error surface being followed through Stop', async () => {
		const p = probe({ error: 'Already visible failure' });
		const stopped = p.stop();
		expect(p.following()).toBe(true);
		await p.flush(); await stopped;
		expect(p.env.calls).toContain('follow');
	});

	it('does not release the incoming chat when old Stop layout work resolves', async () => {
		const p = probe();
		const stopped = p.stop();
		p.switchSession();
		await p.flush(); await stopped;
		expect(p.following()).toBe(true);
		expect(p.env.calls).toEqual(['cancel']);
	});
});
