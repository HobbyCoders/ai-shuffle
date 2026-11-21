/**
 * Settings > Vault against a mocked Vault API: the list, adding a machine and
 * a web service (only the fields of that kind are sent), editing with only the
 * changed fields, secrets that never stay in the form, removal after a
 * confirmation, and the states in which the Vault is not shown at all (plain
 * HTTP, preview sign-in, unapproved device, locked encryption key).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import type { Writable } from 'svelte/store';

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }));
const status = vi.hoisted(() => ({ preview_access: false }));
vi.mock('$lib/api/client', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/client')>()),
	api
}));
vi.mock('$lib/stores/auth.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { isAdmin: writable(true), auth: { current: { status } } };
});
vi.mock('$lib/services/watch-resources', () => ({ watchResources: vi.fn() }));

import { isAdmin } from '$lib/stores/auth.svelte';
import { settingsNav } from '$lib/settings/navigation.svelte';
import { assertCategoryCoverage } from '$lib/settings/testing/coverage';
import { searchSource } from '$lib/settings/entries/vault';
import VaultCollection from './VaultCollection.svelte';
import {
	DRAFT_ID,
	INSECURE_TEXT,
	VaultModel,
	createBody,
	editBody,
	emptyForm,
	formFromEntry,
	problems,
	type VaultEntry
} from './vault-model.svelte';

const machine = (over: Partial<VaultEntry> = {}): VaultEntry => ({
	id: 'a'.repeat(32),
	kind: 'machine',
	label: 'HomeLab',
	target: 'root@10.0.0.251:22',
	fingerprint: 'SHA256:' + 'A'.repeat(43),
	host: '10.0.0.251',
	port: 22,
	username: 'root',
	uses_key: false,
	...over
});
const service = (over: Partial<VaultEntry> = {}): VaultEntry => ({
	id: 'b'.repeat(32),
	kind: 'service',
	label: 'Cloudflare',
	target: 'https://api.cloudflare.com',
	fingerprint: '',
	writable: false,
	auth_header: 'Authorization',
	auth_scheme: 'Bearer',
	...over
});
let entries: VaultEntry[];
const listing = (available = true) => ({ entries, vault_available: available });
const SECRET = 'correct-horse-battery';

function secure(value: boolean) {
	Object.defineProperty(window, 'isSecureContext', { configurable: true, value });
}

async function show(item?: string) {
	settingsNav.reset();
	settingsNav.selectCategory('vault');
	if (item) settingsNav.go({ category: 'vault', item });
	const page = render(VaultCollection);
	await screen.findByText('Encrypted on this worker; never synced', { exact: false });
	return page;
}

const field = (name: string) => screen.getByRole('textbox', { name }) as HTMLInputElement;
const secretField = (name: string) => screen.getByLabelText(name) as HTMLInputElement;
async function type(input: HTMLElement, value: string) {
	await fireEvent.input(input, { target: { value } });
	await fireEvent.change(input, { target: { value } });
}
const button = (name: string) => screen.getAllByRole('button', { name }).at(-1) as HTMLButtonElement;

beforeEach(() => {
	vi.clearAllMocks();
	secure(true);
	status.preview_access = false;
	(isAdmin as unknown as Writable<boolean>).set(true);
	entries = [machine(), service()];
	api.get.mockImplementation(async () => listing());
	api.post.mockImplementation(async () => ({ id: 'c'.repeat(32) }));
	api.patch.mockResolvedValue({ updated: true, revoked: 0, host_key_cleared: false });
	api.delete.mockResolvedValue({ deleted: true, revoked: 0 });
});
afterEach(() => cleanup());

describe('Settings > Vault list', () => {
	it('lists saved sign-ins by name and address, without a secret', async () => {
		const { container } = await show();
		expect(api.get).toHaveBeenCalledWith('/vault/entries');
		expect(screen.getByRole('button', { name: /^HomeLab/ }).textContent).toContain('root@10.0.0.251:22');
		expect(screen.getByRole('button', { name: /^Cloudflare/ }).textContent).toContain('https://api.cloudflare.com');
		expect(container.querySelector('input[type="password"]')).toBeNull();
	});

	it('covers the registry entry and reaches search by name and kind only', async () => {
		await assertCategoryCoverage('vault', async () => (await show()).container);
		await show();
		await waitFor(() => expect(searchSource().map((s) => s.title)).toEqual(['Cloudflare', 'HomeLab']));
		expect(JSON.stringify(searchSource())).not.toContain('10.0.0.251');
	});

	it('offers the first sign-in when the Vault is empty', async () => {
		entries = [];
		await show();
		expect(screen.getByText('Nothing in the Vault yet')).toBeTruthy();
		await fireEvent.click(button('Add sign-in'));
		expect(settingsNav.selectedItem('vault')).toBe(DRAFT_ID);
	});

	it('reports a load failure and retries', async () => {
		api.get.mockRejectedValueOnce({ status: 500, detail: 'boom' });
		settingsNav.reset();
		settingsNav.selectCategory('vault');
		render(VaultCollection);
		expect((await screen.findByRole('alert')).textContent).toContain('Could not load the Vault');
		await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
		expect(await screen.findByRole('button', { name: /^HomeLab/ })).toBeTruthy();
	});
});

describe('Settings > Vault is not shown where a secret would be unsafe', () => {
	async function blocked() {
		settingsNav.reset();
		settingsNav.selectCategory('vault');
		render(VaultCollection);
		return (await screen.findByRole('alert')).textContent ?? '';
	}

	it('asks the worker nothing on a plain HTTP page', async () => {
		secure(false);
		expect(await blocked()).toContain('not available on this connection');
		expect(api.get).not.toHaveBeenCalled();
		expect(button('Add sign-in').disabled).toBe(true);
	});

	it('asks the worker nothing for a preview sign-in', async () => {
		status.preview_access = true;
		expect(await blocked()).toContain('preview sign-in cannot open the Vault');
		expect(api.get).not.toHaveBeenCalled();
		expect(button('Add sign-in').disabled).toBe(true);
	});

	it('shows why the worker refused this connection', async () => {
		api.get.mockRejectedValue({ status: 403, detail: INSECURE_TEXT });
		expect(await blocked()).toContain('not available on this connection');
		expect(screen.queryByRole('button', { name: /^HomeLab/ })).toBeNull();
	});

	it('is read-only for a device that is not approved', async () => {
		(isAdmin as unknown as Writable<boolean>).set(false);
		api.get.mockRejectedValue({ status: 403, detail: 'Admin access required' });
		settingsNav.reset();
		settingsNav.selectCategory('vault');
		render(VaultCollection);
		expect(await screen.findByText('Available after approval')).toBeTruthy();
		expect(button('Add sign-in').disabled).toBe(true);
	});

	it('cannot add or save while the encryption key is locked', async () => {
		api.get.mockImplementation(async () => listing(false));
		settingsNav.reset();
		settingsNav.selectCategory('vault');
		settingsNav.go({ category: 'vault', item: machine().id });
		render(VaultCollection);
		expect((await screen.findByRole('alert')).textContent).toContain('The Vault is locked');
		expect(button('Add sign-in').disabled).toBe(true);
		await type(field('Name'), 'Renamed');
		expect(button('Save').disabled).toBe(true);
	});
});

describe('Settings > Vault adding', () => {
	it('adds a machine with only machine fields and forgets the typed password', async () => {
		await show();
		await fireEvent.click(button('Add sign-in'));
		await type(field('Name'), ' Dev box ');
		await type(field('Address'), 'dev.example.com');
		await type(field('Username'), 'deploy');
		await type(secretField('Password'), SECRET);
		entries = [...entries, machine({ id: 'c'.repeat(32), label: 'Dev box', target: 'deploy@dev.example.com:22', host: 'dev.example.com', username: 'deploy', fingerprint: '' })];
		await fireEvent.click(button('Add sign-in'));
		await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
		expect(api.post.mock.calls[0][0]).toBe('/vault/entries');
		// The request body is wiped once the request is over.
		await waitFor(() => expect(api.post.mock.calls[0][1].password).toBe(''));
		expect(Object.keys(api.post.mock.calls[0][1]).sort()).toEqual(['host', 'kind', 'label', 'password', 'port', 'username']);
		expect(api.post.mock.calls[0][1]).toMatchObject({ kind: 'machine', label: 'Dev box', host: 'dev.example.com', port: 22, username: 'deploy' });
		await waitFor(() => expect(settingsNav.selectedItem('vault')).toBe('c'.repeat(32)));
		expect(document.body.innerHTML).not.toContain(SECRET);
		expect(secretField('Password').value).toBe('');
	});

	it('shows the added sign-in in the draft\'s step, so a phone\'s Back returns to the list', async () => {
		await show();
		await fireEvent.click(button('Add sign-in'));
		const replace = vi.spyOn(settingsNav, 'replaceItem');
		await type(field('Name'), 'Dev box');
		await type(field('Address'), 'dev.example.com');
		await type(field('Username'), 'deploy');
		await type(secretField('Password'), SECRET);
		entries = [...entries, machine({ id: 'c'.repeat(32), label: 'Dev box' })];
		await fireEvent.click(button('Add sign-in'));
		await waitFor(() => expect(replace).toHaveBeenCalledWith('c'.repeat(32), 'vault'));
		replace.mockRestore();
	});

	it('sends the password that was typed', async () => {
		let sent = '';
		api.post.mockImplementation(async (_url: string, body: { password: string }) => {
			sent = body.password;
			return { id: 'c'.repeat(32) };
		});
		await show();
		await fireEvent.click(button('Add sign-in'));
		await type(field('Name'), 'Dev box');
		await type(field('Address'), 'dev.example.com');
		await type(field('Username'), 'deploy');
		await type(secretField('Password'), SECRET);
		await fireEvent.click(button('Add sign-in'));
		await waitFor(() => expect(sent).toBe(SECRET));
	});

	it('adds a web service with only service fields', async () => {
		await show();
		await fireEvent.click(button('Add sign-in'));
		await fireEvent.click(screen.getByRole('button', { name: 'Web service' }));
		await type(field('Name'), 'GitHub');
		await type(field('Address'), 'https://api.github.com');
		await type(secretField('API key or token'), SECRET);
		await fireEvent.click(button('Add sign-in'));
		await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
		expect(Object.keys(api.post.mock.calls[0][1]).sort()).toEqual(['auth_header', 'auth_scheme', 'kind', 'label', 'secret', 'url', 'writable']);
		expect(api.post.mock.calls[0][1]).toMatchObject({ kind: 'service', url: 'https://api.github.com', auth_header: 'Authorization', auth_scheme: 'Bearer', writable: false });
	});

	it('adds a web service that signs in with a username and password, without header settings', async () => {
		let sent = '';
		api.post.mockImplementation(async (_url: string, body: { secret: string }) => {
			sent = body.secret;
			return { id: 'c'.repeat(32) };
		});
		await show();
		await fireEvent.click(button('Add sign-in'));
		await fireEvent.click(screen.getByRole('button', { name: 'Web service' }));
		await type(field('Name'), 'NAS');
		await type(field('Address'), 'https://nas.example/api');
		// A token typed first never becomes the password.
		await type(secretField('API key or token'), 'typed-as-a-token');
		await fireEvent.click(screen.getByRole('button', { name: 'Username and password' }));
		expect(screen.queryByRole('textbox', { name: 'Header' })).toBeNull();
		expect(screen.queryByRole('textbox', { name: 'Scheme' })).toBeNull();
		expect(secretField('Password').value).toBe('');
		await type(secretField('Password'), SECRET);
		await fireEvent.click(button('Add sign-in'));
		expect((await screen.findByRole('alert')).textContent).toContain('Enter the username');
		expect(api.post).not.toHaveBeenCalled();
		await type(field('Username'), 'ad:min');
		await fireEvent.click(button('Add sign-in'));
		await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('cannot contain a colon'));
		expect(api.post).not.toHaveBeenCalled();
		await type(field('Username'), ' admin ');
		await fireEvent.click(button('Add sign-in'));
		await waitFor(() => expect(sent).toBe(SECRET));
		expect(Object.keys(api.post.mock.calls[0][1]).sort()).toEqual(['auth_header', 'auth_scheme', 'kind', 'label', 'secret', 'url', 'username', 'writable']);
		expect(api.post.mock.calls[0][1]).toMatchObject({ kind: 'service', url: 'https://nas.example/api', auth_header: 'Authorization', auth_scheme: 'Basic', username: 'admin' });
		await waitFor(() => expect(api.post.mock.calls[0][1].secret).toBe(''));
		expect(document.body.innerHTML).not.toContain(SECRET);
	});

	it('drops a typed secret when the kind changes', async () => {
		await show();
		await fireEvent.click(button('Add sign-in'));
		await type(field('Name'), 'Kept name');
		await type(secretField('Password'), SECRET);
		await fireEvent.click(screen.getByRole('button', { name: 'Web service' }));
		expect(field('Name').value).toBe('Kept name');
		await fireEvent.click(screen.getByRole('button', { name: 'SSH machine' }));
		expect(secretField('Password').value).toBe('');
	});

	it('names what is missing before anything is sent, and keeps the worker reason without the secret', async () => {
		await show();
		await fireEvent.click(button('Add sign-in'));
		await fireEvent.click(button('Add sign-in'));
		expect((await screen.findByRole('alert')).textContent).toContain('Give the sign-in a name');
		expect(api.post).not.toHaveBeenCalled();

		api.post.mockRejectedValueOnce({ status: 400, detail: 'Enter a machine IP or hostname, not a URL or credentials.' });
		await type(field('Name'), 'Dev box');
		await type(field('Address'), 'dev.example.com');
		await type(field('Username'), 'deploy');
		await type(secretField('Password'), SECRET);
		await fireEvent.click(button('Add sign-in'));
		await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('Enter a machine IP or hostname'));
		// A failed save also forgets the secret; the rest of the form stays.
		expect(secretField('Password').value).toBe('');
		expect(field('Address').value).toBe('dev.example.com');
	});
});

describe('Settings > Vault editing', () => {
	it('sends only the changed field and keeps the stored secret', async () => {
		await show(machine().id);
		const save = () => button('Save');
		expect(save().disabled).toBe(true);
		await type(field('Name'), 'NAS');
		expect(api.patch).not.toHaveBeenCalled();
		await waitFor(() => expect(save().disabled).toBe(false));
		await fireEvent.click(save());
		await waitFor(() => expect(api.patch).toHaveBeenCalledWith(`/vault/entries/${machine().id}`, { label: 'NAS' }));
	});

	it('warns that more than a rename ends access, and says what the save ended', async () => {
		api.patch.mockResolvedValue({ updated: true, revoked: 2, host_key_cleared: true });
		await show(machine().id);
		await type(field('Name'), 'NAS');
		expect(screen.queryByText('Saving this ends access', { exact: false })).toBeNull();
		await type(field('Address'), '10.0.0.9');
		expect(screen.getByText('Saving this ends access', { exact: false })).toBeTruthy();
		entries = [machine({ label: 'NAS', host: '10.0.0.9', target: 'root@10.0.0.9:22', fingerprint: '' }), service()];
		await fireEvent.click(button('Save'));
		await waitFor(() => expect(api.patch).toHaveBeenCalledWith(`/vault/entries/${machine().id}`, { label: 'NAS', host: '10.0.0.9' }));
		expect((await screen.findByRole('status')).textContent).toContain('Ended 2 accesses that used this sign-in');
		expect(screen.getByRole('status').textContent).toContain('pins the new address');
	});

	it('replaces the password only when one is typed and forgets it afterwards', async () => {
		let sent = '';
		api.patch.mockImplementation(async (_url: string, body: { password: string }) => {
			sent = body.password;
			return { updated: true, revoked: 0, host_key_cleared: false };
		});
		await show(machine().id);
		await type(secretField('Password'), SECRET);
		await fireEvent.click(button('Save'));
		await waitFor(() => expect(sent).toBe(SECRET));
		expect(Object.keys(api.patch.mock.calls[0][1])).toEqual(['password']);
		await waitFor(() => expect(api.patch.mock.calls[0][1].password).toBe(''));
		expect(secretField('Password').value).toBe('');
		expect(document.body.innerHTML).not.toContain(SECRET);
	});

	it('needs the key to switch a password sign-in to a private key', async () => {
		await show(machine().id);
		await fireEvent.click(screen.getByRole('button', { name: 'Private key' }));
		await fireEvent.click(button('Save'));
		expect((await screen.findByRole('alert')).textContent).toContain('Paste the private key');
		expect(api.patch).not.toHaveBeenCalled();
	});

	it('edits a web service and discards back to the saved values', async () => {
		await show(service().id);
		await fireEvent.click(screen.getByRole('switch', { name: 'Allow changes' }));
		await type(field('Scheme'), 'Token');
		await fireEvent.click(button('Discard'));
		expect(field('Scheme').value).toBe('Bearer');
		await fireEvent.click(screen.getByRole('switch', { name: 'Allow changes' }));
		await fireEvent.click(button('Save'));
		await waitFor(() => expect(api.patch).toHaveBeenCalledWith(`/vault/entries/${service().id}`, { writable: true }));
	});

	it('changes the account of a username and password sign-in and keeps the stored password', async () => {
		entries = [machine(), service({ auth_scheme: 'Basic', username: 'admin' })];
		await show(service().id);
		// Named in the summary facts as well as on the choice itself.
		expect(screen.getAllByText('Username and password').length).toBeGreaterThan(1);
		expect(screen.queryByRole('textbox', { name: 'Header' })).toBeNull();
		expect(field('Username').value).toBe('admin');
		expect(secretField('Password').getAttribute('placeholder')).toBe('Unchanged');
		await type(field('Username'), 'operator');
		await fireEvent.click(button('Save'));
		await waitFor(() => expect(api.patch).toHaveBeenCalledWith(`/vault/entries/${service().id}`, { username: 'operator' }));
	});

	it('needs the new credential to change how a web service signs in, in both directions', async () => {
		await show(service().id);
		await fireEvent.click(screen.getByRole('button', { name: 'Username and password' }));
		await type(field('Username'), 'admin');
		// The stored token is never reused as the password.
		expect(secretField('Password').getAttribute('placeholder')).toBe('');
		await fireEvent.click(button('Save'));
		expect((await screen.findByRole('alert')).textContent).toContain('Enter the password');
		expect(api.patch).not.toHaveBeenCalled();
		// Back to the token: nothing changed, nothing to save.
		await fireEvent.click(screen.getByRole('button', { name: 'API token' }));
		expect(field('Scheme').value).toBe('Bearer');
		await waitFor(() => expect(button('Save').disabled).toBe(true));
		await fireEvent.click(screen.getByRole('button', { name: 'Username and password' }));
		expect(field('Username').value).toBe('');
		await type(field('Username'), 'admin');
		await type(secretField('Password'), SECRET);
		let sent: Record<string, unknown> = {};
		api.patch.mockImplementation(async (_url: string, body: Record<string, unknown>) => {
			sent = { ...body };
			return { updated: true, revoked: 1, host_key_cleared: false };
		});
		await fireEvent.click(button('Save'));
		await waitFor(() => expect(sent).toEqual({ auth_header: 'Authorization', auth_scheme: 'Basic', username: 'admin', secret: SECRET }));
	});

	it('removes a sign-in only after confirming', async () => {
		await show(machine().id);
		await fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
		expect(api.delete).not.toHaveBeenCalled();
		expect(await screen.findByText('Remove HomeLab?')).toBeTruthy();
		entries = [service()];
		await fireEvent.click(button('Remove'));
		await waitFor(() => expect(api.delete).toHaveBeenCalledWith(`/vault/entries/${machine().id}`));
		await waitFor(() => expect(settingsNav.selectedItem('vault')).toBeFalsy());
	});
});

describe('vault-model', () => {
	it('builds a machine form from an older entry that lists only its target', () => {
		const form = formFromEntry(machine({ host: undefined, port: undefined, username: undefined, target: 'deploy@nas.lan:2222' }));
		expect(form).toMatchObject({ host: 'nas.lan', port: 2222, username: 'deploy', password: '', privateKey: '' });
	});

	it('sends a key sign-in without a password, and a passphrase alone for a stored key', () => {
		const draft = { ...emptyForm(), label: 'K', host: 'h', username: 'u', useKey: true, privateKey: 'KEY', passphrase: 'pp', password: 'ignored' };
		expect(createBody(draft)).toEqual({ kind: 'machine', label: 'K', host: 'h', port: 22, username: 'u', private_key: 'KEY', passphrase: 'pp' });
		const initial = formFromEntry(machine({ uses_key: true }));
		expect(editBody({ ...initial, passphrase: 'new' }, initial)).toEqual({ passphrase: 'new' });
		expect(editBody({ ...initial }, initial)).toEqual({});
	});

	it('never reuses a stored service secret as the other kind of sign-in', () => {
		const token = formFromEntry(service({ auth_header: 'X-Api-Key', auth_scheme: '' }));
		expect(token).toMatchObject({ basic: false, username: '', authHeader: 'X-Api-Key', authScheme: '' });
		const account = formFromEntry(service({ auth_scheme: 'Basic', username: 'admin' }));
		expect(account).toMatchObject({ basic: true, username: 'admin' });
		expect(problems(account, account)).toEqual([]);
		// To a password: all of it is said, and the password is required.
		const toBasic = { ...token, basic: true, username: 'admin', authHeader: 'Authorization', authScheme: 'Basic' };
		expect(problems(toBasic, token)).toEqual(['Enter the password']);
		expect(editBody({ ...toBasic, secret: 'pw-pw-pw-pw' }, token)).toEqual({
			auth_header: 'Authorization', auth_scheme: 'Basic', username: 'admin', secret: 'pw-pw-pw-pw' });
		// To a token: the username is cleared explicitly, and the token is required.
		const toToken = { ...account, basic: false, username: '', authHeader: 'Authorization', authScheme: 'Bearer' };
		expect(problems(toToken, account)).toEqual(['Enter the secret the service expects']);
		expect(editBody({ ...toToken, secret: 'tok-tok-tok' }, account)).toEqual({
			auth_header: 'Authorization', auth_scheme: 'Bearer', username: '', secret: 'tok-tok-tok' });
		// A new one carries the username only when it signs in with a password.
		expect('username' in createBody({ ...token, secret: 'tok-tok-tok' })).toBe(false);
		expect(createBody({ ...account, secret: 'pw-pw-pw-pw' })).toMatchObject({ auth_scheme: 'Basic', username: 'admin', secret: 'pw-pw-pw-pw' });
	});

	it('accepts a blank secret only where one is already stored', () => {
		const initial = formFromEntry(machine());
		expect(problems(initial, initial)).toEqual([]);
		expect(problems({ ...initial, useKey: true }, initial)).toEqual(['Paste the private key']);
		expect(problems(formFromEntry(machine()))).toEqual(['Enter the password']);
		const web = formFromEntry(service());
		expect(problems(web, web)).toEqual([]);
		expect(problems({ ...web, url: 'http://plain.example' }, web)).toEqual(['Enter an address that starts with https://']);
		expect(problems(web)).toEqual(['Enter the secret the service expects']);
	});

	it('keeps one draft, follows clean forms and flags dirty forms changed elsewhere', async () => {
		const model = new VaultModel();
		await model.load();
		expect(model.addDraft()).toBe(DRAFT_ID);
		model.draft!.label = 'kept';
		model.addDraft();
		expect(model.draft!.label).toBe('kept');

		model.ensure(machine().id)!.label = 'edited';
		model.ensure(service().id);
		entries = [machine({ label: 'Renamed elsewhere' }), service({ label: 'CF' })];
		await model.load();
		expect(model.changedElsewhere[machine().id]).toBe(true);
		expect(model.form(machine().id)!.label).toBe('edited');
		expect(model.form(service().id)!.label).toBe('CF');
	});

	it('drops a form and its typed secret when the sign-in is removed elsewhere or the Vault is blocked', async () => {
		const model = new VaultModel();
		await model.load();
		model.ensure(machine().id)!.password = SECRET;
		entries = [service()];
		await model.load();
		expect(model.form(machine().id)).toBeUndefined();

		model.ensure(service().id)!.secret = SECRET;
		model.addDraft();
		model.draft!.password = SECRET;
		status.preview_access = true;
		await model.load();
		expect(model.form(service().id)).toBeUndefined();
		expect(model.draft).toBeNull();
		expect(model.entries).toEqual([]);
	});

	it('refuses to send a secret from a page that is not secure', async () => {
		const model = new VaultModel();
		await model.load();
		const form = model.ensure(machine().id)!;
		form.password = SECRET;
		secure(false);
		expect(await model.save(machine().id)).toBeNull();
		expect(api.patch).not.toHaveBeenCalled();
		expect(form.password).toBe('');
		expect(model.errors[machine().id][0]).toContain('not available on this connection');
	});
});
