import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
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

vi.mock('$lib/api/owner', () => owner);
vi.mock('$lib/stores/owner-access.svelte', () => ({ ownerAccess: { refresh } }));
vi.mock('$lib/api/client', () => ({
	api: transport,
	getErrorMessage: (e: { detail?: string }, fallback: string) => e?.detail ?? fallback,
	isApiError: (e: unknown) => typeof e === 'object' && e !== null && 'status' in e && 'detail' in e
}));
vi.mock('$lib/stores/layout.svelte', () => ({ layout }));

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

/** The row's value button, once its data has loaded (it is busy and disabled before). */
async function loadedButton(name: RegExp) {
	return waitFor(() => {
		const button = screen.getByRole('button', { name }) as HTMLButtonElement;
		expect(button.disabled).toBe(false);
		return button;
	});
}

async function openPassword() {
	await fireEvent.click(await loadedButton(/^Owner password/));
}

async function fill(values: Record<string, string>) {
	for (const [name, value] of Object.entries(values)) await fireEvent.input(input(name), { target: { value } });
	await fireEvent.submit(screen.getByRole('form', { name: 'Owner password' }));
}

beforeEach(() => {
	vi.clearAllMocks();
	owner.fetchOwnerStatus.mockResolvedValue(loaded());
	owner.changeOwnerPassword.mockResolvedValue({ status: 'ok' });
	owner.createOwnerPassword.mockResolvedValue({ status: 'ok' });
	owner.setTrustedLan.mockResolvedValue({ trusted_lan: true });
	refresh.mockResolvedValue(null);
	transport.get.mockResolvedValue({});
	transport.put.mockResolvedValue({});
	transport.delete.mockResolvedValue({});
});
afterEach(() => {
	cleanup();
	resourceSync.clear();
});

describe('coverage', () => {
	it('renders a row for every Security entry', async () => {
		await assertCategoryCoverage('security', () => render(SecurityPage).container);
	});

	it('no longer has the agent rows; they moved to Agent behavior', () => {
		render(SecurityPage);
		for (const label of ['Chat instructions', 'Computer use', 'Only agents use this computer', 'Agents run as you']) {
			expect(screen.queryByText(label)).toBeNull();
		}
	});
});

describe('while loading', () => {
	it('draws the loaded row\'s value button and hint, busy and closed, so nothing grows when the data lands (#1301)', async () => {
		let answerOwner!: (value: ReturnType<typeof loaded>) => void;
		owner.fetchOwnerStatus.mockReturnValue(new Promise((done) => (answerOwner = done)));
		render(SecurityPage);

		const password = screen.getByRole('button', { name: /^Owner password/ }) as HTMLButtonElement;
		expect(password.classList.contains('st-vbtn')).toBe(true);
		expect(password.disabled).toBe(true);
		expect(password.getAttribute('aria-busy')).toBe('true');
		expect(password.getAttribute('aria-expanded')).toBe('false');
		expect(password.closest('.st-row')?.querySelector('.st-rh')?.textContent).toBe('Loading…');

		answerOwner(loaded());
		const ready = await screen.findByRole('button', { name: 'Owner password Set' });
		expect((ready as HTMLButtonElement).disabled).toBe(false);
		expect(ready.closest('.st-row')?.querySelector('.st-rh')?.textContent).toBe('Asked once per browser');
	});

	it('keeps the same shape when the data cannot be read: a disabled, not busy, value and a retry', async () => {
		owner.fetchOwnerStatus.mockResolvedValue({ ok: false, reason: 'unreachable' });
		render(SecurityPage);
		const password = await screen.findByRole('button', { name: 'Owner password Unavailable' });
		expect((password as HTMLButtonElement).disabled).toBe(true);
		expect(password.hasAttribute('aria-busy')).toBe(false);
		expect(password.closest('.st-row')?.querySelector('.st-rh')?.textContent).toBe('Could not load (no answer)');
		expect(within(password.closest('.st-row') as HTMLElement).getByRole('button', { name: 'Try again' })).toBeTruthy();
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
		const { container } = render(SecurityPage);
		const value = await screen.findByRole('button', { name: 'Owner password Not supported' });
		expect(value.closest('.st-row')?.querySelector('.st-rh')?.textContent).toBe('Needs a newer worker');
		// The full sentence is for a screen reader; the row says it on screen (#1403).
		expect(screen.getByRole('status', { name: '' }).textContent).toMatch(/does not support owner passwords/);
		expect(screen.getByText(/does not support owner passwords/).classList.contains('st-vh')).toBe(true);
		expect(container.querySelector('p.st-msg')).toBeNull();
		expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull();
	});
});

describe('a failed load inserts nothing between the groups (#1403)', () => {
	it('says why in the row\'s hint, keeps Try again, and tells a screen reader in full', async () => {
		owner.fetchOwnerStatus.mockResolvedValueOnce({ ok: false, reason: 'error', code: 500 });
		const { container } = render(SecurityPage);
		const password = await screen.findByRole('button', { name: 'Owner password Unavailable' });
		const hint = (button: HTMLElement) => button.closest('.st-row')?.querySelector('.st-rh')?.textContent;
		expect(hint(password)).toBe('Could not load (error 500)');

		// No visible notice between the groups; the alert takes no space.
		expect(container.querySelector('p.st-msg')).toBeNull();
		const alerts = screen.getAllByRole('alert');
		expect(alerts.map((a) => a.textContent?.trim())).toEqual([
			'Could not load owner access status. The worker answered with an error (status 500).'
		]);
		for (const alert of alerts) expect(alert.classList.contains('st-vh')).toBe(true);

		// Try again stays a focusable button in the row, described by the full error.
		const retry = within(password.closest('.st-row') as HTMLElement).getByRole('button', { name: 'Try again' });
		expect(document.getElementById(retry.getAttribute('aria-describedby') ?? '')?.textContent).toContain('status 500');
		retry.focus();
		expect(document.activeElement).toBe(retry);
		await fireEvent.click(retry);
		expect(await loadedButton(/^Owner password/)).toBeTruthy();
		expect(hint(screen.getByRole('button', { name: 'Owner password Set' }))).toBe('Asked once per browser');
		expect(screen.queryByRole('alert')).toBeNull();
	});

	it('announces a second failure after Try again', async () => {
		let answer!: (value: unknown) => void;
		owner.fetchOwnerStatus
			.mockResolvedValueOnce({ ok: false, reason: 'unreachable' })
			.mockReturnValueOnce(new Promise((done) => (answer = done)));
		render(SecurityPage);
		await screen.findByRole('alert');
		await fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
		// While it retries the alert leaves, so the next failure is inserted (and read) again.
		expect(screen.getByRole('button', { name: 'Retrying…' })).toBeTruthy();
		expect(screen.queryByRole('alert')).toBeNull();
		answer({ ok: false, reason: 'unreachable' });
		expect((await screen.findByRole('alert')).textContent?.trim()).toBe('Could not load owner access status. The worker did not answer.');
	});

	it('shows a non-owner "Owner only" with no alert and no retry', async () => {
		owner.fetchOwnerStatus.mockResolvedValueOnce({ ok: false, reason: 'error', code: 403 });
		const { container } = render(SecurityPage);
		const password = await screen.findByRole('button', { name: /^Owner password / });
		await waitFor(() => expect(password.closest('.st-row')?.querySelector('.st-rh')?.textContent).toBe('Owner only'));
		expect(screen.queryByRole('alert')).toBeNull();
		expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull();
		expect(container.querySelector('p.st-msg')).toBeNull();
	});
});

describe('Try again after a failed load', () => {
	it('keeps keyboard focus while it retries, and moves it to the row\'s value once loaded', async () => {
		let answer!: (value: unknown) => void;
		owner.fetchOwnerStatus
			.mockResolvedValueOnce({ ok: false, reason: 'unreachable' })
			.mockReturnValueOnce(new Promise((done) => (answer = done)))
			.mockResolvedValueOnce(loaded());
		render(SecurityPage);
		await screen.findByRole('alert');
		const retry = screen.getByRole('button', { name: 'Try again' }) as HTMLButtonElement;
		retry.focus();
		await fireEvent.click(retry);
		// Busy, but still a focusable button: not disabled, so focus does not drop to the page.
		expect(retry.textContent?.trim()).toBe('Retrying…');
		expect(retry.disabled).toBe(false);
		expect(retry.getAttribute('aria-disabled')).toBe('true');
		expect(document.activeElement).toBe(retry);
		await fireEvent.click(retry);
		expect(owner.fetchOwnerStatus).toHaveBeenCalledTimes(2);

		answer({ ok: false, reason: 'unreachable' });
		await screen.findByRole('alert');
		expect(document.activeElement).toBe(retry);
		expect(retry.hasAttribute('aria-disabled')).toBe(false);

		await fireEvent.click(retry);
		const value = await loadedButton(/^Owner password Set/);
		await waitFor(() => expect(document.activeElement).toBe(value));
	});

	it('moves no focus once the page is gone', async () => {
		let answer!: (value: unknown) => void;
		owner.fetchOwnerStatus
			.mockResolvedValueOnce({ ok: false, reason: 'unreachable' })
			.mockReturnValueOnce(new Promise((done) => (answer = done)));
		render(SecurityPage);
		const retry = await screen.findByRole('button', { name: 'Try again' });
		retry.focus();
		await fireEvent.click(retry);
		cleanup();
		answer(loaded());
		await new Promise((done) => setTimeout(done, 0));
		expect(document.activeElement).toBe(document.body);
	});

	it('leaves focus where the reader moved it', async () => {
		let answer!: (value: unknown) => void;
		owner.fetchOwnerStatus
			.mockResolvedValueOnce({ ok: false, reason: 'unreachable' })
			.mockReturnValueOnce(new Promise((done) => (answer = done)));
		render(SecurityPage);
		const retry = await screen.findByRole('button', { name: 'Try again' });
		retry.focus();
		await fireEvent.click(retry);
		const elsewhere = screen.getByRole('button', { name: 'About Terminals on other workers' });
		elsewhere.focus();
		answer(loaded());
		await loadedButton(/^Owner password Set/);
		await new Promise((done) => setTimeout(done, 0));
		expect(document.activeElement).toBe(elsewhere);
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
