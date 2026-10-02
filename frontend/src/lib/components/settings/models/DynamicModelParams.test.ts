/** Ported from the old setting-editor-ownership tests (model parameter half). */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';

const mocks = vi.hoisted(() => ({ owner: 0, owners: new Set<(epoch: number) => void>(), get: vi.fn(), patch: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: { get: mocks.get, patch: mocks.patch }, getErrorMessage: () => 'Failed' }));
vi.mock('$lib/stores/session-owner', () => ({
	sessionOwnerEpoch: () => mocks.owner,
	sessionOwnerBoundary: {
		subscribe(run: (epoch: number) => void) {
			run(mocks.owner);
			mocks.owners.add(run);
			return () => mocks.owners.delete(run);
		}
	}
}));
vi.mock('$lib/services/watch-resources', () => ({ watchResources: () => {} }));
vi.mock('$lib/stores/modelSchema', () => ({
	modelSchemaStore: {
		fetch: async () => ({}),
		configurableFields: (schema: unknown) =>
			schema
				? [
						{ name: 'enabled', title: 'Enabled', type: 'boolean', default: false, description: 'Turn it on' },
						{ name: 'steps', title: 'Steps', type: 'integer', default: 28, minimum: 1, maximum: 50 },
						{ name: 'style', title: 'Style', type: 'enum', enum: ['a', 'b'], required: false, default: null }
					]
				: []
	}
}));
import DynamicModelParams from './DynamicModelParams.svelte';
import LazyPanel from '../../deck/lazy/LazyPanel.svelte';
import { createLazyComponent } from '../../deck/lazy/lazy-component';

beforeEach(() => {
	mocks.owner = 0;
	vi.clearAllMocks();
	mocks.get.mockResolvedValue({ params: {} });
	mocks.patch.mockResolvedValue({});
});
afterEach(() => {
	cleanup();
	vi.useRealTimers();
});

const enabled = () => screen.findByRole('switch', { name: 'Enabled' });

describe('DynamicModelParams', () => {
	it('renders one row per field in the row grammar', async () => {
		render(DynamicModelParams, { endpointId: 'model', persistKey: 'image_model' });
		await enabled();
		expect(screen.getByRole('heading', { name: 'Model parameters' })).toBeTruthy();
		expect((screen.getByRole('slider', { name: 'Steps' }) as HTMLInputElement).value).toBe('28');
		const style = screen.getByRole('combobox', { name: 'Style' }) as HTMLSelectElement;
		expect([...style.options].map((o) => o.textContent)).toEqual(['Default', 'a', 'b']);
		expect(screen.getByRole('button', { name: 'About Enabled' })).toBeTruthy();
	});

	for (const boundary of ['account', 'unmount'] as const) {
		it(`cancels model parameter saves after ${boundary}`, async () => {
			const view = render(DynamicModelParams, { endpointId: 'model', persistKey: 'image_model' });
			const input = await enabled();
			vi.useFakeTimers();
			await fireEvent.click(input);
			if (boundary === 'account') mocks.owner++;
			else view.unmount();
			await vi.advanceTimersByTimeAsync(400);
			expect(mocks.patch).not.toHaveBeenCalled();
		});
	}

	it('still saves a current model parameter edit', async () => {
		render(DynamicModelParams, { endpointId: 'model', persistKey: 'image_model' });
		const input = await enabled();
		vi.useFakeTimers();
		await fireEvent.click(input);
		await vi.advanceTimersByTimeAsync(400);
		expect(mocks.patch).toHaveBeenCalledWith('/settings/integrations/model-params', {
			key: 'image_model',
			params: { enabled: true }
		});
	});

	it('reloads parameters when the setting key changes for the same model', async () => {
		const view = render(DynamicModelParams, { endpointId: 'model', persistKey: 'image_model' });
		await enabled();
		mocks.get.mockResolvedValue({ params: { enabled: true } });
		await view.rerender({ endpointId: 'model', persistKey: 'tts_model' });
		await waitFor(() => expect(screen.getByRole('switch').getAttribute('aria-checked')).toBe('true'));
		expect(mocks.get).toHaveBeenLastCalledWith('/settings/integrations/model-params?key=tts_model');
	});

	it('hides old parameter controls while a different setting context is loading', async () => {
		const view = render(DynamicModelParams, { endpointId: 'model', persistKey: 'image_model' });
		await enabled();
		let resolve!: (value: unknown) => void;
		mocks.get.mockImplementationOnce(() => new Promise((r) => (resolve = r)));
		await view.rerender({ endpointId: 'model', persistKey: 'tts_model' });
		expect(screen.queryByRole('switch')).toBeNull();
		expect(screen.getByText('Loading model options…')).toBeTruthy();
		expect(mocks.patch).not.toHaveBeenCalled();
		resolve({ params: { enabled: true } });
		expect((await enabled()).getAttribute('aria-checked')).toBe('true');
	});

	it('remounts retained panels for a new account while preserving the same-account instance', async () => {
		const handle = createLazyComponent('params', async () => ({ default: DynamicModelParams }));
		render(LazyPanel, { props: { handle, props: { endpointId: 'model', persistKey: 'image_model' } } });
		const first = await enabled();
		for (const notify of mocks.owners) notify(mocks.owner);
		await tick();
		expect(screen.getByRole('switch')).toBe(first);
		mocks.owner++;
		for (const notify of mocks.owners) notify(mocks.owner);
		await tick();
		const second = await enabled();
		expect(second).not.toBe(first);
		vi.useFakeTimers();
		await fireEvent.click(second);
		await vi.advanceTimersByTimeAsync(400);
		expect(mocks.patch).toHaveBeenCalledWith('/settings/integrations/model-params', {
			key: 'image_model',
			params: { enabled: true }
		});
	});

	it('recovers when returning to the previous context while another load is pending', async () => {
		const view = render(DynamicModelParams, { endpointId: 'model', persistKey: 'image_model' });
		await enabled();
		let releaseOther!: (value: unknown) => void;
		mocks.get.mockImplementationOnce(() => new Promise((r) => (releaseOther = r)));
		await view.rerender({ endpointId: 'model', persistKey: 'tts_model' });
		expect(screen.queryByRole('switch')).toBeNull();
		mocks.get.mockResolvedValue({ params: { enabled: true } });
		await view.rerender({ endpointId: 'model', persistKey: 'image_model' });
		expect((await enabled()).getAttribute('aria-checked')).toBe('true');
		releaseOther({ params: { enabled: false } });
		await tick();
		expect(screen.getByRole('switch').getAttribute('aria-checked')).toBe('true');
	});
});
