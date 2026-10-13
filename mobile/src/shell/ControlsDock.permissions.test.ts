import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { tick } from 'svelte';
import ControlsDock from '$lib/components/chat/ControlsDock.svelte';
import { activeSession } from '$lib/stores/active-session';
import { appMode } from '$lib/stores/sessionMode.svelte';
import { api } from '$lib/api/client';
import { installMobileShell } from './register';
import '$lib/test-stubs/composer-dom';

installMobileShell();
vi.hoisted(() => {
	vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404, json: async () => ({}) }));
});
vi.mock('$lib/api/client', async (importOriginal) => ({
	...await importOriginal<Record<string, unknown>>(),
	getUsageLimits: vi.fn().mockResolvedValue({ available: false, buckets: {}, provider_buckets: {} })
}));

beforeEach(async () => {
	appMode.set('code');
	activeSession.newChat();
	await activeSession.setPermissionModeOverride('default');
});
afterEach(() => {
	cleanup();
	document.querySelectorAll('.mcp-portal').forEach((n) => n.remove());
	vi.restoreAllMocks();
	delete window.__SHUFFLE_MOBILE__;
	activeSession.current.sessionId = null;
	activeSession.newChat();
});
afterAll(() => vi.unstubAllGlobals());

/** The Chat controls panel's Tool access radios (#1816). */
const panelAccess = () => within(document.querySelector<HTMLElement>('.mcp [role="radiogroup"][aria-label="Tool access"]')!);
async function openControls() {
	const view = render(ControlsDock, { props: { isUploading: false, onAttachClick: vi.fn() } });
	view.component.setMobileSheetOpen(true);
	await tick();
	return { ...view, access: panelAccess() };
}

describe('mobile tool-access persistence', () => {
	it.each([['Ask', 'default'], ['Accept edits', 'acceptEdits'], ['Bypass', 'bypassPermissions']])('carries the explicit %s choice across sheet reopen and a new compose screen', async (label, mode) => {
		const { component, access } = await openControls();
		await fireEvent.click(access.getByRole('radio', { name: label }));
		await waitFor(() => expect(activeSession.current.permissionModeOverride).toBe(mode));
		component.setMobileSheetOpen(false);
		await tick();
		activeSession.newChat();
		component.setMobileSheetOpen(true);
		await tick();
		expect(panelAccess().getByRole('radio', { name: label }).getAttribute('aria-checked')).toBe('true');
		// The quick bar names the same choice.
		expect(screen.getByRole('button', { name: new RegExp(`^Tool access: ${label}`, 'i') })).toBeTruthy();
	});

	it('keeps the saved selection while saving and disables ignored repeat taps until acknowledgement', async () => {
		activeSession.current.sessionId = 'mobile-permissions';
		let acknowledge!: (value: { permission_mode: string }) => void;
		const put = vi.spyOn(api, 'put').mockImplementation(() => new Promise(resolve => { acknowledge = resolve; }));
		const { access } = await openControls();
		await fireEvent.click(access.getByRole('radio', { name: 'Accept edits' }));
		expect(put).toHaveBeenCalledWith('/sessions/mobile-permissions/permission-mode', { permission_mode: 'acceptEdits' });
		expect(access.getByRole('radio', { name: 'Ask' }).getAttribute('aria-checked')).toBe('true');
		expect(access.getAllByRole('radio')[0].closest('.tile')!.querySelector('.access-help')).toBeTruthy();
		for (const button of access.getAllByRole('radio')) expect((button as HTMLButtonElement).disabled).toBe(true);
		acknowledge({ permission_mode: 'acceptEdits' });
		await waitFor(() => expect(access.getByRole('radio', { name: 'Accept edits' }).getAttribute('aria-checked')).toBe('true'));
		for (const button of access.getAllByRole('radio')) expect((button as HTMLButtonElement).disabled).toBe(false);
		expect(document.querySelector('.mcp .access-help')).toBeNull();
	});

	it('sends the mobile selection to the paired worker with the bundled runtime authentication', async () => {
		window.__SHUFFLE_MOBILE__ = { apiOrigin: 'https://paired-worker.test', token: 'test-paired-session' };
		activeSession.current.sessionId = 'mobile-permissions';
		const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) =>
			String(url).endsWith('/sessions/mobile-permissions/permission-mode')
				? Response.json({ permission_mode: 'acceptEdits' })
				: new Response('{}', { status: 404 })
		);
		const { access } = await openControls();
		await fireEvent.click(access.getByRole('radio', { name: 'Accept edits' }));
		await waitFor(() => expect(access.getByRole('radio', { name: 'Accept edits' }).getAttribute('aria-checked')).toBe('true'));
		expect(fetch).toHaveBeenCalledWith('https://paired-worker.test/api/v1/sessions/mobile-permissions/permission-mode', expect.objectContaining({
			method: 'PUT', credentials: 'omit', redirect: 'error',
			headers: expect.objectContaining({ Authorization: 'Bearer test-paired-session' }),
			body: JSON.stringify({ permission_mode: 'acceptEdits' })
		}));
	});

	it('preserves the saved choice after a failed write and permits retry', async () => {
		activeSession.current.sessionId = 'mobile-permissions';
		const put = vi.spyOn(api, 'put').mockRejectedValueOnce(new Error('Worker unavailable'))
			.mockResolvedValueOnce({ permission_mode: 'acceptEdits' });
		const { access } = await openControls();
		const edits = access.getByRole('radio', { name: 'Accept edits' }) as HTMLButtonElement;
		await fireEvent.click(edits);
		await waitFor(() => expect(edits.disabled).toBe(false));
		expect(access.getByRole('radio', { name: 'Ask' }).getAttribute('aria-checked')).toBe('true');
		expect(activeSession.current.permissionModeOverride).toBe('default');
		await fireEvent.click(edits);
		await waitFor(() => expect(edits.getAttribute('aria-checked')).toBe('true'));
		expect(put).toHaveBeenCalledTimes(2);
	});
});
