import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const transport = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn(), patch: vi.fn() }));
const host = vi.hoisted(() => ({
	desktop: null as unknown,
	status: { available: true, reason: 'ok', enabled: false } as Record<string, unknown>
}));
const computer = vi.hoisted(() => ({ get: vi.fn(), set: vi.fn(), setMode: vi.fn(), stop: vi.fn() }));

vi.mock('$lib/api/client', () => ({
	api: transport,
	getErrorMessage: (e: { detail?: string }, fallback: string) => e?.detail ?? fallback,
	isApiError: (e: unknown) => typeof e === 'object' && e !== null && 'status' in e && 'detail' in e
}));
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

import AgentsPage from './AgentsPage.svelte';
import { resourceSync } from '$lib/services/resource-sync';
import { assertCategoryCoverage } from '$lib/settings/testing/coverage';

let instructions: { current: string; default: string; is_customized: boolean };

/** The row's value button, once its data has loaded (it is busy and disabled before). */
async function loadedButton(name: RegExp) {
	return waitFor(() => {
		const button = screen.getByRole('button', { name }) as HTMLButtonElement;
		expect(button.disabled).toBe(false);
		return button;
	});
}

beforeEach(() => {
	vi.clearAllMocks();
	host.desktop = null;
	host.status = { available: true, reason: 'ok', enabled: false };
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
	it('renders a row for every Agent behavior entry in the desktop app', async () => {
		host.desktop = { agentAccess: { title: 'Agents run as you', message: 'm', points: [], footer: 'f' } };
		await assertCategoryCoverage('agents', () => render(AgentsPage).container);
	});

	it('leaves "Agents run as you" out of the web app', () => {
		render(AgentsPage);
		expect(screen.queryByText('Agents run as you')).toBeNull();
	});
});

describe('while loading', () => {
	it('draws the loaded row\'s value button, busy and closed, so nothing grows when the data lands (#1301)', async () => {
		let answer!: (value: typeof instructions) => void;
		transport.get.mockImplementation((path: string) =>
			path === '/settings/security-instructions' ? new Promise((done) => (answer = done)) : Promise.resolve({})
		);
		render(AgentsPage);
		const chat = screen.getByRole('button', { name: /^Chat instructions/ }) as HTMLButtonElement;
		expect(chat.classList.contains('st-vbtn')).toBe(true);
		expect(chat.disabled).toBe(true);
		expect(chat.getAttribute('aria-busy')).toBe('true');
		expect(chat.getAttribute('aria-expanded')).toBe('false');
		expect(chat.textContent).toContain('Loading…');
		answer({ ...instructions });
		expect(((await screen.findByRole('button', { name: 'Chat instructions Customized' })) as HTMLButtonElement).disabled).toBe(false);
	});

	it('keeps the same shape when the data cannot be read: a disabled, not busy, value and a retry', async () => {
		transport.get.mockImplementation((path: string) =>
			path === '/settings/security-instructions' ? Promise.reject(new Error('boom')) : Promise.resolve({})
		);
		render(AgentsPage);
		const chat = await screen.findByRole('button', { name: 'Chat instructions Unavailable' });
		expect(within(chat.closest('.st-row') as HTMLElement).getByRole('button', { name: 'Try again' })).toBeTruthy();
		expect((chat as HTMLButtonElement).disabled).toBe(true);
		expect(chat.hasAttribute('aria-busy')).toBe(false);
	});
});

describe('a failed load inserts nothing between the groups (#1403)', () => {
	it('says why in the row\'s hint, keeps Try again, and tells a screen reader in full', async () => {
		let fail = true;
		transport.get.mockImplementation(async (path: string) => {
			if (path === '/settings/security-instructions') {
				if (fail) throw { status: 500, detail: 'Database unavailable' };
				return { ...instructions };
			}
			return {};
		});
		const { container } = render(AgentsPage);
		const chat = await screen.findByRole('button', { name: 'Chat instructions Unavailable' });
		const hint = (button: HTMLElement) => button.closest('.st-row')?.querySelector('.st-rh')?.textContent;
		await waitFor(() => expect(hint(chat)).toBe('Could not load (error 500)'));
		// The value button is described by that hint.
		expect(document.getElementById(chat.getAttribute('aria-describedby') ?? '')?.textContent).toBe('Could not load (error 500)');
		expect(container.querySelector('p.st-msg')).toBeNull();
		const alerts = screen.getAllByRole('alert');
		expect(alerts.map((a) => a.textContent?.trim())).toEqual([
			'Could not load the chat instructions. The worker answered with an error (status 500).'
		]);
		for (const alert of alerts) expect(alert.classList.contains('st-vh')).toBe(true);

		const retry = within(chat.closest('.st-row') as HTMLElement).getByRole('button', { name: 'Try again' });
		expect(document.getElementById(retry.getAttribute('aria-describedby') ?? '')?.textContent).toContain('status 500');
		fail = false;
		await fireEvent.click(retry);
		expect(await loadedButton(/^Chat instructions Customized/)).toBeTruthy();
		expect(screen.queryByRole('alert')).toBeNull();
	});

	it('shows a non-owner "Available after approval" with no alert and no retry', async () => {
		transport.get.mockImplementation(async (path: string) => {
			if (path === '/settings/security-instructions') throw { status: 403, detail: 'Forbidden' };
			return {};
		});
		const { container } = render(AgentsPage);
		const chat = await screen.findByRole('button', { name: 'Chat instructions Available after approval' });
		expect(chat.closest('.st-row')?.querySelector('.st-rh')?.textContent).toBe('Sent to every agent');
		expect(screen.queryByRole('alert')).toBeNull();
		expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull();
		expect(container.querySelector('p.st-msg')).toBeNull();
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
		const { container } = render(AgentsPage);
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
		const { container } = render(AgentsPage);
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

	it('keeps keyboard focus while it retries, and moves it to the row\'s value once loaded', async () => {
		const state = { fail: true };
		failInstructions(state);
		render(AgentsPage);
		const chat = await screen.findByRole('button', { name: 'Chat instructions Unavailable' });
		const retry = within(rowOf(chat)).getByRole('button', { name: 'Try again' }) as HTMLButtonElement;
		expect(screen.getAllByRole('button', { name: 'Try again' })).toHaveLength(1);
		const alert = screen.getByRole('alert');
		expect(alert.textContent).toBe('Could not load the chat instructions. The worker did not answer.');
		expect(document.getElementById(retry.getAttribute('aria-describedby') ?? '')).toBe(alert);
		let answer!: (value: typeof instructions) => void;
		transport.get.mockImplementation((path: string) =>
			path === '/settings/security-instructions' ? new Promise((done) => (answer = done)) : Promise.resolve({})
		);
		retry.focus();
		await fireEvent.click(retry);
		// Busy, but still a focusable button: not disabled, so focus does not drop to the page.
		expect(retry.textContent?.trim()).toBe('Retrying…');
		expect(retry.disabled).toBe(false);
		expect(retry.getAttribute('aria-disabled')).toBe('true');
		expect(document.activeElement).toBe(retry);
		answer({ ...instructions });
		const value = await loadedButton(/^Chat instructions Customized/);
		await waitFor(() => expect(document.activeElement).toBe(value));
	});

	it('moves focus off a Try again that a background read removes, though it was not pressed', async () => {
		const state = { fail: true };
		failInstructions(state);
		render(AgentsPage);
		const chat = await screen.findByRole('button', { name: 'Chat instructions Unavailable' });
		within(rowOf(chat)).getByRole('button', { name: 'Try again' }).focus();
		state.fail = false;
		resourceSync.invalidate(['settings']);
		const value = await loadedButton(/^Chat instructions Customized/);
		await waitFor(() => expect(document.activeElement).toBe(value));
	});

	it('moves no focus once the page is gone', async () => {
		failInstructions({ fail: true });
		render(AgentsPage);
		const chat = await screen.findByRole('button', { name: 'Chat instructions Unavailable' });
		let answer!: (value: typeof instructions) => void;
		transport.get.mockImplementation((path: string) =>
			path === '/settings/security-instructions' ? new Promise((done) => (answer = done)) : Promise.resolve({})
		);
		const retry = within(rowOf(chat)).getByRole('button', { name: 'Try again' });
		retry.focus();
		await fireEvent.click(retry);
		cleanup();
		answer({ ...instructions });
		await new Promise((done) => setTimeout(done, 0));
		expect(document.activeElement).toBe(document.body);
	});

	it('leaves focus where the reader moved it', async () => {
		failInstructions({ fail: true });
		render(AgentsPage);
		const chat = await screen.findByRole('button', { name: 'Chat instructions Unavailable' });
		let answer!: (value: typeof instructions) => void;
		transport.get.mockImplementation((path: string) =>
			path === '/settings/security-instructions' ? new Promise((done) => (answer = done)) : Promise.resolve({})
		);
		const retry = within(rowOf(chat)).getByRole('button', { name: 'Try again' });
		retry.focus();
		await fireEvent.click(retry);
		const elsewhere = screen.getByRole('button', { name: 'About Computer use' });
		elsewhere.focus();
		answer({ ...instructions });
		await loadedButton(/^Chat instructions Customized/);
		await new Promise((done) => setTimeout(done, 0));
		expect(document.activeElement).toBe(elsewhere);
	});
});

describe('computer use', () => {
	it('is off with the reason as hint when the host cannot do it', async () => {
		host.status = { available: false, reason: 'docker', enabled: false };
		render(AgentsPage);
		expect(await screen.findByText('Unavailable in Docker')).toBeTruthy();
		expect((screen.getByRole('switch', { name: 'Computer use' }) as HTMLButtonElement).disabled).toBe(true);
	});

	it('shows the switch off on a host that cannot run it, even when the saved setting is on', async () => {
		host.status = { available: false, reason: 'docker', enabled: true };
		render(AgentsPage);
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
		render(AgentsPage);
		expect(await screen.findByText('Wayland is not supported')).toBeTruthy();
		expect((screen.getByRole('switch', { name: 'Computer use' }) as HTMLButtonElement).disabled).toBe(true);
		await fireEvent.focus(screen.getByRole('button', { name: 'About Computer use' }));
		expect((await screen.findByRole('tooltip')).textContent).toContain('pick an X11 session');
	});

	it('names missing Linux packages when the server sends no detail', async () => {
		host.status = { available: false, reason: 'missing-system-packages', enabled: false };
		render(AgentsPage);
		expect(await screen.findByText('System libraries missing')).toBeTruthy();
		await fireEvent.focus(screen.getByRole('button', { name: 'About Computer use' }));
		expect((await screen.findByRole('tooltip')).textContent).toContain('Screen-capture libraries are missing');
	});

	it('turns on through the shared store', async () => {
		render(AgentsPage);
		const toggle = screen.getByRole('switch', { name: 'Computer use' });
		await waitFor(() => expect(computer.get).toHaveBeenCalled());
		await fireEvent.click(toggle);
		await waitFor(() => expect(computer.set).toHaveBeenCalledWith(true));
	});

	it('marks this computer as agents-only, which skips the first-use prompt', async () => {
		render(AgentsPage);
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
		render(AgentsPage);
		await screen.findByText('Unavailable in Docker');
		const toggle = screen.getByRole('switch', { name: 'Only agents use this computer' }) as HTMLButtonElement;
		expect(toggle.disabled).toBe(true);
	});
});

it('does not offer an OS sandbox setting or fetch its retired status', async () => {
	render(AgentsPage);
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
		render(AgentsPage);
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
		render(AgentsPage);
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
		render(AgentsPage);
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
		render(AgentsPage);
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
		render(AgentsPage);
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
		render(AgentsPage);
		await openEditor();
		expect(screen.getByText('Your saved text matches the current default.')).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'View default' })).toBeNull();
	});

	it('shows no comparison note while the default is in use', async () => {
		instructions = { current: 'Default rules.', default: 'Default rules.', is_customized: false };
		render(AgentsPage);
		await openEditor();
		expect(screen.queryByText(/current default/)).toBeNull();
		expect((screen.getByRole('button', { name: 'Revert to default' }) as HTMLButtonElement).disabled).toBe(true);
	});

	it('keeps unsaved edits when the instructions change elsewhere, then loads the latest on request', async () => {
		render(AgentsPage);
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
		render(AgentsPage);
		const area = await openEditor();
		instructions = { ...instructions, current: 'Changed on another device.' };
		resourceSync.invalidate(['settings']);
		await waitFor(() => expect(area.value).toBe('Changed on another device.'));
		expect(screen.queryByText('Changed elsewhere, your edits kept')).toBeNull();
	});
});
