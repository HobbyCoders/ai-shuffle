import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/svelte';
import LazyPanel from './LazyPanel.svelte';
import { createLazyComponent } from './lazy-component';
import Stub from './__fixtures__/StubPanel.svelte';

afterEach(() => cleanup());

describe('LazyPanel', () => {
	it('renders an already-resolved handle without ever showing a skeleton', async () => {
		const handle = createLazyComponent('stub', async () => ({ default: Stub }));
		await handle.load(); // warmed by hover / idle prefetch

		render(LazyPanel, { props: { handle, props: { label: 'warm' }, label: 'Stub' } });

		expect(screen.getByTestId('stub-panel').textContent).toBe('warm');
		expect(screen.queryByLabelText('Loading Stub')).toBeNull();
	});

	it('holds the skeleton back for a grace period, then renders the component', async () => {
		let release: (v: { default: unknown }) => void = () => {};
		const handle = createLazyComponent(
			'stub',
			() => new Promise<{ default: unknown }>((res) => (release = res))
		);

		render(LazyPanel, { props: { handle, label: 'Stub', skeletonDelayMs: 20 } });
		// Nothing at all in the grace period — no spinner flash on a fast chunk.
		expect(screen.queryByLabelText('Loading Stub')).toBeNull();

		await waitFor(() => expect(screen.getByLabelText('Loading Stub')).toBeTruthy());

		release({ default: Stub });
		await waitFor(() => expect(screen.getByTestId('stub-panel')).toBeTruthy());
		expect(screen.queryByLabelText('Loading Stub')).toBeNull();
	});

	it('shows a retry on chunk-load failure and recovers when the retry succeeds', async () => {
		vi.spyOn(console, 'error').mockImplementation(() => {});
		const loader = vi
			.fn()
			.mockRejectedValueOnce(new Error('Failed to fetch dynamically imported module'))
			.mockResolvedValueOnce({ default: Stub });
		const handle = createLazyComponent('stub', loader as never);

		render(LazyPanel, { props: { handle, label: 'Stub' } });

		const retry = await screen.findByRole('button', { name: 'Retry' });
		expect(screen.getByRole('alert')).toBeTruthy();

		await fireEvent.click(retry);
		await waitFor(() => expect(screen.getByTestId('stub-panel')).toBeTruthy());
		expect(screen.queryByRole('alert')).toBeNull();
	});

	it('renders nothing while loading or failing when `bare` (modals, hidden surfaces)', async () => {
		vi.spyOn(console, 'error').mockImplementation(() => {});
		const handle = createLazyComponent('stub', async () => {
			throw new Error('offline');
		});

		const { container } = render(LazyPanel, { props: { handle, label: 'Stub', bare: true } });
		await new Promise((r) => setTimeout(r, 30));
		expect(container.textContent?.trim()).toBe('');
	});
});
