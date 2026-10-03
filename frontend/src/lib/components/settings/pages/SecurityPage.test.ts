import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { OwnerStatus } from '$lib/api/owner';

const owner = vi.hoisted(() => ({
	fetchOwnerStatus: vi.fn(),
	changeOwnerPassword: vi.fn(),
	createOwnerPassword: vi.fn(),
	setTrustedLan: vi.fn()
}));
const transport = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn(), patch: vi.fn() }));
const refresh = vi.hoisted(() => vi.fn());
const layout = vi.hoisted(() => ({ openFeaturePage: vi.fn() }));
const host = vi.hoisted(() => ({ desktop: null as unknown, status: { available: true, reason: 'ok', enabled: false } }));
const computer = vi.hoisted(() => ({ get: vi.fn(), set: vi.fn(), setMode: vi.fn(), stop: vi.fn() }));

vi.mock('$lib/api/owner', () => owner);
vi.mock('$lib/stores/owner-access.svelte', () => ({ ownerAccess: { refresh } }));
vi.mock('$lib/api/client', () => ({
	api: transport,
	getErrorMessage: (e: { detail?: string }, fallback: string) => e?.detail ?? fallback
}));
vi.mock('$lib/stores/layout.svelte', () => ({ layout }));
vi.mock('$lib/services/desktop', () => ({
	get desktop() {
		return host.desktop;
	}
}));
vi.mock('$lib/api/computerUse', () => ({
	getComputerUseStatus: computer.get,
	setComputerUseEnabled: computer.set,
	setComputerUseInputMode: computer.setMode,
	stopComputerUse: computer.stop
}));

import SecurityPage from './SecurityPage.svelte';
import { resourceSync } from '$lib/services/resource-sync';
import { assertCategoryCoverage } from '$lib/settings/testing/coverage';

const status = (over: Partial<OwnerStatus> = {}): OwnerStatus => ({
	password_set: true,
	setup_allowed: false,
	required: false,
	trusted_lan: false,
	login_available: true,
	desktop_handoff: false,
	min_length: 12,
	...over
});
const loaded = (over: Partial<OwnerStatus> = {}) => ({ ok: true as const, status: status(over) });
const input = (name: string) => screen.getByLabelText(name) as HTMLInputElement;

let instructions: { current: string; default: string; is_customized: boolean };
let sandbox: unknown;

async function openPassword() {
	await fireEvent.click(await screen.findByRole('button', { name: /^Owner password/ }));
}

async function fill(values: Record<string, string>) {
	for (const [name, value] of Object.entries(values)) await fireEvent.input(input(name), { target: { value } });
	await fireEvent.submit(screen.getByRole('form', { name: 'Owner password' }));
}

beforeEach(() => {
	vi.clearAllMocks();
	host.desktop = null;
	host.status = { available: true, reason: 'ok', enabled: false };
	owner.fetchOwnerStatus.mockResolvedValue(loaded());
	owner.changeOwnerPassword.mockResolvedValue({ status: 'ok' });
	owner.createOwnerPassword.mockResolvedValue({ status: 'ok' });
	owner.setTrustedLan.mockResolvedValue({ trusted_lan: true });
	refresh.mockResolvedValue(null);
	instructions = { current: 'Be careful.', default: 'Default rules.', is_customized: true };
	sandbox = { mode: 'auto', state: 'active', active: true, reason: '' };
	transport.get.mockImplementation(async (path: string) => {
		if (path === '/deployment') return { code_sandbox: sandbox };
		if (path === '/settings/security-instructions') return { ...instructions };
		return {};
	});
	transport.put.mockResolvedValue({});
	transport.delete.mockResolvedValue({});
	computer.get.mockImplementation(async () => ({ display: null, display_label: null, active: false, ...host.status }));
	computer.set.mockImplementation(async (on: boolean) => ({ enabled: on }));
	computer.setMode.mockImplementation(async (mode: string) => ({ input_mode: mode }));
});
afterEach(() => {
	cleanup();
	resourceSync.clear();
});

describe('coverage', () => {
	it('renders a row for every Security entry in the desktop app', async () => {
		host.desktop = { agentAccess: { title: 'Agents run as you', message: 'm', points: [], footer: 'f' } };
		await assertCategoryCoverage('security', () => render(SecurityPage).container);
	});

	it('leaves "Agents run as you" out of the web app', () => {
		render(SecurityPage);
		expect(screen.queryByText('Agents run as you')).toBeNull();
	});
});

describe('owner password', () => {
	it('enforces the worker minimum and the repeat before calling the API', async () => {
		render(SecurityPage);
		await openPassword();
		expect(screen.getByText('At least 12 characters')).toBeTruthy();
		await fill({ 'Current password': 'old-secret', 'New password': 'short', 'Repeat new password': 'short' });
		expect(screen.getByRole('alert').textContent).toBe('Use at least 12 characters.');
		await fill({ 'New password': 'long-enough-pass', 'Repeat new password': 'long-enough-typo' });
		expect(screen.getByRole('alert').textContent).toBe('The two new passwords are different.');
		expect(owner.changeOwnerPassword).not.toHaveBeenCalled();
	});

	it('changes the password, says other browsers were signed out and closes the form', async () => {
		render(SecurityPage);
		await openPassword();
		await fill({ 'Current password': 'old-secret', 'New password': 'long-enough-pass', 'Repeat new password': 'long-enough-pass' });
		await waitFor(() =>
			expect(screen.getByRole('status', { name: '' }).textContent).toContain('Every other browser was signed out')
		);
		expect(owner.changeOwnerPassword).toHaveBeenCalledWith('old-secret', 'long-enough-pass');
		expect(owner.createOwnerPassword).not.toHaveBeenCalled();
		await waitFor(() => expect(refresh).toHaveBeenCalledOnce());
		expect(owner.fetchOwnerStatus).toHaveBeenCalledTimes(2);
		expect(screen.queryByRole('form', { name: 'Owner password' })).toBeNull();
		expect(screen.getByText('Other browsers signed out')).toBeTruthy();
	});

	it('creates the first password without asking for a current one', async () => {
		owner.fetchOwnerStatus.mockResolvedValue(loaded({ password_set: false, min_length: 8 }));
		render(SecurityPage);
		expect(await screen.findByText('Not set yet')).toBeTruthy();
		await openPassword();
		expect(screen.queryByLabelText('Current password')).toBeNull();
		await fill({ 'New password': 'eight-ch', 'Repeat new password': 'eight-ch' });
		await waitFor(() => expect(owner.createOwnerPassword).toHaveBeenCalledWith('eight-ch'));
		expect(await screen.findByText('Password set')).toBeTruthy();
	});

	it('shows the worker rejection and keeps what was typed', async () => {
		owner.changeOwnerPassword.mockRejectedValueOnce({ detail: 'The current password is wrong.' });
		render(SecurityPage);
		await openPassword();
		await fill({ 'Current password': 'nope', 'New password': 'long-enough-pass', 'Repeat new password': 'long-enough-pass' });
		expect((await screen.findByRole('alert')).textContent).toBe('The current password is wrong.');
		expect(input('New password').value).toBe('long-enough-pass');
		expect(refresh).not.toHaveBeenCalled();
	});
});

describe('owner status load failures', () => {
	it('shows an error with a retry when the worker does not answer', async () => {
		owner.fetchOwnerStatus.mockResolvedValueOnce({ ok: false, reason: 'unreachable' });
		render(SecurityPage);
		expect((await screen.findByRole('alert')).textContent?.trim()).toBe(
			'Could not load owner access status. The worker did not answer.'
		);
		await fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
		expect(await screen.findByRole('button', { name: /^Owner password/ })).toBeTruthy();
		expect(owner.fetchOwnerStatus).toHaveBeenCalledTimes(2);
		expect(screen.queryByRole('alert')).toBeNull();
	});

	it('says the worker answered with an error rather than did not answer', async () => {
		owner.fetchOwnerStatus.mockResolvedValueOnce({ ok: false, reason: 'error', code: 503 });
		render(SecurityPage);
		const alert = (await screen.findByRole('alert')).textContent ?? '';
		expect(alert).toContain('The worker answered with an error (status 503).');
		expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy();
	});

	it('explains that an older worker has no owner passwords', async () => {
		owner.fetchOwnerStatus.mockResolvedValueOnce({ ok: false, reason: 'unsupported' });
		render(SecurityPage);
		expect(await screen.findByText('Not supported')).toBeTruthy();
		expect(screen.getByText(/does not support owner passwords/)).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull();
	});
});

describe('trusted LAN', () => {
	it('asks before turning on, and turns the switch back off when saving fails', async () => {
		render(SecurityPage);
		const lan = await screen.findByRole('switch', { name: 'Trusted LAN, no login' });
		await waitFor(() => expect((lan as HTMLButtonElement).disabled).toBe(false));
		await fireEvent.click(lan);
		expect(screen.getByText('Any device becomes owner?')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
		expect(owner.setTrustedLan).not.toHaveBeenCalled();
		expect(screen.getByRole('switch', { name: 'Trusted LAN, no login' }).getAttribute('aria-checked')).toBe('false');

		owner.setTrustedLan.mockRejectedValueOnce({ detail: 'Not allowed from here' });
		await fireEvent.click(screen.getByRole('switch', { name: 'Trusted LAN, no login' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Trust network' }));
		expect(owner.setTrustedLan).toHaveBeenCalledWith(true);
		expect((await screen.findByRole('alert')).textContent).toBe('Not allowed from here');
		expect(screen.getByRole('switch', { name: 'Trusted LAN, no login' }).getAttribute('aria-checked')).toBe('false');
	});

	it('turns off without asking and reloads the owner status', async () => {
		owner.fetchOwnerStatus.mockResolvedValue(loaded({ trusted_lan: true }));
		render(SecurityPage);
		const lan = await screen.findByRole('switch', { name: 'Trusted LAN, no login' });
		await waitFor(() => expect(lan.getAttribute('aria-checked')).toBe('true'));
		await fireEvent.click(lan);
		await waitFor(() => expect(refresh).toHaveBeenCalledOnce());
		expect(owner.setTrustedLan).toHaveBeenCalledWith(false);
		expect(screen.queryByText('Any device becomes owner?')).toBeNull();
	});
});

describe('computer use', () => {
	it('is off with the reason as hint when the host cannot do it', async () => {
		host.status = { available: false, reason: 'docker', enabled: false };
		render(SecurityPage);
		expect(await screen.findByText('Unavailable in Docker')).toBeTruthy();
		expect((screen.getByRole('switch', { name: 'Computer use' }) as HTMLButtonElement).disabled).toBe(true);
	});

	it('turns on through the shared store', async () => {
		render(SecurityPage);
		const toggle = screen.getByRole('switch', { name: 'Computer use' });
		await waitFor(() => expect(computer.get).toHaveBeenCalled());
		await fireEvent.click(toggle);
		await waitFor(() => expect(computer.set).toHaveBeenCalledWith(true));
	});

	it('marks this computer as agents-only, which skips the first-use prompt', async () => {
		render(SecurityPage);
		const toggle = screen.getByRole('switch', { name: 'Only agents use this computer' });
		expect(screen.getByText('Skips the first-use prompt')).toBeTruthy();
		await waitFor(() => expect(computer.get).toHaveBeenCalled());
		await waitFor(() => expect((toggle as HTMLButtonElement).disabled).toBe(false));
		expect(toggle.getAttribute('aria-checked')).toBe('false');
		await fireEvent.click(toggle);
		await waitFor(() => expect(computer.setMode).toHaveBeenCalledWith('dedicated_desktop'));
	});

	it('cannot mark an agents-only computer when the host has no computer use', async () => {
		host.status = { available: false, reason: 'docker', enabled: false };
		render(SecurityPage);
		await screen.findByText('Unavailable in Docker');
		const toggle = screen.getByRole('switch', { name: 'Only agents use this computer' }) as HTMLButtonElement;
		expect(toggle.disabled).toBe(true);
	});
});

describe('code sandbox', () => {
	it('says the sandbox is active', async () => {
		render(SecurityPage);
		expect(await screen.findByText('Active')).toBeTruthy();
	});

	it('distinguishes unavailable, off and checking, with the reason behind the help button', async () => {
		sandbox = { mode: 'auto', state: 'unavailable', active: false, reason: 'bwrap: No permissions to create a new namespace' };
		const view = render(SecurityPage);
		expect(await screen.findByText('Unavailable')).toBeTruthy();
		await fireEvent.focus(screen.getByRole('button', { name: 'About Code sandbox' }));
		expect((await screen.findByRole('tooltip')).textContent).toContain('No permissions to create a new namespace');
		view.unmount();
		sandbox = { mode: 'off', state: 'off', active: false, reason: 'CODE_MODE_OS_SANDBOX is off' };
		const second = render(SecurityPage);
		expect(await screen.findByText('Off')).toBeTruthy();
		second.unmount();
		sandbox = { mode: 'auto', state: 'checking', active: false, reason: '' };
		render(SecurityPage);
		await waitFor(() => expect(transport.get).toHaveBeenCalledWith('/deployment'));
		expect(screen.getByText('Checking')).toBeTruthy();
	});

	it('opens the install guide in the docs panel', async () => {
		render(SecurityPage);
		await fireEvent.click(screen.getByRole('button', { name: 'Open the code sandbox guide' }));
		expect(layout.openFeaturePage).toHaveBeenCalledWith('docs', 'getting-started/install-docker-compose');
	});
});

describe('security instructions', () => {
	async function openEditor() {
		await fireEvent.click(await screen.findByRole('button', { name: /^Security instructions/ }));
		return screen.getByLabelText('Security instructions', { selector: 'textarea' }) as HTMLTextAreaElement;
	}

	it('saves edits explicitly and refuses an empty text', async () => {
		render(SecurityPage);
		const area = await openEditor();
		expect(area.value).toBe('Be careful.');
		const save = screen.getByRole('button', { name: 'Save changes' }) as HTMLButtonElement;
		expect(save.disabled).toBe(true);
		await fireEvent.input(area, { target: { value: '   ' } });
		expect(screen.getByText('Unsaved changes')).toBeTruthy();
		await fireEvent.click(save);
		expect(screen.getByRole('alert').textContent).toBe('Instructions cannot be empty. Use Revert to restore defaults.');
		expect(transport.put).not.toHaveBeenCalled();
		await fireEvent.input(area, { target: { value: 'Never push to main.' } });
		await fireEvent.click(save);
		await waitFor(() =>
			expect(transport.put).toHaveBeenCalledWith('/settings/security-instructions', { instructions: 'Never push to main.' })
		);
		await waitFor(() => expect(screen.queryByText('Unsaved changes')).toBeNull());
	});

	it('reverts to the default only after confirming', async () => {
		render(SecurityPage);
		const area = await openEditor();
		await fireEvent.click(screen.getByRole('button', { name: 'Revert to default' }));
		expect(screen.getByText('Discard customizations?')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
		expect(transport.delete).not.toHaveBeenCalled();
		await fireEvent.click(screen.getByRole('button', { name: 'Revert to default' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Revert' }));
		await waitFor(() => expect(transport.delete).toHaveBeenCalledWith('/settings/security-instructions'));
		await waitFor(() => expect(area.value).toBe('Default rules.'));
		expect(screen.getByRole('button', { name: /^Security instructions/ }).textContent).toContain('Default');
	});

	it('keeps unsaved edits when the instructions change elsewhere, then loads the latest on request', async () => {
		render(SecurityPage);
		const area = await openEditor();
		await fireEvent.input(area, { target: { value: 'My draft' } });
		instructions = { ...instructions, current: 'Changed on another device.' };
		resourceSync.invalidate(['settings']);
		expect(await screen.findByText('Changed elsewhere, your edits kept')).toBeTruthy();
		expect(area.value).toBe('My draft');
		await fireEvent.click(screen.getByRole('button', { name: 'Load latest' }));
		expect(area.value).toBe('Changed on another device.');
	});

	it('follows a change made elsewhere when there are no edits', async () => {
		render(SecurityPage);
		const area = await openEditor();
		instructions = { ...instructions, current: 'Changed on another device.' };
		resourceSync.invalidate(['settings']);
		await waitFor(() => expect(area.value).toBe('Changed on another device.'));
		expect(screen.queryByText('Changed elsewhere, your edits kept')).toBeNull();
	});
});
