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
const host = vi.hoisted(() => ({
	desktop: null as unknown,
	status: { available: true, reason: 'ok', enabled: false } as Record<string, unknown>
}));
const computer = vi.hoisted(() => ({ get: vi.fn(), set: vi.fn(), setMode: vi.fn(), stop: vi.fn() }));

vi.mock('$lib/api/owner', () => owner);
vi.mock('$lib/stores/owner-access.svelte', () => ({ ownerAccess: { refresh } }));
vi.mock('$lib/api/client', () => ({
	api: transport,
	getErrorMessage: (e: { detail?: string }, fallback: string) => e?.detail ?? fallback,
	isApiError: (e: unknown) => typeof e === 'object' && e !== null && 'status' in e && 'detail' in e
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
	host.desktop = null;
	host.status = { available: true, reason: 'ok', enabled: false };
	owner.fetchOwnerStatus.mockResolvedValue(loaded());
	owner.changeOwnerPassword.mockResolvedValue({ status: 'ok' });
	owner.createOwnerPassword.mockResolvedValue({ status: 'ok' });
	owner.setTrustedLan.mockResolvedValue({ trusted_lan: true });
	refresh.mockResolvedValue(null);
	instructions = { current: 'Be careful.', default: 'Default rules.', is_customized: true };
	transport.get.mockImplementation(async (path: string) => {
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

describe('while loading', () => {
	it('draws the loaded rows\' value buttons and hint, busy and closed, so nothing grows when the data lands (#1301)', async () => {
		let answerOwner!: (value: ReturnType<typeof loaded>) => void;
		let answerInstructions!: (value: typeof instructions) => void;
		owner.fetchOwnerStatus.mockReturnValue(new Promise((done) => (answerOwner = done)));
		transport.get.mockImplementation((path: string) =>
			path === '/settings/security-instructions' ? new Promise((done) => (answerInstructions = done)) : Promise.resolve({})
		);
		render(SecurityPage);

		const password = screen.getByRole('button', { name: /^Owner password/ });
		const chat = screen.getByRole('button', { name: /^Chat instructions/ });
		for (const button of [password, chat]) {
			expect(button.classList.contains('st-vbtn')).toBe(true);
			expect((button as HTMLButtonElement).disabled).toBe(true);
			expect(button.getAttribute('aria-busy')).toBe('true');
			expect(button.getAttribute('aria-expanded')).toBe('false');
		}
		expect(password.closest('.st-row')?.querySelector('.st-rh')?.textContent).toBe('Loading…');
		expect(chat.textContent).toContain('Loading…');

		answerOwner(loaded());
		answerInstructions({ ...instructions });
		const ready = await screen.findByRole('button', { name: 'Owner password Set' });
		expect((ready as HTMLButtonElement).disabled).toBe(false);
		expect(ready.closest('.st-row')?.querySelector('.st-rh')?.textContent).toBe('Asked once per browser');
		expect(((await screen.findByRole('button', { name: 'Chat instructions Customized' })) as HTMLButtonElement).disabled).toBe(false);
	});

	it('keeps the same shape when the data cannot be read: a disabled, not busy, value and a retry', async () => {
		owner.fetchOwnerStatus.mockResolvedValue({ ok: false, reason: 'unreachable' });
		transport.get.mockImplementation((path: string) =>
			path === '/settings/security-instructions' ? Promise.reject(new Error('boom')) : Promise.resolve({})
		);
		render(SecurityPage);
		const password = await screen.findByRole('button', { name: 'Owner password Unavailable' });
		expect((password as HTMLButtonElement).disabled).toBe(true);
		expect(password.hasAttribute('aria-busy')).toBe(false);
		expect(password.closest('.st-row')?.querySelector('.st-rh')?.textContent).toBe('Could not load (no answer)');
		expect(within(password.closest('.st-row') as HTMLElement).getByRole('button', { name: 'Try again' })).toBeTruthy();
		const chat = await screen.findByRole('button', { name: 'Chat instructions Unavailable' });
		expect(within(chat.closest('.st-row') as HTMLElement).getByRole('button', { name: 'Try again' })).toBeTruthy();
		expect((chat as HTMLButtonElement).disabled).toBe(true);
		expect(chat.hasAttribute('aria-busy')).toBe(false);
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
	it('says why in the rows\' hints, keeps Try again, and tells a screen reader in full', async () => {
		owner.fetchOwnerStatus.mockResolvedValueOnce({ ok: false, reason: 'error', code: 500 });
		transport.get.mockImplementation(async (path: string) => {
			if (path === '/settings/security-instructions') throw { status: 500, detail: 'Database unavailable' };
			return {};
		});
		const { container } = render(SecurityPage);
		const password = await screen.findByRole('button', { name: 'Owner password Unavailable' });
		const hint = (button: HTMLElement) => button.closest('.st-row')?.querySelector('.st-rh')?.textContent;
		expect(hint(password)).toBe('Could not load (error 500)');
		const chat = await screen.findByRole('button', { name: 'Chat instructions Unavailable' });
		await waitFor(() => expect(hint(chat)).toBe('Could not load (error 500)'));
		// The value buttons are described by those hints.
		expect(document.getElementById(chat.getAttribute('aria-describedby') ?? '')?.textContent).toBe('Could not load (error 500)');

		// No visible notice between the groups; the alerts take no space.
		expect(container.querySelector('p.st-msg')).toBeNull();
		const alerts = screen.getAllByRole('alert');
		expect(alerts.map((a) => a.textContent?.trim())).toEqual([
			'Could not load owner access status. The worker answered with an error (status 500).',
			'Could not load the chat instructions. The worker answered with an error (status 500).'
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
		// The same Try again read the instructions again too; they failed again.
		await waitFor(() =>
			expect(screen.getAllByRole('alert').map((a) => a.textContent)).toEqual([
				'Could not load the chat instructions. The worker answered with an error (status 500).'
			])
		);
		expect(transport.get.mock.calls.filter(([path]) => path === '/settings/security-instructions')).toHaveLength(2);
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
		transport.get.mockImplementation(async (path: string) => {
			if (path === '/settings/security-instructions') throw { status: 403, detail: 'Forbidden' };
			return {};
		});
		const { container } = render(SecurityPage);
		const password = await screen.findByRole('button', { name: /^Owner password / });
		await waitFor(() => expect(password.closest('.st-row')?.querySelector('.st-rh')?.textContent).toBe('Owner only'));
		expect(screen.queryByRole('alert')).toBeNull();
		expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull();
		expect(container.querySelector('p.st-msg')).toBeNull();
		// Chat instructions keeps its normal hint.
		const chat = await screen.findByRole('button', { name: /^Chat instructions / });
		expect(chat.closest('.st-row')?.querySelector('.st-rh')?.textContent).toBe('Sent to every agent');
	});

	it('clears the load error once a later read of the instructions succeeds', async () => {
		let fail = true;
		transport.get.mockImplementation(async (path: string) => {
			if (path === '/settings/security-instructions') {
				if (fail) throw new Error('offline');
				return { ...instructions };
			}
			return {};
		});
		const { container } = render(SecurityPage);
		const chat = await screen.findByRole('button', { name: 'Chat instructions Unavailable' });
		expect(chat.closest('.st-row')?.querySelector('.st-rh')?.textContent).toBe('Could not load (no answer)');
		fail = false;
		resourceSync.invalidate(['settings']);
		await loadedButton(/^Chat instructions/);
		expect(screen.queryByRole('alert')).toBeNull();
		expect(container.querySelector('p.st-msg')).toBeNull();
	});

	it('drops the load error when a later read says owner only, and stays quiet once a read works', async () => {
		let answer: 500 | 403 | 200 = 500;
		transport.get.mockImplementation(async (path: string) => {
			if (path === '/settings/security-instructions') {
				if (answer !== 200) throw { status: answer, detail: answer === 500 ? 'Database unavailable' : 'Forbidden' };
				return { ...instructions };
			}
			return {};
		});
		const { container } = render(SecurityPage);
		await screen.findByRole('button', { name: 'Chat instructions Unavailable' });
		const loadError = /Could not load the chat instructions/;
		await screen.findByText(loadError);

		answer = 403;
		resourceSync.invalidate(['settings']);
		await screen.findByRole('button', { name: 'Chat instructions Available after approval' });
		expect(screen.queryByText(loadError)).toBeNull();
		expect(screen.queryByText('Database unavailable')).toBeNull();
		expect(container.querySelector('p.st-msg')).toBeNull();

		answer = 200;
		resourceSync.invalidate(['settings']);
		await loadedButton(/^Chat instructions/);
		expect(screen.queryByText(loadError)).toBeNull();
		expect(screen.queryByRole('alert')).toBeNull();
		expect(container.querySelector('p.st-msg')).toBeNull();
	});
});

describe('Try again after a failed load', () => {
	const failInstructions = (state: { fail: boolean }) =>
		transport.get.mockImplementation(async (path: string) => {
			if (path === '/settings/security-instructions') {
				if (state.fail) throw new Error('Failed to fetch');
				return { ...instructions };
			}
			return {};
		});
	const rowOf = (button: HTMLElement) => button.closest('.st-row') as HTMLElement;

	it('reloads every failed row, not only the one it sits in', async () => {
		const state = { fail: true };
		owner.fetchOwnerStatus.mockResolvedValueOnce({ ok: false, reason: 'error', code: 500 });
		failInstructions(state);
		render(SecurityPage);
		const password = await screen.findByRole('button', { name: 'Owner password Unavailable' });
		await screen.findByRole('button', { name: 'Chat instructions Unavailable' });
		state.fail = false;
		await fireEvent.click(within(rowOf(password)).getByRole('button', { name: 'Try again' }));
		expect(await screen.findByRole('button', { name: 'Owner password Set' })).toBeTruthy();
		expect(await screen.findByRole('button', { name: 'Chat instructions Customized' })).toBeTruthy();
		expect(screen.queryByRole('alert')).toBeNull();
		expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull();
	});

	it('is offered in the instructions row when only the instructions failed, and says why in full', async () => {
		const state = { fail: true };
		failInstructions(state);
		const { container } = render(SecurityPage);
		const chat = await screen.findByRole('button', { name: 'Chat instructions Unavailable' });
		const retry = within(rowOf(chat)).getByRole('button', { name: 'Try again' });
		expect(screen.getAllByRole('button', { name: 'Try again' })).toHaveLength(1);
		const alert = screen.getByRole('alert');
		expect(alert.textContent).toBe('Could not load the chat instructions. The worker did not answer.');
		expect(document.getElementById(retry.getAttribute('aria-describedby') ?? '')).toBe(alert);
		expect(container.querySelector('p.st-msg')).toBeNull();
		state.fail = false;
		await fireEvent.click(retry);
		expect(await screen.findByRole('button', { name: 'Chat instructions Customized' })).toBeTruthy();
		expect(owner.fetchOwnerStatus).toHaveBeenCalledOnce();
	});

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

	it('moves focus as soon as its row recovers, while the other row is still retrying', async () => {
		const state = { fail: true };
		let answer!: (value: typeof instructions) => void;
		owner.fetchOwnerStatus.mockResolvedValueOnce({ ok: false, reason: 'error', code: 500 });
		failInstructions(state);
		render(SecurityPage);
		const password = await screen.findByRole('button', { name: 'Owner password Unavailable' });
		await screen.findByRole('button', { name: 'Chat instructions Unavailable' });
		transport.get.mockImplementation((path: string) =>
			path === '/settings/security-instructions' ? new Promise((done) => (answer = done)) : Promise.resolve({})
		);
		const retry = within(rowOf(password)).getByRole('button', { name: 'Try again' });
		retry.focus();
		await fireEvent.click(retry);
		const value = await loadedButton(/^Owner password Set/);
		await waitFor(() => expect(document.activeElement).toBe(value));
		// The instructions are still out: their row keeps its own (busy) Try again.
		expect(screen.getByRole('button', { name: 'Retrying…' })).toBeTruthy();
		answer({ ...instructions });
		expect(await screen.findByRole('button', { name: 'Chat instructions Customized' })).toBeTruthy();
		expect(document.activeElement).toBe(value);
	});

	// Both rows fail; the retry runs with each answer held until the test sends it.
	async function retryBothFrom(start: 'owner' | 'instructions') {
		owner.fetchOwnerStatus.mockResolvedValueOnce({ ok: false, reason: 'error', code: 500 });
		failInstructions({ fail: true });
		render(SecurityPage);
		const password = await screen.findByRole('button', { name: 'Owner password Unavailable' });
		const chat = await screen.findByRole('button', { name: 'Chat instructions Unavailable' });
		let ownerAnswer!: (value: unknown) => void;
		let instrAnswer!: (value: unknown) => void;
		let instrFail!: (error: unknown) => void;
		owner.fetchOwnerStatus.mockReturnValueOnce(new Promise((done) => (ownerAnswer = done)));
		transport.get.mockImplementation((path: string) =>
			path === '/settings/security-instructions'
				? new Promise((done, fail) => ((instrAnswer = done), (instrFail = fail)))
				: Promise.resolve({})
		);
		const retry = within(rowOf(start === 'owner' ? password : chat)).getByRole('button', { name: 'Try again' });
		const other = within(rowOf(start === 'owner' ? chat : password)).getByRole('button', { name: 'Try again' });
		retry.focus();
		await fireEvent.click(retry);
		const settle = () => new Promise((done) => setTimeout(done, 0));
		return { ownerAnswer, instrAnswer: (v: unknown) => instrAnswer(v), instrFail: (e: unknown) => instrFail(e), other, settle };
	}

	it('follows focus to the other Try again and on to its row when that row recovers', async () => {
		const run = await retryBothFrom('owner');
		run.ownerAnswer({ ok: false, reason: 'unsupported' });
		await waitFor(() => expect(document.activeElement).toBe(run.other));
		run.instrAnswer({ ...instructions });
		const value = await loadedButton(/^Chat instructions Customized/);
		await waitFor(() => expect(document.activeElement).toBe(value));
	});

	it('does the same the other way round, from the instructions row', async () => {
		const run = await retryBothFrom('instructions');
		run.instrFail({ status: 403, detail: 'Forbidden' });
		await waitFor(() => expect(document.activeElement).toBe(run.other));
		run.ownerAnswer(loaded());
		const value = await loadedButton(/^Owner password Set/);
		await waitFor(() => expect(document.activeElement).toBe(value));
	});

	it('follows the reader who Tabs onto the other Try again before it goes away', async () => {
		const run = await retryBothFrom('owner');
		run.ownerAnswer(loaded());
		const ownerValue = await loadedButton(/^Owner password Set/);
		await waitFor(() => expect(document.activeElement).toBe(ownerValue));
		run.other.focus();
		run.instrAnswer({ ...instructions });
		const value = await loadedButton(/^Chat instructions Customized/);
		await waitFor(() => expect(document.activeElement).toBe(value));
	});

	it('moves focus off a Try again that a background read removes, though it was not pressed', async () => {
		const state = { fail: true };
		failInstructions(state);
		render(SecurityPage);
		const chat = await screen.findByRole('button', { name: 'Chat instructions Unavailable' });
		within(rowOf(chat)).getByRole('button', { name: 'Try again' }).focus();
		state.fail = false;
		resourceSync.invalidate(['settings']);
		const value = await loadedButton(/^Chat instructions Customized/);
		await waitFor(() => expect(document.activeElement).toBe(value));
	});

	it('stays on the other Try again when that load fails again, and says so again', async () => {
		const run = await retryBothFrom('owner');
		run.ownerAnswer({ ok: false, reason: 'unsupported' });
		await waitFor(() => expect(document.activeElement).toBe(run.other));
		expect(screen.queryByRole('alert')).toBeNull();
		run.instrFail(new Error('Failed to fetch'));
		expect((await screen.findByRole('alert')).textContent).toBe('Could not load the chat instructions. The worker did not answer.');
		expect(run.other.isConnected).toBe(true);
		expect(document.activeElement).toBe(run.other);
	});

	it('moves no focus once the page is gone', async () => {
		const run = await retryBothFrom('owner');
		cleanup();
		run.ownerAnswer(loaded());
		run.instrAnswer({ ...instructions });
		await run.settle();
		expect(document.activeElement).toBe(document.body);
	});

	it('leaves focus where the reader moved it', async () => {
		const run = await retryBothFrom('owner');
		const elsewhere = screen.getByRole('button', { name: 'About Computer use' });
		elsewhere.focus();
		run.ownerAnswer(loaded());
		await loadedButton(/^Owner password Set/);
		run.instrAnswer({ ...instructions });
		await loadedButton(/^Chat instructions Customized/);
		await run.settle();
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

describe('computer use', () => {
	it('is off with the reason as hint when the host cannot do it', async () => {
		host.status = { available: false, reason: 'docker', enabled: false };
		render(SecurityPage);
		expect(await screen.findByText('Unavailable in Docker')).toBeTruthy();
		expect((screen.getByRole('switch', { name: 'Computer use' }) as HTMLButtonElement).disabled).toBe(true);
	});

	it('shows the switch off on a host that cannot run it, even when the saved setting is on', async () => {
		host.status = { available: false, reason: 'docker', enabled: true };
		render(SecurityPage);
		await screen.findByText('Unavailable in Docker');
		const toggle = screen.getByRole('switch', { name: 'Computer use' });
		expect(toggle.getAttribute('aria-checked')).toBe('false');
	});

	it('explains a Linux host it cannot use, with the server detail behind the help button', async () => {
		host.status = {
			available: false,
			reason: 'wayland',
			enabled: false,
			detail: 'This is a Wayland session. Log out and pick an X11 session.'
		};
		render(SecurityPage);
		expect(await screen.findByText('Wayland is not supported')).toBeTruthy();
		expect((screen.getByRole('switch', { name: 'Computer use' }) as HTMLButtonElement).disabled).toBe(true);
		await fireEvent.focus(screen.getByRole('button', { name: 'About Computer use' }));
		expect((await screen.findByRole('tooltip')).textContent).toContain('pick an X11 session');
	});

	it('names missing Linux packages when the server sends no detail', async () => {
		host.status = { available: false, reason: 'missing-system-packages', enabled: false };
		render(SecurityPage);
		expect(await screen.findByText('System libraries missing')).toBeTruthy();
		await fireEvent.focus(screen.getByRole('button', { name: 'About Computer use' }));
		expect((await screen.findByRole('tooltip')).textContent).toContain('Screen-capture libraries are missing');
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

it('does not offer an OS sandbox setting or fetch its retired status', async () => {
	render(SecurityPage);
	await screen.findByText('Customized');
	expect(screen.queryByText('Code sandbox')).toBeNull();
	expect(transport.get).not.toHaveBeenCalledWith('/deployment');
});

describe('chat instructions', () => {
	async function openEditor() {
		await fireEvent.click(await loadedButton(/^Chat instructions/));
		return screen.getByLabelText('Chat instructions', { selector: 'textarea' }) as HTMLTextAreaElement;
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

	it('keeps a failed save\'s message through a background read', async () => {
		render(SecurityPage);
		const area = await openEditor();
		transport.put.mockRejectedValueOnce({ status: 500, detail: 'Disk full' });
		await fireEvent.input(area, { target: { value: 'Never push to main.' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
		await screen.findByText('Disk full');
		const reads = transport.get.mock.calls.length;
		resourceSync.invalidate(['settings']);
		await waitFor(() => expect(transport.get.mock.calls.length).toBeGreaterThan(reads));
		await Promise.resolve();
		expect(screen.getByText('Disk full')).toBeTruthy();
	});

	it('keeps a failed save\'s message visible when a later read says owner only and closes the editor', async () => {
		render(SecurityPage);
		const area = await openEditor();
		transport.put.mockRejectedValueOnce({ status: 500, detail: 'Disk full' });
		await fireEvent.input(area, { target: { value: 'Never push to main.' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
		await screen.findByText('Disk full');
		transport.get.mockRejectedValue({ status: 403, detail: 'Forbidden' });
		resourceSync.invalidate(['settings']);
		await screen.findByRole('button', { name: /^Chat instructions Available after approval/ });
		expect(screen.queryByLabelText('Chat instructions', { selector: 'textarea' })).toBeNull();
		expect(screen.getByRole('alert').textContent).toBe('Disk full');
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
		expect(screen.getByRole('button', { name: /^Chat instructions/ }).textContent).toContain('Default');
		expect(screen.queryByText(/current default/)).toBeNull();
	});

	it('says when the saved text differs from the default and shows the default read-only', async () => {
		render(SecurityPage);
		await openEditor();
		expect(screen.getByText('Your saved text differs from the current default.')).toBeTruthy();
		expect(screen.queryByLabelText('Current default chat instructions')).toBeNull();
		const toggle = screen.getByRole('button', { name: 'View default' });
		expect(toggle.getAttribute('aria-expanded')).toBe('false');
		await fireEvent.click(toggle);
		const shown = screen.getByLabelText('Current default chat instructions') as HTMLTextAreaElement;
		expect(shown.value).toBe('Default rules.');
		expect(shown.readOnly).toBe(true);
		expect(screen.getByLabelText('Chat instructions', { selector: 'textarea' })).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Hide default' }));
		expect(screen.queryByLabelText('Current default chat instructions')).toBeNull();
		expect(transport.put).not.toHaveBeenCalled();
		expect(transport.delete).not.toHaveBeenCalled();
	});

	it('says when a saved customization matches the default, without a compare view', async () => {
		instructions = { current: 'Default rules.', default: 'Default rules.', is_customized: true };
		render(SecurityPage);
		await openEditor();
		expect(screen.getByText('Your saved text matches the current default.')).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'View default' })).toBeNull();
	});

	it('shows no comparison note while the default is in use', async () => {
		instructions = { current: 'Default rules.', default: 'Default rules.', is_customized: false };
		render(SecurityPage);
		await openEditor();
		expect(screen.queryByText(/current default/)).toBeNull();
		expect((screen.getByRole('button', { name: 'Revert to default' }) as HTMLButtonElement).disabled).toBe(true);
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
