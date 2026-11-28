import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ getBrowserTarget: vi.fn(), setBrowserTarget: vi.fn() }));
vi.mock('$lib/api/browser-panel', () => api);

import BrowserTargetPicker from './BrowserTargetPicker.svelte';

afterEach(() => {
	cleanup();
	vi.clearAllMocks();
});

const chrome = { id: 'device-1', name: 'Chrome on Windows' };

describe('browser target picker', () => {
	it('stays hidden while no extension browser is connected', async () => {
		api.getBrowserTarget.mockResolvedValue({ browser_id: null, assigned: false, browsers: [] });
		const view = render(BrowserTargetPicker, { sessionId: 'chat-1' });
		await waitFor(() => expect(api.getBrowserTarget).toHaveBeenCalled());
		expect(view.queryByRole('combobox')).toBeNull();
	});

	it('switches the chat to a connected browser and back to the built-in one', async () => {
		api.getBrowserTarget.mockResolvedValue({ browser_id: null, assigned: false, browsers: [chrome] });
		api.setBrowserTarget.mockImplementation(async (_sid: string, id: string | null) =>
			({ browser_id: id, assigned: !!id, browsers: [chrome] }));
		const view = render(BrowserTargetPicker, { sessionId: 'chat-1' });
		const select = (await view.findByRole('combobox')) as HTMLSelectElement;
		expect(select.value).toBe('');

		await fireEvent.change(select, { target: { value: 'device-1' } });
		expect(api.setBrowserTarget).toHaveBeenLastCalledWith('chat-1', 'device-1');
		await waitFor(() => expect(select.value).toBe('device-1'));

		await fireEvent.change(select, { target: { value: '' } });
		expect(api.setBrowserTarget).toHaveBeenLastCalledWith('chat-1', null);
	});

	it('names an assigned browser that went offline instead of showing the built-in one', async () => {
		api.getBrowserTarget.mockResolvedValue({ browser_id: 'device-9', assigned: true, browsers: [] });
		const view = render(BrowserTargetPicker, { sessionId: 'chat-1' });
		const select = (await view.findByRole('combobox')) as HTMLSelectElement;
		await waitFor(() => expect(select.value).toBe('device-9'));
		expect(select.selectedOptions[0].textContent).toBe('Your browser (offline)');
	});
});
