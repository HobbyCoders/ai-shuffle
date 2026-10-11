import { render } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import HandoffCard from './HandoffCard.svelte';
import type { HandoffEpisode } from '$lib/utils/handoff-episodes';

const episode = (over: Partial<HandoffEpisode>): HandoffEpisode => ({
	key: 'k', worker: 'DesktopWorker', by: 'agent', phase: 'starting', followUps: [], records: 1, ...over
});
const read = (over: Partial<HandoffEpisode>) => {
	const { container } = render(HandoffCard, { props: { episode: episode(over) } });
	const card = container.querySelector('[data-testid="handoff-card"]')!;
	return {
		card,
		title: card.querySelector('.title')?.textContent?.trim(),
		detail: card.querySelector('.detail')?.textContent?.trim(),
		current: card.querySelector('[aria-current="step"]')?.textContent?.trim()
	};
};

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
});
