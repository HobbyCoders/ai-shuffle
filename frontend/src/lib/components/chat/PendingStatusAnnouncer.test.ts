/**
 * PendingStatusAnnouncer is the single screen-reader channel for unsend
 * verdicts (#2275). It sits outside the transcript log, so it must announce
 * each transition itself — including a withdrawal, whose chip disappears.
 */
import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import PendingStatusAnnouncer from './PendingStatusAnnouncer.svelte';
import type { QueuedMessage } from '$lib/stores/types';
import { pendingQueue } from '$lib/stores/session/pending-queue';

const msg = (id: string, status?: QueuedMessage['status']): QueuedMessage => ({
	id,
	content: id,
	queuedAt: 1,
	status
});

afterEach(() => cleanup());

function setup(initial: QueuedMessage[], sessionId = 's1') {
	const view = render(PendingStatusAnnouncer, { props: { messages: initial, sessionId } });
	const region = view.container.querySelector('[role="status"]') as HTMLElement;
	return { ...view, region, text: () => region.textContent?.replace(/ /g, '') };
}

describe('PendingStatusAnnouncer', () => {
	it('is one persistent polite region that starts silent', () => {
		const { region, text } = setup([msg('a')]);
		expect(region.getAttribute('aria-live')).toBe('polite');
		expect(region.classList.contains('sr-only')).toBe(true);
		expect(text()).toBe('');
	});

	it('announces cancelling, then withdrawn when the chip is removed', async () => {
		const { rerender, region, text } = setup([msg('a'), msg('b')]);
		await rerender({ messages: [msg('a'), msg('b', 'cancelling')], sessionId: 's1' });
		expect(text()).toBe('Cancelling message…');
		await rerender({ messages: [msg('a')], sessionId: 's1' });
		expect(text()).toBe('Message withdrawn');
		expect(region.isConnected).toBe(true);
	});

	it('announces cancelling, then already sent when delivery wins', async () => {
		const { rerender, text } = setup([msg('a')]);
		await rerender({ messages: [msg('a', 'cancelling')], sessionId: 's1' });
		expect(text()).toBe('Cancelling message…');
		await rerender({ messages: [msg('a', 'delivered')], sessionId: 's1' });
		expect(text()).toBe('Message already sent');
	});

	it('re-announces identical words by changing the node text', async () => {
		const { rerender, region } = setup([msg('a'), msg('b')]);
		await rerender({ messages: [msg('a', 'cancelling'), msg('b')], sessionId: 's1' });
		const first = region.textContent;
		await rerender({ messages: [msg('a', 'cancelling'), msg('b', 'cancelling')], sessionId: 's1' });
		expect(region.textContent).not.toBe(first);
		expect(region.textContent?.trim()).toBe('Cancelling message…');
	});

	it('stays silent on ordinary queue churn and on a session switch', async () => {
		const { rerender, text } = setup([msg('a', 'cancelling')]);
		await rerender({ messages: [msg('a', 'cancelling'), msg('b')], sessionId: 's1' });
		await rerender({ messages: [msg('b')], sessionId: 's1' });
		expect(text()).toBe('Message withdrawn');
		await rerender({ messages: [msg('x', 'delivered')], sessionId: 's2' });
		expect(text()).toBe('');
		await rerender({ messages: [], sessionId: 's2' });
		expect(text()).toBe('');
	});

	describe('optimistic → authoritative rename through the real store', () => {
		const SID = 'announcer-rename';

		for (const [verdict, words] of [
			['withdrawn', 'Message withdrawn'],
			['already_delivered', 'Message already sent']
		] as const) {
			it(`stays silent across the rename, then announces ${verdict}`, async () => {
				pendingQueue.reset(SID, true);
				try {
					const local = { ...msg('client-c'), clientMessageId: 'client-c' };
					pendingQueue.noteOptimistic(SID, local.id);
					const view = setup(pendingQueue.reconcile(SID, [local]), SID);

					const key = pendingQueue.requestRemoval(SID, local);
					await view.rerender({ messages: pendingQueue.reconcile(SID, [local]), sessionId: SID });
					expect(view.text()).toBe('Cancelling message…');
					const beforeRename = view.region.textContent;

					// The server row arrives: new id, same client identity, still cancelling.
					const server = { ...local, id: 'turn-c' };
					pendingQueue.noteAuthoritative(SID, [server]);
					const renamed = pendingQueue.reconcile(SID, [server]);
					expect(renamed).toEqual([{ ...server, status: 'cancelling' }]);
					await view.rerender({ messages: renamed, sessionId: SID });
					expect(view.region.textContent).toBe(beforeRename);

					pendingQueue.applyRemovalVerdict(SID, key, verdict);
					await view.rerender({ messages: pendingQueue.reconcile(SID, [server]), sessionId: SID });
					expect(view.text()).toBe(words);
				} finally {
					pendingQueue.reset(SID, true);
				}
			});
		}
	});
});
