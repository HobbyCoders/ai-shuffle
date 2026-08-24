import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { createRawSnippet, tick } from 'svelte';
import type { ChatMessage } from '$lib/stores/types';
import WorkLogGroup from './WorkLogGroup.svelte';

/** jsdom has no ResizeObserver. Drive reserved height from the stub. */
function installFakeRO(initialHeight = 80) {
	let height = initialHeight;
	let cb: ResizeObserverCallback | null = null;
	let el: Element | null = null;

	class FakeRO {
		constructor(callback: ResizeObserverCallback) {
			cb = callback;
		}
		observe = (target: Element) => {
			el = target;
			Object.defineProperty(target, 'getBoundingClientRect', {
				value: () => ({ height }),
				configurable: true
			});
			cb?.([], this as unknown as ResizeObserver);
		};
		unobserve = () => {};
		disconnect = () => {
			cb = null;
			el = null;
		};
	}

	const g = globalThis as unknown as { ResizeObserver?: unknown };
	const previous = g.ResizeObserver;
	g.ResizeObserver = FakeRO;

	return {
		async setHeight(next: number) {
			height = next;
			if (el) {
				Object.defineProperty(el, 'getBoundingClientRect', {
					value: () => ({ height }),
					configurable: true
				});
			}
			cb?.([], {} as ResizeObserver);
			await tick();
		},
		restore() {
			if (previous) g.ResizeObserver = previous;
			else delete g.ResizeObserver;
		}
	};
}

const message = {
	id: 'tool-1',
	role: 'assistant',
	type: 'tool_use',
	content: '',
	toolName: 'Read',
	toolStatus: 'complete'
} as ChatMessage;

const row = createRawSnippet<[ChatMessage, number]>((getMessage) => ({
	render: () => `<div data-testid="work-row">${getMessage().content || getMessage().toolName}</div>`
}));

const baseProps = {
	folded: [{ message, index: 0 }],
	tail: [],
	durationLabel: null,
	summary: 'Read 1 file',
	row
};

afterEach(cleanup);

describe('WorkLogGroup', () => {
	it('uses action text for a collapsed live group', () => {
		render(WorkLogGroup, { props: { ...baseProps, mode: 'live' } });
		expect(screen.getByRole('button', { name: /Show work details: Read 1 file/i })).toBeTruthy();
	});

	it('closes an inspected live group when it settles', async () => {
		const view = render(WorkLogGroup, { props: { ...baseProps, mode: 'live' as const } });
		await fireEvent.click(screen.getByRole('button', { name: /Show work details/i }));
		expect(screen.getByTestId('work-row')).toBeTruthy();

		await view.rerender({ ...baseProps, mode: 'settled', durationLabel: 'Worked for 4s' });
		expect(screen.queryByTestId('work-row')).toBeNull();
		expect(screen.getByRole('button', { name: /Show work details: Worked for 4s, Read 1 file/i })).toBeTruthy();
	});

	it('holds peak height while live and releases it on settle', async () => {
		const ro = installFakeRO(80);
		const view = render(WorkLogGroup, { props: { ...baseProps, mode: 'live' as const } });
		const section = view.container.querySelector('section.worklog') as HTMLElement;
		expect(section.style.minHeight).toBe('80px');

		await view.rerender({ ...baseProps, mode: 'settled' as const, durationLabel: 'Worked for 4s' });
		expect(section.style.minHeight).toBe('');
		ro.restore();
	});

	it('does not keep the open-fold height after the user collapses it', async () => {
		const ro = installFakeRO(80);
		const view = render(WorkLogGroup, { props: { ...baseProps, mode: 'live' as const } });
		const section = view.container.querySelector('section.worklog') as HTMLElement;
		expect(section.style.minHeight).toBe('80px');

		await fireEvent.click(screen.getByRole('button', { name: /Show work details/i }));
		await ro.setHeight(240);
		expect(section.style.minHeight).toBe('80px');

		await fireEvent.click(screen.getByRole('button', { name: /Hide work details/i }));
		await ro.setHeight(80);
		expect(section.style.minHeight).toBe('80px');
		ro.restore();
	});

	it('releases reserved height when the same live tail row shrinks', async () => {
		const ro = installFakeRO(80);
		const tail = [{ message, index: 0 }];
		render(WorkLogGroup, { props: { ...baseProps, folded: [], tail, mode: 'live' as const } });
		const section = document.querySelector('section.worklog') as HTMLElement;

		await ro.setHeight(200);
		expect(section.style.minHeight).toBe('200px');

		await ro.setHeight(90);
		expect(section.style.minHeight).toBe('90px');
		ro.restore();
	});

	it('keeps peak height when a shorter successor replaces the tail', async () => {
		const ro = installFakeRO(80);
		const tailA = [{ message: { ...message, id: 'tool-a' }, index: 0 }];
		const tailB = [{ message: { ...message, id: 'tool-b' }, index: 1 }];
		const view = render(WorkLogGroup, {
			props: { ...baseProps, folded: [], tail: tailA, mode: 'live' as const }
		});
		const section = view.container.querySelector('section.worklog') as HTMLElement;

		await ro.setHeight(200);
		expect(section.style.minHeight).toBe('200px');

		await view.rerender({
			...baseProps,
			folded: tailA,
			tail: tailB,
			mode: 'live' as const
		});
		await ro.setHeight(90);
		expect(section.style.minHeight).toBe('200px');
		ro.restore();
	});

	it('keeps the terminal answer visible while settled work is collapsed', async () => {
		const answer = {
			id: 'answer-1',
			role: 'assistant',
			type: 'text',
			content: 'Final answer'
		} as ChatMessage;
		render(WorkLogGroup, {
			props: {
				...baseProps,
				mode: 'settled',
				durationLabel: 'Worked for 4s',
				sequence: [
					{ message, index: 0, hiddenWhenCollapsed: true },
					{ message: answer, index: 1, hiddenWhenCollapsed: false }
				]
			}
		});

		expect(screen.getByText('Final answer')).toBeTruthy();
		expect(screen.queryByText('Read')).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: /Show work details/i }));
		expect(screen.getByText('Read')).toBeTruthy();
		expect(screen.getByText('Final answer')).toBeTruthy();
	});
});
