import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render } from '@testing-library/svelte';
import WorkerStatusDot from './WorkerStatusDot.svelte';

afterEach(() => cleanup());

describe('WorkerStatusDot', () => {
	it('renders the green online state', () => {
		const { container } = render(WorkerStatusDot, { online: true });
		const dot = container.querySelector('.worker-status-dot')!;
		expect(dot.classList.contains('online')).toBe(true);
		expect(dot.classList.contains('offline')).toBe(false);
		expect(dot.getAttribute('data-status')).toBe('online');
		expect(dot.getAttribute('title')).toBe('Online');
	});

	it('renders the red offline state', () => {
		const { container } = render(WorkerStatusDot, { online: false });
		const dot = container.querySelector('.worker-status-dot')!;
		expect(dot.classList.contains('offline')).toBe(true);
		expect(dot.classList.contains('online')).toBe(false);
		expect(dot.getAttribute('data-status')).toBe('offline');
		expect(dot.getAttribute('title')).toBe('Offline');
	});

	it('stays out of the accessible name of the item it decorates', () => {
		const { container } = render(WorkerStatusDot, { online: true });
		expect(container.querySelector('.worker-status-dot')?.getAttribute('aria-hidden')).toBe('true');
	});
});
