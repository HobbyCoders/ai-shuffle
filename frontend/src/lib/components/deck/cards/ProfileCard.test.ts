/**
 * The Agents page (#1825) end to end against faked API replies: the list and
 * its filters, the detail tabs, autosave, create, duplicate and delete, the
 * avatar colour, and the Models tab with and without the subagent default.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({
	get: vi.fn(),
	put: vi.fn(),
	post: vi.fn(),
	del: vi.fn(),
	getAssistant: vi.fn(),
	getAgentIdentities: vi.fn(),
	updateAgentIdentity: vi.fn(),
	clearFeatureItemId: vi.fn(),
	openFeaturePage: vi.fn()
}));
vi.mock('$lib/api/client', async (original) => ({
	...(await original<typeof import('$lib/api/client')>()),
	api: { get: mocks.get, put: mocks.put, post: mocks.post, delete: mocks.del }
}));
vi.mock('$lib/api/assistant', async (original) => ({
	...(await original<typeof import('$lib/api/assistant')>()),
	getAssistant: mocks.getAssistant,
	getAgentIdentities: mocks.getAgentIdentities,
	updateAgentIdentity: mocks.updateAgentIdentity
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
import { assistantState, agentIdentities } from '$lib/stores/assistant';
import type { Profile } from '$lib/api/client';

let serverAvatar: string;
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
		avatar_id: 'robot',
		system_prompt: { type: 'custom', content: 'Keep my original prompt' },
		model: 'original-model',
		permission_mode: 'default',
		custom_future_key: { keep: true },
		tool_selection_mode: 'all'
	} as never
});
const explore = (): Profile => ({
	id: 'Explore',
	name: 'Explore',
	description: 'Read-only search',
	is_builtin: true,
	is_modified: true,
	created_at: '',
	updated_at: '',
	config: { system_prompt: { type: 'custom', content: 'Search only.' }, tool_selection_mode: 'allow', allowed_tools: ['Read'] }
});

beforeEach(async () => {
	vi.clearAllMocks();
	try {
		sessionStorage.clear();
	} catch {
		/* no storage */
	}
	serverAvatar = 'robot';
	subagentDefault = null;
	agents = [researcher(), explore()];
	assistantState.set(null);
	agentIdentities.set([]);
	mocks.get.mockImplementation(async (path: string) => {
		if (path === '/agents') return agents.map((a) => ({ ...a, config: { ...a.config, avatar_id: serverAvatar } }));
		if (path === '/settings/subagent-default') {
			if (subagentDefault === null) throw { status: 404, detail: 'Not Found' };
			return subagentDefault;
		}
		return [];
	});
	mocks.getAssistant.mockResolvedValue(null);
	mocks.getAgentIdentities.mockImplementation(async () => [{ id: 'agent', name: 'Researcher', avatar_id: serverAvatar }]);
	mocks.updateAgentIdentity.mockImplementation(async (id, patch) => {
		if (id === 'agent') serverAvatar = patch.avatar_id;
		return { id, name: 'Researcher', avatar_id: patch.avatar_id };
	});
	// Model the normal PUT contract: an omitted identity preserves its latest
	// narrow PATCH, while a stale explicit identity would overwrite it.
	mocks.put.mockImplementation(async (path: string, payload: any) => {
		if (path === '/settings/subagent-default') return payload;
		if (Object.hasOwn(payload.config, 'avatar_id')) serverAvatar = payload.config.avatar_id;
		const id = path.split('/').pop();
		agents = agents.map((a) => (a.id === id ? { ...a, ...payload } : a));
		return { ...payload, config: { ...payload.config, avatar_id: serverAvatar } };
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
		expect(within(row(/^Explore/)).getByText('Modified')).toBeTruthy();
	});

	it('searches and clears', async () => {
		render(ProfileCard);
		await heading('Researcher');
		await fireEvent.input(screen.getByPlaceholderText('Search agents'), { target: { value: 'read-only' } });
		expect(row(/^Explore/)).toBeTruthy();
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
		expect(row(/^Explore/)).toBeTruthy();
	});

	it('opens another agent from its row', async () => {
		render(ProfileCard);
		await heading('Researcher');
		await fireEvent.click(row(/^Explore/));
		await heading('Explore');
		expect(screen.getAllByText('Built-in').length).toBeGreaterThan(1);
	});
});

describe('detail', () => {
	it('shows the facts and opens a tab from its fact', async () => {
		render(ProfileCard);
		await heading('Researcher');
		expect(screen.getByText('Chats')).toBeTruthy();
		expect(screen.getByText('7')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: /^Tools: .*Open Tools/ }));
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

	it('shows Duplicate, Export and Delete for your agent, Restore for a modified built-in, and no menu', async () => {
		render(ProfileCard);
		await heading('Researcher');
		expect(screen.getByRole('button', { name: 'Duplicate' })).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Export .json' })).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Delete agent' })).toBeTruthy();
		expect(screen.queryByRole('button', { name: /More/ })).toBeNull();
		await fireEvent.click(row(/^Explore/));
		await heading('Explore');
		expect(screen.getByRole('button', { name: 'Restore defaults' })).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'Delete agent' })).toBeNull();
	});
});

describe('subagents roster', () => {
	const fact = () => screen.getByRole('button', { name: /^Subagents: / });

	it('saves None with an empty selection and says None in the header', async () => {
		agents = agents.map((a) =>
			a.id === 'agent' ? { ...a, config: { ...a.config, agent_invoke_mode: 'custom', enabled_agents: ['Explore'] } as never } : a
		);
		await appData.loadProfiles();
		render(ProfileCard, { initialSelectedId: 'agent' });
		await heading('Researcher');
		expect(fact().getAttribute('aria-label')).toMatch(/^Subagents: 1 of 1\./);
		await fireEvent.click(tab('Subagents'));
		await fireEvent.click(screen.getByRole('button', { name: 'None' }));
		expect(fact().getAttribute('aria-label')).toMatch(/^Subagents: None\./);
		await waitFor(() => expect(mocks.put).toHaveBeenCalled(), { timeout: 2000 });
		const [path, payload] = mocks.put.mock.calls.at(-1)!;
		expect(path).toBe('/agents/agent');
		expect(payload.config.agent_invoke_mode).toBe('none');
		expect(payload.config.enabled_agents).toEqual([]);
		// Back to Selected only: the old pick is gone, nothing is selected.
		await fireEvent.click(screen.getByRole('button', { name: 'Selected only' }));
		expect(fact().getAttribute('aria-label')).toMatch(/^Subagents: 0 of 1\./);
		expect(screen.getByRole('switch', { name: 'Allow Explore' }).getAttribute('aria-checked')).toBe('false');
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
		expect(fact().getAttribute('aria-label')).toMatch(/^Subagents: None\./);
		await fireEvent.click(tab('Subagents'));
		expect(screen.getByRole('button', { name: 'None' }).getAttribute('aria-pressed')).toBe('true');
		expect(screen.queryAllByRole('switch')).toHaveLength(0);
	});

	it('keeps an older "Selected only" agent with nothing selected as 0 of N', async () => {
		agents = agents.map((a) =>
			a.id === 'agent' ? { ...a, config: { ...a.config, agent_invoke_mode: 'custom', enabled_agents: [] } as never } : a
		);
		await appData.loadProfiles();
		render(ProfileCard, { initialSelectedId: 'agent' });
		await heading('Researcher');
		expect(fact().getAttribute('aria-label')).toMatch(/^Subagents: 0 of 1\./);
		await fireEvent.click(tab('Subagents'));
		expect(screen.getByRole('button', { name: 'Selected only' }).getAttribute('aria-pressed')).toBe('true');
		expect(screen.getByText('Nothing selected. To turn subagents off, pick None.')).toBeTruthy();
	});
});

describe('autosave and identity', () => {
	it('omits the stale read-projected avatar after an identity edit', async () => {
		render(ProfileCard, { initialSelectedId: 'agent' });
		await heading('Researcher');
		await fireEvent.click(await screen.findByRole('button', { name: 'Change' }));
		await fireEvent.click(await screen.findByRole('button', { name: 'orbit avatar' }));
		await waitFor(() => expect(mocks.updateAgentIdentity).toHaveBeenCalledWith('agent', { avatar_id: 'orbit' }));
		expect(screen.getByRole('button', { name: 'orbit avatar' }).getAttribute('aria-pressed')).toBe('true');
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
		expect(payload.config).not.toHaveProperty('custom_future_key');
		// No colour field on this worker, so none is sent.
		expect(payload).not.toHaveProperty('avatar_color');
		expect(serverAvatar).toBe('orbit');
	});

	it('hides the colour choice when the agent API has no avatar_color', async () => {
		render(ProfileCard);
		await heading('Researcher');
		await fireEvent.click(screen.getByRole('button', { name: 'Change' }));
		expect(screen.queryByRole('radiogroup', { name: 'Colour' })).toBeNull();
	});

	it('saves the colour through the agent save when the field exists', async () => {
		agents = agents.map((a) => ({ ...a, avatar_color: null }));
		await appData.loadProfiles();
		render(ProfileCard);
		await heading('Researcher');
		await fireEvent.click(screen.getByRole('button', { name: 'Change' }));
		await fireEvent.click(screen.getByRole('radio', { name: 'Green' }));
		await waitFor(() => expect(mocks.put).toHaveBeenCalled(), { timeout: 2000 });
		const [, payload] = mocks.put.mock.calls.at(-1)!;
		expect(payload.avatar_color).toBe('emerald');
		expect(payload.config).not.toHaveProperty('avatar_color');
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
		expect((within(dialog).getByLabelText('Built-in') as HTMLSelectElement).value).toBe('Explore');
		await fireEvent.click(within(dialog).getByRole('button', { name: 'Create' }));
		await heading('New agent');
		await fireEvent.click(tab('Instructions'));
		expect((screen.getByLabelText('System prompt') as HTMLTextAreaElement).value).toBe('Search only.');
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
		expect(payload.config).not.toHaveProperty('avatar_id');
		expect(payload.config.system_prompt.content).toBe('Keep my original prompt');
		await waitFor(() => expect(mocks.updateAgentIdentity).toHaveBeenCalledWith('researcher-copy', { avatar_id: 'robot' }));
		await heading('Researcher copy');
	});

	it('deletes only after the inline confirm in the danger zone', async () => {
		render(ProfileCard);
		await heading('Researcher');
		await fireEvent.click(screen.getByRole('button', { name: 'Delete agent' }));
		const zone = screen.getByRole('heading', { name: 'Danger zone' }).closest('section') as HTMLElement;
		expect(within(zone).getByText('Delete Researcher?')).toBeTruthy();
		await fireEvent.click(within(zone).getByRole('button', { name: 'Cancel' }));
		expect(within(zone).queryByText('Delete Researcher?')).toBeNull();
		expect(mocks.del).not.toHaveBeenCalled();

		await fireEvent.click(within(zone).getByRole('button', { name: 'Delete' }));
		await fireEvent.click(within(zone).getByRole('button', { name: 'Delete' }));
		await waitFor(() => expect(mocks.del).toHaveBeenCalledWith('/agents/agent'));
		// The next agent opens.
		await heading('Explore');
	});

	it('restores a modified built-in after the confirm', async () => {
		render(ProfileCard, { initialSelectedId: 'Explore' });
		await heading('Explore');
		await fireEvent.click(screen.getByRole('button', { name: 'Restore defaults' }));
		const zone = screen.getByRole('heading', { name: 'Danger zone' }).closest('section') as HTMLElement;
		expect(within(zone).getByText('Drop your edits?')).toBeTruthy();
		await fireEvent.click(within(zone).getByRole('button', { name: 'Restore' }));
		await waitFor(() => expect(mocks.post).toHaveBeenCalledWith('/agents/Explore/restore'));
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
