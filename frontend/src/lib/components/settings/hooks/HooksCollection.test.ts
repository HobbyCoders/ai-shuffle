/**
 * Settings > Hooks against a mocked hooks API: app and plugin groups,
 * search, the add flow and its payload, explicit Save, the Enabled switch
 * saving at once, read-only plugin hooks, delete confirmation, and the
 * model's payload and "changed elsewhere" rules.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import type { Hook } from '$lib/api/hooks';

const api = vi.hoisted(() => ({
	getAppHooks: vi.fn(),
	getProjectHooks: vi.fn(),
	getPluginHooks: vi.fn(),
	getProjectPluginHooks: vi.fn(),
	createHook: vi.fn(),
	createAppHook: vi.fn(),
	updateHook: vi.fn(),
	deleteHook: vi.fn(),
	testHook: vi.fn()
}));
vi.mock('$lib/api/client', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/client')>()),
	...api
}));
vi.mock('$lib/services/watch-resources', () => ({ watchResources: vi.fn() }));

import { hooks } from '$lib/stores/hooks.svelte';
import { watchResources } from '$lib/services/watch-resources';
import { settingsNav } from '$lib/settings/navigation.svelte';
import HooksCollection from './HooksCollection.svelte';
import { DRAFT_ID, HooksModel, createPayload, emptyForm, formFromHook, updatePayload } from './hooks-model.svelte';

const hook = (over: Partial<Hook> = {}): Hook =>
	({
		id: 'h1',
		name: 'Guard rm',
		event: 'PreToolUse',
		hook_type: 'bash',
		command: 'guard.sh',
		timeout: 30000,
		tool_matcher: 'Bash',
		enabled: true,
		project_id: null,
		failure_count: 0,
		...over
	}) as Hook;

const plugin = { plugin_id: 'lint@local', plugin_name: 'Linter', event: 'PostToolUse', tool_matcher: 'Edit', description: 'Lints edits' };

async function show(item?: string) {
	settingsNav.reset();
	settingsNav.selectCategory('hooks');
	if (item) settingsNav.go({ category: 'hooks', item });
	const page = render(HooksCollection);
	await screen.findAllByText('Guard rm');
	return page;
}

const field = (placeholder: string) => screen.getByPlaceholderText(placeholder) as HTMLInputElement;
async function type(input: HTMLInputElement | HTMLTextAreaElement, value: string) {
	await fireEvent.input(input, { target: { value } });
	await fireEvent.change(input, { target: { value } });
}

beforeEach(() => {
	vi.clearAllMocks();
	hooks.reset();
	api.getAppHooks.mockResolvedValue([hook(), hook({ id: 'h2', name: 'Notify', event: 'Stop', hook_type: 'webhook', command: null, url: 'https://ntfy.sh/x', tool_matcher: null, enabled: false })]);
	api.getPluginHooks.mockResolvedValue([plugin]);
	api.getProjectHooks.mockResolvedValue([]);
	api.getProjectPluginHooks.mockResolvedValue([]);
	api.createAppHook.mockImplementation(async (body) => hook({ ...body, id: 'h9', project_id: null }));
	api.updateHook.mockImplementation(async (id, body) => hook({ id, ...body }));
	api.deleteHook.mockResolvedValue(undefined);
});
afterEach(() => cleanup());

describe('Settings > Hooks list', () => {
	it('groups app and plugin hooks and filters by search', async () => {
		await show();
		expect(screen.getByText('PreToolUse, Bash')).toBeTruthy();
		expect(screen.getByText('Off, Stop')).toBeTruthy();
		expect(screen.getByText('Linter')).toBeTruthy();

		await fireEvent.input(screen.getByRole('searchbox'), { target: { value: 'notify' } });
		expect(screen.queryByText('PreToolUse, Bash')).toBeNull();
		expect(screen.getByText('Off, Stop')).toBeTruthy();
		await fireEvent.input(screen.getByRole('searchbox'), { target: { value: 'zzz' } });
		expect(screen.getByText('Nothing matches.')).toBeTruthy();
	});
});

describe('Settings > Hooks editing', () => {
	it('explains and blocks recreating a deleted signed webhook without a replacement', async () => {
		api.getAppHooks.mockResolvedValue([hook({ hook_type: 'webhook', url: 'https://example.test', has_secret: true })]);
		await show('h1');
		await type(field('Block rm -rf'), 'Renamed');
		api.getAppHooks.mockResolvedValue([]);
		await vi.mocked(watchResources).mock.calls.at(-1)![1]();
		expect(await screen.findByText('Deleted elsewhere. Enter a signing secret before saving it again.')).toBeTruthy();
		expect(field('Required to recreate').value).toBe('');
		await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
		expect((await screen.findByRole('alert')).textContent).toContain('Enter a signing secret to recreate this deleted webhook');
		expect(api.createAppHook).not.toHaveBeenCalled();
		expect(api.updateHook).not.toHaveBeenCalled();
	});

	it('shows saved-secret presence without hydrating it on an unrelated save', async () => {
		api.getAppHooks.mockResolvedValue([hook({ hook_type: 'webhook', url: 'https://example.test', has_secret: true })]);
		await show('h1');
		expect(field('Leave blank to keep').value).toBe('');
		expect(screen.getByText('Secret saved')).toBeTruthy();
		await type(field('Block rm -rf'), 'Renamed');
		await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
		await waitFor(() => expect(api.updateHook).toHaveBeenCalledWith('h1', expect.objectContaining({ name: 'Renamed', secret: undefined })));
	});

	it('adds an app hook with the typed command', async () => {
		await show();
		await fireEvent.click(screen.getByRole('button', { name: 'Add hook' }));
		await type(field('Block rm -rf'), 'Ping');
		await type(screen.getByLabelText('Command') as HTMLTextAreaElement, 'echo hi');
		await fireEvent.click(screen.getAllByRole('button', { name: 'Add hook' }).at(-1)!);
		await waitFor(() =>
			expect(api.createAppHook).toHaveBeenCalledWith(
				expect.objectContaining({ name: 'Ping', event: 'Stop', hook_type: 'bash', command: 'echo hi', timeout: 30000 })
			)
		);
		await waitFor(() => expect(settingsNav.selectedItem('hooks')).toBe('h9'));
	});

	it('saves an edit only when Save is pressed', async () => {
		await show('h1');
		const save = () => screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement;
		expect(save().disabled).toBe(true);
		await type(field('Bash or Write|Edit'), 'Write|Edit');
		expect(api.updateHook).not.toHaveBeenCalled();
		await waitFor(() => expect(save().disabled).toBe(false));
		await fireEvent.click(save());
		await waitFor(() => expect(api.updateHook).toHaveBeenCalledWith('h1', expect.objectContaining({ tool_matcher: 'Write|Edit' })));
	});

	it('saves the Enabled switch at once', async () => {
		await show('h1');
		await fireEvent.click(screen.getByRole('switch'));
		await waitFor(() => expect(api.updateHook).toHaveBeenCalledWith('h1', { enabled: false }));
	});

	it('shows plugin hooks read-only', async () => {
		await show('plugin:lint@local:PostToolUse');
		expect(screen.getAllByText('Linter').length).toBeGreaterThan(0);
		expect(screen.queryByRole('button', { name: 'Save' })).toBeNull();
	});

	it('deletes a hook only after confirming', async () => {
		await show('h1');
		await fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
		expect(api.deleteHook).not.toHaveBeenCalled();
		expect(await screen.findByText('Delete Guard rm?')).toBeTruthy();
		await fireEvent.click(screen.getAllByRole('button', { name: 'Delete' }).at(-1)!);
		await waitFor(() => expect(api.deleteHook).toHaveBeenCalledWith('h1'));
	});
});

describe('hooks-model', () => {
	it('keeps secret input empty and sends only an intentional replacement', () => {
		const form = formFromHook(hook({ hook_type: 'webhook', has_secret: true }));
		expect(form.secret).toBe('');
		expect(updatePayload(form).secret).toBeUndefined();
		form.secret = '  ';
		expect(updatePayload(form).secret).toBeUndefined();
		form.secret = ' dummy-replacement ';
		expect(updatePayload(form).secret).toBe('dummy-replacement');
	});

	it('builds create and update payloads for the chosen action type', () => {
		const web = { ...emptyForm(null), name: ' Ping ', type: 'webhook' as const, url: ' https://x.test ', command: 'ignored', timeoutSec: 999 };
		expect(createPayload(web)).toMatchObject({ name: 'Ping', command: undefined, url: 'https://x.test', timeout: 300000, tool_matcher: undefined });
		// A cleared matcher is sent as '' so the PATCH actually clears it.
		const tool = { ...formFromHook(hook()), matcher: '' };
		expect(updatePayload(tool)).toMatchObject({ tool_matcher: '', command: 'guard.sh', url: undefined, secret: undefined });
		expect(updatePayload({ ...tool, event: 'Stop' }).tool_matcher).toBe('');
	});

	it.each([null, 'P1'])('requires a replacement before recreating a deleted signed webhook in scope %s', async (projectId) => {
		const signed = hook({ hook_type: 'webhook', url: 'https://example.test', has_secret: true, project_id: projectId });
		api.getAppHooks.mockResolvedValue([signed]);
		api.getProjectHooks.mockResolvedValue([signed]);
		api.createHook.mockImplementation(async (_pid, body) => hook({ ...body, id: 'h9', project_id: projectId }));
		const model = new HooksModel();
		model.projectId = projectId;
		await model.load();
		model.ensure('h1')!.name = 'Renamed';
		api.getAppHooks.mockResolvedValue([]);
		api.getProjectHooks.mockResolvedValue([]);
		await model.load();
		expect(model.remote.h1).toBe('deleted');
		model.form('h1')!.secret = '  ';
		expect(await model.save('h1')).toBeNull();
		expect(api.createAppHook).not.toHaveBeenCalled();
		expect(api.createHook).not.toHaveBeenCalled();
		expect(model.errors.h1).toEqual(['Enter a signing secret to recreate this deleted webhook']);
		model.form('h1')!.secret = ' dummy-replacement ';
		expect(await model.save('h1')).toBe('h9');
		const created = projectId ? api.createHook.mock.calls[0][1] : api.createAppHook.mock.calls[0][0];
		expect(created).toMatchObject({ name: 'Renamed', secret: 'dummy-replacement', enabled: true });
	});

	it.each(['bash', 'webhook'] as const)('still recreates an unsigned %s hook without a replacement', async (hook_type) => {
		api.getAppHooks.mockResolvedValue([hook({ hook_type, has_secret: false, url: 'https://example.test' })]);
		const model = new HooksModel();
		await model.load();
		model.ensure('h1')!.name = 'Renamed';
		api.getAppHooks.mockResolvedValue([]);
		await model.load();
		expect(await model.save('h1')).toBe('h9');
		expect(api.createAppHook).toHaveBeenCalledWith(expect.objectContaining({ name: 'Renamed', secret: undefined }));
	});

	it('retains a signing requirement observed while edits are dirty', async () => {
		api.getAppHooks.mockResolvedValue([hook({ hook_type: 'webhook', url: 'https://example.test', has_secret: false })]);
		const model = new HooksModel();
		await model.load();
		model.ensure('h1')!.name = 'Renamed';
		api.getAppHooks.mockResolvedValue([hook({ hook_type: 'webhook', url: 'https://example.test', has_secret: true })]);
		await model.load();
		api.getAppHooks.mockResolvedValue([]);
		await model.load();
		expect(await model.save('h1')).toBeNull();
		expect(api.createAppHook).not.toHaveBeenCalled();
	});

	it('retains signing presence from an Enabled response before a deletion refresh', async () => {
		api.getAppHooks.mockResolvedValue([hook({ hook_type: 'webhook', url: 'https://example.test', has_secret: false })]);
		const model = new HooksModel();
		await model.load();
		model.ensure('h1')!.name = 'Renamed';
		// Another client adds a secret; the Enabled PATCH is the only response
		// that observes it before deletion and the next coalesced list refresh.
		api.updateHook.mockResolvedValue(hook({ hook_type: 'webhook', url: 'https://example.test', has_secret: true, enabled: false }));
		expect(await model.setEnabled('h1', false)).toBe(true);
		api.getAppHooks.mockResolvedValue([]);
		await model.load();
		expect(await model.save('h1')).toBeNull();
		expect(model.form('h1')!.name).toBe('Renamed');
		expect(model.form('h1')!.enabled).toBe(false);
		expect(api.createAppHook).not.toHaveBeenCalled();
	});

	it('flags dirty forms changed or deleted elsewhere and keeps clean ones in step', async () => {
		const model = new HooksModel();
		await model.load();
		model.ensure('h1')!.command = 'edited.sh';
		model.ensure('h2');
		expect(model.dirty('h1')).toBe(true);
		// Enabled saves on its own, so it never makes a form dirty.
		model.form('h2')!.enabled = true;
		expect(model.dirty('h2')).toBe(false);

		api.getAppHooks.mockResolvedValue([hook({ command: 'other.sh' })]);
		await model.load();
		expect(model.remote.h1).toBe('changed');
		expect(model.form('h1')!.command).toBe('edited.sh');
		expect(model.form('h2')).toBeUndefined();

		api.getAppHooks.mockResolvedValue([]);
		await model.load();
		expect(model.remote.h1).toBe('deleted');
	});

	it('refuses to save a draft without a name or command', async () => {
		const model = new HooksModel();
		await model.load();
		model.addDraft();
		expect(await model.save(DRAFT_ID)).toBeNull();
		expect(model.errors[DRAFT_ID]).toEqual(['Name is required', 'Command is required']);
		expect(api.createAppHook).not.toHaveBeenCalled();
	});
});
