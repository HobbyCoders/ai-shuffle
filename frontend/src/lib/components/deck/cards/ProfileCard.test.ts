/**
 * The Agents page (#1825) end to end against faked API replies: the list and
 * its filters, the detail tabs, autosave, create, duplicate and delete, and
 * the Models tab with and without the subagent default.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({
	get: vi.fn(),
	put: vi.fn(),
	post: vi.fn(),
	del: vi.fn(),
	clearFeatureItemId: vi.fn(),
	openFeaturePage: vi.fn()
}));
vi.mock('$lib/api/client', async (original) => ({
	...(await original<typeof import('$lib/api/client')>()),
	api: { get: mocks.get, put: mocks.put, post: mocks.post, delete: mocks.del }
}));
vi.mock('$lib/services/watch-resources', () => ({ watchResources: () => {} }));
vi.mock('$lib/stores/auth.svelte', async (original) => ({
	...(await original<typeof import('$lib/stores/auth.svelte')>()),
	isAdmin: (await import('svelte/store')).writable(true)
}));
vi.mock('$lib/stores/layout.svelte', async (original) => ({
	...(await original<typeof import('$lib/stores/layout.svelte')>()),
	layout: { clearFeatureItemId: mocks.clearFeatureItemId, openFeaturePage: mocks.openFeaturePage }
}));
vi.mock('$lib/api/toolSurface', async (original) => ({
	...(await original<typeof import('$lib/api/toolSurface')>()),
	fetchToolSurface: async () => ({ groups: [], errors: [], discovered_at: '' })
}));

import ProfileCard from './ProfileCard.svelte';
import { appData } from '$lib/stores/app-data.svelte';
import type { Profile } from '$lib/api/client';

let agents: Profile[];
/** What GET /settings/subagent-default answers; null = 404. */
let subagentDefault: unknown;

const researcher = (): Profile => ({
	id: 'agent',
	name: 'Researcher',
	description: 'Original description',
	is_builtin: false,
	is_modified: false,
	session_count: 7,
	created_at: '',
	updated_at: '',
	config: {
		system_prompt: { type: 'custom', content: 'Keep my original prompt' },
		model: 'original-model',
		permission_mode: 'default',
		custom_future_key: { keep: true },
		tool_selection_mode: 'all'
	} as never
});
const designer = (): Profile => ({
	id: 'frontend-designer',
	name: 'Frontend Designer',
	description: 'Read-only design',
	is_builtin: true,
	is_modified: true,
	created_at: '',
	updated_at: '',
	config: { system_prompt: { type: 'custom', content: 'Design only.' }, tool_selection_mode: 'allow', allowed_tools: ['Read'] }
});

beforeEach(async () => {
	vi.clearAllMocks();
	try {
		sessionStorage.clear();
	} catch {
		/* no storage */
	}
	subagentDefault = null;
	agents = [researcher(), designer()];
	mocks.get.mockImplementation(async (path: string) => {
		if (path === '/agents') return agents;
		if (path === '/settings/subagent-default') {
			if (subagentDefault === null) throw { status: 404, detail: 'Not Found' };
			return subagentDefault;
		}
		return [];
	});
	mocks.put.mockImplementation(async (path: string, payload: any) => {
		if (path === '/settings/subagent-default') return payload;
		const id = path.split('/').pop();
		agents = agents.map((a) => (a.id === id ? { ...a, ...payload } : a));
		return payload;
	});
	mocks.post.mockImplementation(async (path: string, payload: any) => {
		if (path === '/agents') {
			agents = [...agents, { is_builtin: false, is_modified: false, created_at: '', updated_at: '', ...payload }];
		}
		return payload;
	});
	mocks.del.mockImplementation(async (path: string) => {
		agents = agents.filter((a) => `/agents/${a.id}` !== path);
		return {};
	});
	await appData.loadProfiles();
});
afterEach(() => {
	cleanup();
	vi.useRealTimers();
});

const heading = (name: string) => screen.findByRole('heading', { level: 2, name });
const row = (name: RegExp) => screen.getByRole('button', { name });
const list = () => within(screen.getByRole('region', { name: 'Agent list' }));
const tab = (name: string) => screen.getByRole('tab', { name: new RegExp(`^${name}`) });

describe('list', () => {
	it('opens the first agent beside the list and splits Yours from Built-in', async () => {
		render(ProfileCard);
		await heading('Researcher');
		expect(list().getByText('Yours')).toBeTruthy();
		expect(list().getByText('Built-in')).toBeTruthy();
		expect(row(/^Researcher/).getAttribute('aria-current')).toBe('true');
		// A modified built-in carries the chip on its row.
		expect(within(row(/^Frontend Designer/)).getByText('Modified')).toBeTruthy();
	});

	it('searches and clears', async () => {
		render(ProfileCard);
		await heading('Researcher');
		await fireEvent.input(screen.getByPlaceholderText('Search agents'), { target: { value: 'read-only' } });
		expect(row(/^Frontend Designer/)).toBeTruthy();
		await fireEvent.input(screen.getByPlaceholderText('Search agents'), { target: { value: 'zzz' } });
		expect(screen.getByText(/No agents match/)).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
		expect((screen.getByPlaceholderText('Search agents') as HTMLInputElement).value).toBe('');
	});

	it('filters to built-ins', async () => {
		render(ProfileCard);
		await heading('Researcher');
		await fireEvent.click(screen.getByRole('button', { name: 'Built-in', pressed: false }));
		// The open agent stays listed; the "Yours" section is gone.
		expect(list().queryByText('Yours')).toBeNull();
		expect(row(/^Frontend Designer/)).toBeTruthy();
	});

	it('opens another agent from its row', async () => {
		render(ProfileCard);
		await heading('Researcher');
		await fireEvent.click(row(/^Frontend Designer/));
		await heading('Frontend Designer');
		expect(screen.getAllByText('Built-in').length).toBeGreaterThan(1);
	});
});

describe('detail', () => {
	it('puts counts on the tabs and chats in the metadata, with no facts strip', async () => {
		const { container } = render(ProfileCard);
		await heading('Researcher');
		expect(container.querySelector('.st-facts')).toBeNull();
		expect(screen.getByText(/· 7 chats$/)).toBeTruthy();
		expect(tab('Subagents').querySelector('.n')).toBeTruthy();
		await fireEvent.click(tab('Tools'));
		expect(tab('Tools').getAttribute('aria-selected')).toBe('true');
		expect(screen.getByRole('radiogroup', { name: 'Tool access mode' })).toBeTruthy();
	});

	it('switches tabs from the strip', async () => {
		render(ProfileCard);
		await heading('Researcher');
		await fireEvent.click(tab('Instructions'));
		expect((screen.getByLabelText('System prompt') as HTMLTextAreaElement).value).toBe('Keep my original prompt');
		await fireEvent.click(tab('Advanced'));
		expect(screen.getByRole('switch', { name: 'Stream replies' })).toBeTruthy();
		await fireEvent.keyDown(tab('Advanced'), { key: 'Home' });
		expect(tab('Profile').getAttribute('aria-selected')).toBe('true');
	});

	it('lists the agent\'s saved approvals on the Approvals tab', async () => {
		const get = mocks.get.getMockImplementation()!;
		mocks.get.mockImplementation(async (path: string) =>
			path === '/agents/agent/permission-rules'
				? [{ id: 'rule-1', profile_id: 'agent', tool_name: 'Bash', tool_pattern: null, decision: 'deny', created_at: '2026-10-20T10:00:00Z' }]
				: get(path)
		);
		render(ProfileCard);
		await heading('Researcher');
		await fireEvent.click(tab('Approvals'));
		const saved = await screen.findByRole('list', { name: 'Saved approvals' });
		expect(within(saved).getByText('Bash')).toBeTruthy();
		expect(within(saved).getByText('Every call')).toBeTruthy();
		expect(within(saved).getByText('Deny')).toBeTruthy();
	});

	it('hides the Approvals tab from anyone but the owner', async () => {
		const { isAdmin } = await import('$lib/stores/auth.svelte');
		(isAdmin as unknown as { set: (v: boolean) => void }).set(false);
		try {
			render(ProfileCard);
			await heading('Researcher');
			expect(screen.queryByRole('tab', { name: /^Approvals/ })).toBeNull();
		} finally {
			(isAdmin as unknown as { set: (v: boolean) => void }).set(true);
		}
	});

	it('shows Duplicate, Export and Delete for your agent, Restore for a modified built-in, and no menu', async () => {
		render(ProfileCard);
		await heading('Researcher');
		expect(screen.getByRole('button', { name: 'Duplicate' })).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Export .json' })).toBeTruthy();
		// Delete is one row at the bottom of the profile, never in the header.
		expect(screen.getAllByRole('button', { name: 'Delete' })).toHaveLength(1);
		expect(screen.queryByRole('button', { name: 'Delete agent' })).toBeNull();
		expect(screen.queryByRole('button', { name: /More/ })).toBeNull();
		await fireEvent.click(row(/^Frontend Designer/));
		await heading('Frontend Designer');
		expect(screen.getByRole('button', { name: 'Restore defaults' })).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'Delete agent' })).toBeNull();
	});
});

describe('subagents roster', () => {
	const subagentCount = () => tab('Subagents').querySelector('.n')?.textContent;

	it('saves None with an empty selection and counts 0 on the tab', async () => {
		agents = agents.map((a) =>
			a.id === 'agent' ? { ...a, config: { ...a.config, agent_invoke_mode: 'custom', enabled_agents: ['frontend-designer'] } as never } : a
		);
		await appData.loadProfiles();
		render(ProfileCard, { initialSelectedId: 'agent' });
		await heading('Researcher');
		expect(subagentCount()).toBe('1');
		await fireEvent.click(tab('Subagents'));
		await fireEvent.click(screen.getByRole('button', { name: 'None' }));
		expect(subagentCount()).toBe('0');
		await waitFor(() => expect(mocks.put).toHaveBeenCalled(), { timeout: 2000 });
		const [path, payload] = mocks.put.mock.calls.at(-1)!;
		expect(path).toBe('/agents/agent');
		expect(payload.config.agent_invoke_mode).toBe('none');
		expect(payload.config.enabled_agents).toEqual([]);
		// Back to Selected only: the old pick is gone, nothing is selected.
		await fireEvent.click(screen.getByRole('button', { name: 'Selected only' }));
		expect(subagentCount()).toBe('0');
		expect(screen.getByRole('switch', { name: 'Allow Frontend Designer' }).getAttribute('aria-checked')).toBe('false');
		await waitFor(() => expect(mocks.put.mock.calls.at(-1)![1].config.agent_invoke_mode).toBe('custom'), { timeout: 2000 });
		expect(mocks.put.mock.calls.at(-1)![1].config.enabled_agents).toEqual([]);
	});

	it('reopens an agent saved as None as None', async () => {
		agents = agents.map((a) =>
			a.id === 'agent' ? { ...a, config: { ...a.config, agent_invoke_mode: 'none', enabled_agents: [] } as never } : a
		);
		await appData.loadProfiles();
		render(ProfileCard, { initialSelectedId: 'agent' });
		await heading('Researcher');
		expect(subagentCount()).toBe('0');
		await fireEvent.click(tab('Subagents'));
		expect(screen.getByRole('button', { name: 'None' }).getAttribute('aria-pressed')).toBe('true');
		expect(screen.queryAllByRole('switch')).toHaveLength(0);
	});

	it('keeps an older "Selected only" agent with nothing selected, counting 0', async () => {
		agents = agents.map((a) =>
			a.id === 'agent' ? { ...a, config: { ...a.config, agent_invoke_mode: 'custom', enabled_agents: [] } as never } : a
		);
		await appData.loadProfiles();
		render(ProfileCard, { initialSelectedId: 'agent' });
		await heading('Researcher');
		expect(subagentCount()).toBe('0');
		await fireEvent.click(tab('Subagents'));
		expect(screen.getByRole('button', { name: 'Selected only' }).getAttribute('aria-pressed')).toBe('true');
		expect(screen.getByText('Nothing selected. To turn subagents off, pick None.')).toBeTruthy();
	});
});

describe('autosave and identity', () => {
	it('shows no avatar and saves the identity without one', async () => {
		render(ProfileCard, { initialSelectedId: 'agent' });
		await heading('Researcher');
		// The avatar feature is gone: no Avatar row, no picker, no art in the header.
		expect(screen.queryByText('Avatar')).toBeNull();
		expect(screen.queryByRole('button', { name: 'Change' })).toBeNull();
		expect(document.querySelector('.st-identity')?.children.length ?? 0).toBe(0);
		await fireEvent.input(screen.getByLabelText('Description'), { target: { value: 'Edited description' } });
		await waitFor(() => expect(mocks.put).toHaveBeenCalled(), { timeout: 2000 });
		const [path, payload] = mocks.put.mock.calls.at(-1)!;
		expect(path).toBe('/agents/agent');
		expect(payload.description).toBe('Edited description');
		expect(payload.config).not.toHaveProperty('avatar_id');
		expect(payload.config.system_prompt.content).toBe('Keep my original prompt');
		// Hidden legacy bindings are retired by the full-replace PUT, as before.
		expect(payload.config).not.toHaveProperty('model');
		expect(payload.config).not.toHaveProperty('permission_mode');
		// A setting the page does not edit goes back as stored (#1546).
		expect(payload.config.custom_future_key).toEqual({ keep: true });
		expect(payload).not.toHaveProperty('avatar_color');
	});

	it('keeps settings the page does not edit when another field is saved (#1546)', async () => {
		agents = agents.map((a) =>
			a.id === 'agent'
				? ({
						...a,
						config: {
							...a.config,
							enabled_plugins: ['plugins/reviewer'],
							app_config_tools: true,
							thinking_mode: 'adaptive',
							tools_preset: 'claude_code',
							cwd: '/old/dir'
						}
					} as Profile)
				: a
		);
		await appData.loadProfiles();
		render(ProfileCard, { initialSelectedId: 'agent' });
		await heading('Researcher');
		await fireEvent.input(screen.getByLabelText('Description'), { target: { value: 'Edited description' } });
		await waitFor(() => expect(mocks.put).toHaveBeenCalled(), { timeout: 2000 });
		const [path, payload] = mocks.put.mock.calls.at(-1)!;
		expect(path).toBe('/agents/agent');
		expect(payload.config).toMatchObject({
			enabled_plugins: ['plugins/reviewer'],
			app_config_tools: true,
			thinking_mode: 'adaptive',
			tools_preset: 'claude_code',
			cwd: '/old/dir'
		});
		// What the page owns still comes from the form, and retired keys stay out.
		expect(payload.config.system_prompt.content).toBe('Keep my original prompt');
		expect(payload.config).not.toHaveProperty('model');
	});
});

describe('a name over the 100 limit (code points, as the server counts)', () => {
	const settle = () => new Promise((r) => setTimeout(r, 900));
	const nameField = () => screen.getByLabelText('Name') as HTMLInputElement;
	const tooLong = () => screen.queryByText(/^Too long: \d+ of 100 characters\. Shorten it to save\.$/);
	const headerSays = (text: string) => screen.getAllByRole('status').some((s) => s.textContent?.includes(text));
	const savedNames = () => mocks.put.mock.calls.map(([, payload]) => payload.name as string);

	async function open() {
		render(ProfileCard, { initialSelectedId: 'agent' });
		await heading('Researcher');
	}

	it('keeps every letter when a combining mark is typed at the limit, and does not save (S4)', async () => {
		await open();
		const full = 'a'.repeat(100);
		await fireEvent.input(nameField(), { target: { value: full } });
		await waitFor(() => expect(savedNames()).toContain(full), { timeout: 2000 });
		const accented = 'a'.repeat(50) + '\u0301' + 'a'.repeat(50);
		await fireEvent.input(nameField(), { target: { value: accented } });
		await settle();
		expect(nameField().value).toBe(accented);
		expect(tooLong()?.textContent).toContain('101 of 100');
		expect(nameField().getAttribute('aria-invalid')).toBe('true');
		expect(headerSays('Name too long')).toBe(true);
		expect(savedNames()).not.toContain(accented);
		expect(savedNames().every((n) => Array.from(n).length <= 100)).toBe(true);
	});

	it('leaves an over-long paste whole, blocks the save, and saves again once shortened (S5)', async () => {
		await open();
		const pasted = 'x'.repeat(150);
		await fireEvent.input(nameField(), { target: { value: pasted } });
		await settle();
		// Nothing rewrote the field, so the browser's own Undo still has the paste to undo.
		expect(nameField().value).toBe(pasted);
		expect(tooLong()?.textContent).toContain('150 of 100');
		expect(mocks.put).not.toHaveBeenCalled();
		// What Undo does to the field (back to the old name) saves as usual.
		await fireEvent.input(nameField(), { target: { value: 'Researcher' } });
		await settle();
		expect(tooLong()).toBeNull();
		expect(headerSays('Name too long')).toBe(false);
	});

	it('counts an emoji as one: 99 letters and an emoji save, one more is refused', async () => {
		await open();
		const atLimit = 'a'.repeat(99) + '😀';
		await fireEvent.input(nameField(), { target: { value: atLimit } });
		await waitFor(() => expect(savedNames()).toContain(atLimit), { timeout: 2000 });
		expect(tooLong()).toBeNull();
		const over = 'a'.repeat(100) + '😀';
		await fireEvent.input(nameField(), { target: { value: over } });
		await settle();
		expect(nameField().value).toBe(over);
		expect(tooLong()?.textContent).toContain('101 of 100');
		expect(savedNames()).not.toContain(over);
	});

	it('leaves an IME composition at the limit whole and does not save it', async () => {
		await open();
		const field = nameField();
		await fireEvent.compositionStart(field);
		await fireEvent.input(field, { target: { value: 'a'.repeat(99) + 'にほ' }, isComposing: true });
		await fireEvent.compositionEnd(field);
		await fireEvent.input(field, { target: { value: 'a'.repeat(99) + 'にほ' } });
		await settle();
		expect(field.value).toBe('a'.repeat(99) + 'にほ');
		expect(tooLong()?.textContent).toContain('101 of 100');
		expect(mocks.put).not.toHaveBeenCalled();
	});
});

describe('create, duplicate, delete', () => {
	it('creates a blank agent once it has a name', async () => {
		render(ProfileCard);
		await heading('Researcher');
		await fireEvent.click(screen.getByRole('button', { name: 'New agent' }));
		const dialog = screen.getByRole('dialog', { name: 'New agent' });
		expect(within(dialog).getByRole('radio', { name: /Blank/ }).getAttribute('aria-checked')).toBe('true');
		await fireEvent.click(within(dialog).getByRole('button', { name: 'Create' }));
		expect(screen.queryByRole('dialog')).toBeNull();
		await heading('New agent');
		expect(screen.getAllByRole('status').some((s) => s.textContent?.includes('Name it to save'))).toBe(true);
		expect(list().getByText('Draft')).toBeTruthy();
		expect(mocks.post).not.toHaveBeenCalled();
		await fireEvent.input(screen.getByLabelText('Name'), { target: { value: 'Release Notes' } });
		await waitFor(() => expect(mocks.post).toHaveBeenCalled(), { timeout: 2000 });
		const [path, payload] = mocks.post.mock.calls.at(-1)!;
		expect(path).toBe('/agents');
		expect(payload.id).toBe('release-notes');
		expect(payload.name).toBe('Release Notes');
		await heading('Release Notes');
	});

	it('starts a new agent from a built-in', async () => {
		render(ProfileCard);
		await heading('Researcher');
		await fireEvent.click(screen.getByRole('button', { name: 'New agent' }));
		const dialog = screen.getByRole('dialog', { name: 'New agent' });
		await fireEvent.click(within(dialog).getByRole('radio', { name: /From a built-in/ }));
		expect((within(dialog).getByRole('combobox', { name: 'Built-in agent' }) as HTMLSelectElement).value).toBe('frontend-designer');
		await fireEvent.click(within(dialog).getByRole('button', { name: 'Create' }));
		await heading('New agent');
		await fireEvent.click(tab('Instructions'));
		expect((screen.getByLabelText('System prompt') as HTMLTextAreaElement).value).toBe('Design only.');
	});

	it('hands "Describe it" to the AI drafting dialog with the text', async () => {
		render(ProfileCard);
		await heading('Researcher');
		await fireEvent.click(screen.getByRole('button', { name: 'New agent' }));
		const dialog = screen.getByRole('dialog', { name: 'New agent' });
		await fireEvent.click(within(dialog).getByRole('radio', { name: /Describe it/ }));
		await fireEvent.input(within(dialog).getByLabelText('What should it do?'), {
			target: { value: 'Turns merged PRs into release notes.' }
		});
		await fireEvent.click(within(dialog).getByRole('button', { name: 'Continue' }));
		const ai = await screen.findByRole('dialog', { name: /Generate Agent with AI/ });
		expect(ai.querySelector('textarea')?.value).toBe('Turns merged PRs into release notes.');
	});

	it('duplicates the open agent under a new name and opens the copy', async () => {
		render(ProfileCard);
		await heading('Researcher');
		await fireEvent.click(screen.getByRole('button', { name: 'Duplicate' }));
		await waitFor(() => expect(mocks.post).toHaveBeenCalled());
		const [path, payload] = mocks.post.mock.calls[0];
		expect(path).toBe('/agents');
		expect(payload).toMatchObject({ id: 'researcher-copy', name: 'Researcher copy', description: 'Original description' });
		expect(payload).not.toHaveProperty('avatar_color');
		expect(payload.config.system_prompt.content).toBe('Keep my original prompt');
		await heading('Researcher copy');
	});

	it('deletes only after the inline confirm in the danger zone', async () => {
		render(ProfileCard);
		await heading('Researcher');
		const zone = screen.getByRole('heading', { name: 'Danger zone' }).closest('section') as HTMLElement;
		await fireEvent.click(within(zone).getByRole('button', { name: 'Delete' }));
		expect(within(zone).getByText('Delete Researcher?')).toBeTruthy();
		await fireEvent.click(within(zone).getByRole('button', { name: 'Cancel' }));
		expect(within(zone).queryByText('Delete Researcher?')).toBeNull();
		expect(mocks.del).not.toHaveBeenCalled();

		await fireEvent.click(within(zone).getByRole('button', { name: 'Delete' }));
		await fireEvent.click(within(zone).getByRole('button', { name: 'Delete' }));
		await waitFor(() => expect(mocks.del).toHaveBeenCalledWith('/agents/agent'));
		// The next agent opens.
		await heading('Frontend Designer');
	});

	it('restores a modified built-in after the confirm', async () => {
		render(ProfileCard, { initialSelectedId: 'frontend-designer' });
		await heading('Frontend Designer');
		await fireEvent.click(screen.getByRole('button', { name: 'Restore defaults' }));
		const zone = screen.getByRole('heading', { name: 'Danger zone' }).closest('section') as HTMLElement;
		expect(within(zone).getByText('Drop your edits?')).toBeTruthy();
		await fireEvent.click(within(zone).getByRole('button', { name: 'Restore' }));
		await waitFor(() => expect(mocks.post).toHaveBeenCalledWith('/agents/frontend-designer/restore'));
	});
});

describe('Models tab', () => {
	it('keeps the per-provider pickers when the worker has no subagent default (404)', async () => {
		render(ProfileCard);
		await heading('Researcher');
		await fireEvent.click(tab('Models'));
		expect(screen.queryByText('Subagent default')).toBeNull();
		expect(screen.getByText('Callers on native Claude.')).toBeTruthy();
	});

	it('shows the default and saves a new default', async () => {
		subagentDefault = { model: 'sonnet' };
		render(ProfileCard);
		await heading('Researcher');
		await fireEvent.click(tab('Models'));
		expect(await screen.findByText('Subagent default')).toBeTruthy();
		// The legacy top-level model counts as a pin.
		expect(screen.getByRole('radio', { name: /Pin a model/ }).getAttribute('aria-checked')).toBe('true');
		expect(screen.queryByRole('button', { name: 'Switch' })).toBeNull();
		expect(screen.queryByText(/Models agents may choose from/)).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: 'Change' }));
		expect(screen.getByRole('button', { name: 'Done' })).toBeTruthy();
	});
});
