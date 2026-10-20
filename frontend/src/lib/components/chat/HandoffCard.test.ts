import { render } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import HandoffCard from './HandoffCard.svelte';
import type { HandoffEpisode } from '$lib/utils/handoff-episodes';
import type { HandoffProgress } from '$lib/utils/handoff-progress';

const episode = (over: Partial<HandoffEpisode>): HandoffEpisode => ({
	key: 'k', worker: 'DesktopWorker', by: 'agent', phase: 'starting', followUps: [], records: 1, ...over
});
const read = (over: Partial<HandoffEpisode>, progress: HandoffProgress | null = null) => {
	const { container } = render(HandoffCard, { props: { episode: episode(over), progress } });
	const card = container.querySelector('[data-testid="handoff-card"]')!;
	return {
		card,
		title: card.querySelector('.title')?.textContent?.trim(),
		detail: card.querySelector('.detail')?.textContent?.trim(),
		current: card.querySelector('[aria-current="step"]')?.textContent?.trim(),
		step: card.querySelector('[data-testid="handoff-progress"] .step')?.textContent?.trim(),
		clock: card.querySelector('[data-testid="handoff-progress"] time')?.textContent?.trim(),
		slow: card.querySelector('.slow')?.textContent?.trim()
	};
};
const at = (step: string, elapsed: number, stepElapsed: number): HandoffProgress => ({
	step, elapsed_seconds: elapsed, step_elapsed_seconds: stepElapsed
});

describe('HandoffCard', () => {
	it('says what it is doing in each phase', () => {
		expect(read({ phase: 'starting' })).toMatchObject({ title: 'Starting hand-off to DesktopWorker', current: 'Queued' });
		expect(read({ phase: 'sending', sent: true })).toMatchObject({ title: 'Sending to DesktopWorker', current: 'Sending' });
		const done = read({ phase: 'done', sent: true, outcome: 'Handed off to DesktopWorker. This chat continues there on the same branch.' });
		expect(done.title).toBe('Handed off to DesktopWorker');
		expect(done.detail).toBe('This chat continues there on the same branch.');
		expect(done.current).toBeUndefined();
	});

	it('settles on the failure reason without repeating the title', () => {
		const failed = read({ phase: 'failed', sent: true, outcome: 'Hand-off to DesktopWorker failed: push rejected' });
		expect(failed.title).toBe('Hand-off to DesktopWorker failed');
		expect(failed.detail).toBe('push rejected');
		expect(failed.card.getAttribute('data-tone')).toBe('failed');
		expect(read({ phase: 'failed', reason: 'timeout', outcome: 'x' }).title).toBe('Hand-off to DesktopWorker timed out');
	});

	it("words the owner's move as a move", () => {
		expect(read({ by: 'owner', phase: 'starting', afterQueued: true })).toMatchObject({
			title: 'Starting move to DesktopWorker',
			detail: 'Waits for the queued messages, this turn and background work to finish.'
		});
		expect(read({ by: 'owner', phase: 'done' }).title).toBe('Moved to DesktopWorker');
	});

	it('announces phase changes politely and shows the note and follow-ups', () => {
		const { card } = read({ phase: 'done', note: 'Finish the review', followUps: ['DesktopWorker did not take 1 message.'] });
		expect(card.querySelector('[role="status"]')?.getAttribute('aria-live')).toBe('polite');
		expect(card.querySelector('.note-text')?.textContent).toBe('Finish the review');
		expect(card.querySelector('.follow-ups')?.textContent).toContain('did not take 1 message');
	});

	it('shows a cancel on its way without settling', () => {
		const cancelling = read({ phase: 'sending', sent: true, cancelling: true });
		expect(cancelling.title).toBe('Sending to DesktopWorker');
		expect(cancelling.detail).toContain('Cancelling');
		expect(cancelling.card.querySelector('.badge')?.textContent).toBe('cancelling');
	});

	describe('receiver progress', () => {
		afterEach(() => vi.useRealTimers());

		it('keeps the sending detail while progress is null (before the first receiver poll)', () => {
			const card = read({ phase: 'sending', sent: true, id: 'X' }, null);
			expect(card.detail).toBe('Committing, pushing and moving this chat.');
			expect(card.card.querySelector('[data-testid="handoff-progress"]')).toBeNull();
			expect(card.current).toBe('Sending');
		});

		it('shows the step in plain words and the elapsed time, never a percentage', () => {
			const card = read({ phase: 'sending', sent: true, id: 'X' }, at('install_history.fluid_session_nodes', 23.4, 5.6));
			expect(card.title).toBe('Sending to DesktopWorker');
			expect(card.detail).toBeUndefined();
			expect(card.step).toBe('Copying transcript');
			expect(card.clock).toBe('0:23');
			expect(card.slow).toBeUndefined();
			expect(card.card.textContent).not.toContain('%');
			expect(card.card.querySelector('[data-testid="handoff-progress"] .step')?.getAttribute('title')).toBe('install_history.fluid_session_nodes');
			expect(card.card.getAttribute('data-tone')).toBe('live');
		});

		it('names the target on the last step and falls back for unknown steps', () => {
			expect(read({ phase: 'sending', sent: true }, at('accept_owner', 80, 1)).step).toBe('Starting on DesktopWorker');
			expect(read({ phase: 'sending', sent: true }, at('warp.drive', 3, 1)).step).toBe('Moving the chat');
		});

		it('says it is still working after a long step, without turning into an error', () => {
			const card = read({ phase: 'sending', sent: true }, at('install_history.session_events', 95, 72));
			expect(card.slow).toBe('Still working. Long chats can take a few minutes here.');
			expect(card.clock).toBe('1:35');
			expect(card.card.getAttribute('data-tone')).toBe('live');
			expect(card.card.querySelector('.badge')?.textContent).toBe('sending');
		});

		it('ticks the clock between reads', async () => {
			vi.useFakeTimers();
			const { container } = render(HandoffCard, { props: { episode: episode({ phase: 'sending', sent: true }), progress: at('decode', 10, 2) } });
			const clock = () => container.querySelector('[data-testid="handoff-progress"] time')?.textContent;
			expect(clock()).toBe('0:10');
			await vi.advanceTimersByTimeAsync(3_000);
			expect(clock()).toBe('0:13');
			// …but not forever when no new report comes.
			await vi.advanceTimersByTimeAsync(10 * 60_000);
			expect(clock()).toBe('0:25');
		});

		it('does not snap back when the same report is read again', async () => {
			vi.useFakeTimers();
			const first = at('decode', 10, 2);
			const { container, rerender } = render(HandoffCard, { props: { episode: episode({ phase: 'sending', sent: true }), progress: first } });
			const clock = () => container.querySelector('[data-testid="handoff-progress"] time')?.textContent;
			await vi.advanceTimersByTimeAsync(3_000);
			expect(clock()).toBe('0:13');
			// A poll hands the card a new object with the same values.
			await rerender({ episode: episode({ phase: 'sending', sent: true }), progress: { ...first } });
			expect(clock()).toBe('0:13');
			await vi.advanceTimersByTimeAsync(1_000);
			expect(clock()).toBe('0:14');
		});

		it('leaves settled, queued and cancelling states as they were', () => {
			const p = at('install_history', 40, 30);
			expect(read({ phase: 'starting' }, p).step).toBeUndefined();
			const done = read({ phase: 'done', sent: true, outcome: 'Handed off to DesktopWorker. Continues there.' }, p);
			expect(done.step).toBeUndefined();
			expect(done.detail).toBe('Continues there.');
			expect(read({ phase: 'failed', sent: true, outcome: 'Hand-off to DesktopWorker failed: boom' }, p).step).toBeUndefined();
			const cancelling = read({ phase: 'sending', sent: true, cancelling: true }, p);
			expect(cancelling.detail).toContain('Cancelling');
			expect(cancelling.step).toBeUndefined();
			expect(cancelling.card.querySelector('.badge')?.textContent).toBe('cancelling');
		});
	});
});
