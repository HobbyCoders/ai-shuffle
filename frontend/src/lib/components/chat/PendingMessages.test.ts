/**
 * Behavior tests for PendingMessages — the user bubbles for prompts that have
 * been sent but not yet handed to the model. The LAST bubble owns the "Not seen
 * yet" row with Interrupt + unsend; earlier bubbles only carry the unsend "×".
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/svelte';
import PendingMessages from './PendingMessages.svelte';
import type { QueuedMessage } from '$lib/stores/types';
import { pendingQueue } from '$lib/stores/session/pending-queue';

function makeMessage(over: Partial<QueuedMessage> = {}): QueuedMessage {
	return {
		id: 'q-1',
		content: 'are you there?',
		queuedAt: Date.now(),
		...over
	};
}

function renderPending(messages: QueuedMessage[]) {
	const onRemove = vi.fn();
	const onInterrupt = vi.fn();
	const result = render(PendingMessages, { props: { messages, onRemove, onInterrupt } });
	return { ...result, onRemove, onInterrupt };
}

afterEach(() => {
	cleanup();
});

describe('PendingMessages', () => {
	it('renders nothing when there is nothing pending', () => {
		const { container } = renderPending([]);
		expect(container.querySelector('.pending-messages')).toBeNull();
	});

	it('renders one right-aligned user bubble per pending message', () => {
		const { container } = renderPending([
			makeMessage({ id: 'q-1', content: 'first' }),
			makeMessage({ id: 'q-2', content: 'second' })
		]);

		expect(container.querySelectorAll('.bubble')).toHaveLength(2);
		expect(screen.getByText('first')).toBeTruthy();
		expect(screen.getByText('second')).toBeTruthy();
	});

	it('puts the "Not seen yet" action row under the LAST bubble only', () => {
		const { container } = renderPending([
			makeMessage({ id: 'q-1', content: 'first' }),
			makeMessage({ id: 'q-2', content: 'second' })
		]);

		const rows = container.querySelectorAll('.pending-actions');
		expect(rows).toHaveLength(1);
		expect(screen.getByText('Not seen yet')).toBeTruthy();
		expect(screen.getByText('Interrupt')).toBeTruthy();
		// The row belongs to the last bubble's column.
		const lastCol = container.querySelectorAll('.usercol')[1];
		expect(lastCol.querySelector('.pending-actions')).not.toBeNull();
	});

	it('unsends the last message through onRemove', async () => {
		const { container, onRemove } = renderPending([
			makeMessage({ id: 'q-1', content: 'first' }),
			makeMessage({ id: 'q-2', content: 'second' })
		]);

		await fireEvent.click(container.querySelector('.pending-remove') as HTMLElement);

		expect(onRemove).toHaveBeenCalledWith('q-2');
	});

	it('unsends an earlier message through its hover "×"', async () => {
		const { container, onRemove } = renderPending([
			makeMessage({ id: 'q-1', content: 'first' }),
			makeMessage({ id: 'q-2', content: 'second' })
		]);

		const hoverRemoves = container.querySelectorAll('.hover-remove');
		expect(hoverRemoves).toHaveLength(1); // never on the last bubble
		await fireEvent.click(hoverRemoves[0] as HTMLElement);

		expect(onRemove).toHaveBeenCalledWith('q-1');
	});

	it('stops the live turn from the Interrupt action', async () => {
		const { onInterrupt } = renderPending([makeMessage()]);

		await fireEvent.click(screen.getByText('Interrupt'));

		expect(onInterrupt).toHaveBeenCalledTimes(1);
	});

	it('clamps a very long pending prompt', () => {
		const { container } = renderPending([makeMessage({ content: 'x'.repeat(900) })]);
		expect(container.querySelector('.bubble-text.is-clamped')).not.toBeNull();
	});

	describe('unsend verdict states (#2275)', () => {
		const statusText = (col: Element) => col.querySelector('.pending-status')?.textContent;

		it('shows "Cancelling…" and disables the controls when every chip is cancelling', async () => {
			const { container, onRemove, onInterrupt } = renderPending([
				makeMessage({ id: 'q-1', status: 'cancelling' })
			]);

			expect(statusText(container)).toBe('Cancelling…');
			expect(screen.queryByText('Not seen yet')).toBeNull();
			const interrupt = screen.getByRole('button', { name: 'Interrupt' }) as HTMLButtonElement;
			const remove = container.querySelector('.pending-remove') as HTMLButtonElement;
			expect(interrupt.disabled).toBe(true);
			expect(remove.disabled).toBe(true);

			await fireEvent.click(interrupt);
			await fireEvent.click(remove);
			expect(onInterrupt).not.toHaveBeenCalled();
			expect(onRemove).not.toHaveBeenCalled();
		});

		it('disables an earlier chip\'s hover "×" while it is cancelling', async () => {
			const { container, onRemove } = renderPending([
				makeMessage({ id: 'q-1', content: 'first', status: 'cancelling' }),
				makeMessage({ id: 'q-2', content: 'second' })
			]);

			const hover = container.querySelector('.hover-remove') as HTMLButtonElement;
			expect(hover.disabled).toBe(true);
			await fireEvent.click(hover);
			expect(onRemove).not.toHaveBeenCalled();
			expect(statusText(container.querySelectorAll('.usercol')[0])).toBe('Cancelling…');
			expect(screen.getByText('Not seen yet')).toBeTruthy();
			expect((screen.getByRole('button', { name: 'Interrupt' }) as HTMLButtonElement).disabled).toBe(false);
		});

		it('shows "Already sent" with no unsend or Interrupt controls', () => {
			const { container } = renderPending([
				makeMessage({ id: 'q-1', content: 'first', status: 'delivered' }),
				makeMessage({ id: 'q-2', content: 'second', status: 'delivered' })
			]);

			const statuses = [...container.querySelectorAll('.pending-status')];
			expect(statuses.map((s) => s.textContent)).toEqual(['Already sent', 'Already sent']);
			expect(statuses.every((s) => s.classList.contains('is-delivered'))).toBe(true);
			expect(container.querySelector('.hover-remove')).toBeNull();
			expect(container.querySelector('.pending-remove')).toBeNull();
			expect(screen.queryByRole('button', { name: 'Interrupt' })).toBeNull();
		});

		it('keeps one enabled Interrupt on the last actionable chip behind a cancelling or delivered tail', async () => {
			for (const status of ['cancelling', 'delivered'] as const) {
				const { container, onInterrupt, onRemove } = renderPending([
					makeMessage({ id: 'q-1', content: 'first' }),
					makeMessage({ id: 'q-2', content: 'second', status })
				]);
				const cols = container.querySelectorAll('.usercol');
				const interrupts = screen.getAllByRole('button', { name: 'Interrupt' }) as HTMLButtonElement[];
				expect(interrupts).toHaveLength(1);
				expect(interrupts[0].disabled).toBe(false);
				expect(cols[0].contains(interrupts[0])).toBe(true);
				expect(cols[0].querySelector('.hover-remove')).toBeNull();
				// The tail keeps only its own (disabled) hover × while cancelling; none once delivered.
				const tailButtons = [...cols[1].querySelectorAll('button')] as HTMLButtonElement[];
				expect(tailButtons.map((b) => [b.classList[0], b.disabled])).toEqual(
					status === 'cancelling' ? [['hover-remove', true]] : []
				);
				await fireEvent.click(interrupts[0]);
				await fireEvent.click(cols[0].querySelector('.pending-remove') as HTMLElement);
				expect(onInterrupt).toHaveBeenCalledTimes(1);
				expect(onRemove).toHaveBeenCalledWith('q-1');
				cleanup();
			}
		});

		it('follows the real store from pending → cancelling → delivered without losing Interrupt', async () => {
			const SID = 'pending-messages-test';
			pendingQueue.reset(SID, true);
			const a = makeMessage({ id: 'turn-a', content: 'first' });
			const b = makeMessage({ id: 'turn-b', content: 'second' });
			const rows = [a, b];
			pendingQueue.noteAuthoritative(SID, rows);
			const view = renderPending(pendingQueue.reconcile(SID, rows));

			const key = pendingQueue.requestRemoval(SID, b);
			await view.rerender({ messages: pendingQueue.reconcile(SID, rows) });
			expect(statusText(view.container.querySelectorAll('.usercol')[1])).toBe('Cancelling…');
			expect((screen.getByRole('button', { name: 'Interrupt' }) as HTMLButtonElement).disabled).toBe(false);

			pendingQueue.applyRemovalVerdict(SID, key, 'already_delivered');
			await view.rerender({ messages: pendingQueue.reconcile(SID, rows) });
			expect(statusText(view.container.querySelectorAll('.usercol')[1])).toBe('Already sent');
			const interrupt = screen.getByRole('button', { name: 'Interrupt' }) as HTMLButtonElement;
			expect(interrupt.disabled).toBe(false);
			await fireEvent.click(interrupt);
			expect(view.onInterrupt).toHaveBeenCalledTimes(1);
			pendingQueue.reset(SID, true);
		});

		it('leaves chips without a status unchanged and puts no live region inside the log', () => {
			const { container } = renderPending([
				makeMessage({ id: 'q-1', content: 'first' }),
				makeMessage({ id: 'q-2', content: 'second', status: 'cancelling' })
			]);
			expect(container.querySelector('[role="status"], [aria-live], [aria-busy]')).toBeNull();
			cleanup();

			const plain = renderPending([
				makeMessage({ id: 'q-1', content: 'first' }),
				makeMessage({ id: 'q-2', content: 'second' })
			]);
			expect(plain.container.querySelectorAll('.pending-actions')).toHaveLength(1);
			expect(plain.container.querySelectorAll('.usercol')[1].querySelector('.pending-actions')).not.toBeNull();
			for (const b of plain.container.querySelectorAll('button')) {
				expect((b as HTMLButtonElement).disabled).toBe(false);
			}
		});
	});
});
