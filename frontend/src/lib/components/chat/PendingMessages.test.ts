/**
 * Behavior tests for PendingMessages — the user bubbles for prompts that have
 * been sent but not yet handed to the model. The LAST bubble owns the "Not seen
 * yet" row with Interrupt + unsend; earlier bubbles only carry the unsend "×".
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/svelte';
import PendingMessages from './PendingMessages.svelte';
import type { QueuedMessage } from '$lib/stores/types';

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
		it('shows "Cancelling…" and disables the last chip\'s unsend and Interrupt', async () => {
			const { container, onRemove, onInterrupt } = renderPending([
				makeMessage({ id: 'q-1', status: 'cancelling' })
			]);

			const status = screen.getByRole('status');
			expect(status.textContent).toBe('Cancelling…');
			expect(screen.queryByText('Not seen yet')).toBeNull();
			const interrupt = screen.getByRole('button', { name: 'Interrupt' }) as HTMLButtonElement;
			const remove = container.querySelector('.pending-remove') as HTMLButtonElement;
			expect(interrupt.disabled).toBe(true);
			expect(remove.disabled).toBe(true);
			expect(container.querySelector('[role="article"]')?.getAttribute('aria-busy')).toBe('true');

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
			// The earlier chip carries its own status line; the last chip is untouched.
			const firstCol = container.querySelectorAll('.usercol')[0];
			expect(firstCol.querySelector('[role="status"]')?.textContent).toBe('Cancelling…');
			expect(screen.getByText('Not seen yet')).toBeTruthy();
			expect((screen.getByRole('button', { name: 'Interrupt' }) as HTMLButtonElement).disabled).toBe(false);
		});

		it('shows "Already sent" with no unsend or Interrupt controls', () => {
			const { container } = renderPending([
				makeMessage({ id: 'q-1', content: 'first', status: 'delivered' }),
				makeMessage({ id: 'q-2', content: 'second', status: 'delivered' })
			]);

			const statuses = screen.getAllByRole('status');
			expect(statuses.map((s) => s.textContent)).toEqual(['Already sent', 'Already sent']);
			expect(statuses.every((s) => s.classList.contains('is-delivered'))).toBe(true);
			expect(container.querySelector('.hover-remove')).toBeNull();
			expect(container.querySelector('.pending-remove')).toBeNull();
			expect(screen.queryByRole('button', { name: 'Interrupt' })).toBeNull();
		});

		it('leaves chips without a status unchanged', () => {
			const { container } = renderPending([
				makeMessage({ id: 'q-1', content: 'first' }),
				makeMessage({ id: 'q-2', content: 'second' })
			]);

			expect(screen.queryByRole('status')).toBeNull();
			expect(container.querySelectorAll('.pending-actions')).toHaveLength(1);
			for (const b of container.querySelectorAll('button')) {
				expect((b as HTMLButtonElement).disabled).toBe(false);
			}
		});
	});
});
