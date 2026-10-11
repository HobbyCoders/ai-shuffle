/**
 * The opt-in starter team on the Agents page: AI Shuffle ships no built-in
 * subagents, so the owner installs the starter team from the empty state or
 * the list header. The header offer hides once any starter role exists.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({
	get: vi.fn(),
	put: vi.fn(),
	post: vi.fn(),
	del: vi.fn(),
	notify: vi.fn()
}));
vi.mock('$lib/api/client', async (original) => ({
	...(await original<typeof import('$lib/api/client')>()),
	api: { get: mocks.get, put: mocks.put, post: mocks.post, delete: mocks.del }
}));
vi.mock('$lib/api/assistant', async (original) => ({
	...(await original<typeof import('$lib/api/assistant')>()),
	getAssistant: async () => null,
	getAgentIdentities: async () => []
}));
vi.mock('$lib/stores/notifications', async (original) => ({
	...(await original<typeof import('$lib/stores/notifications')>()),
	notify: mocks.notify
}));
vi.mock('$lib/services/watch-resources', () => ({ watchResources: () => {} }));
vi.mock('$lib/stores/auth.svelte', async (original) => ({
	...(await original<typeof import('$lib/stores/auth.svelte')>()),
	isAdmin: (await import('svelte/store')).writable(true)
}));
vi.mock('$lib/api/toolSurface', async (original) => ({
	...(await original<typeof import('$lib/api/toolSurface')>()),
	fetchToolSurface: async () => ({ groups: [], errors: [], discovered_at: '' })
}));

import ProfileCard from './ProfileCard.svelte';
import { appData } from '$lib/stores/app-data.svelte';
import { assistantState, agentIdentities } from '$lib/stores/assistant';
import type { Profile } from '$lib/api/client';

const INSTALL = '/agents/templates/starter-team/install';

let agents: Profile[];
/** What GET /agents/team-templates answers; an Error instance is thrown. */
let teamTemplates: unknown;
let installFails: boolean;

const agent = (id: string, name: string): Profile => ({
	id,
	name,
	description: `${name} agent`,
	is_builtin: false,
	is_modified: false,
	created_at: '',
	updated_at: '',
	config: { system_prompt: { type: 'custom', content: '' } }
});

function starter(gp: string | null = null, fd: string | null = null) {
	return {
		id: 'starter-team',
		name: 'Starter team',
		description: 'A general coder and a frontend designer.',
		agents: [
			{ key: 'general-purpose', name: 'General Purpose', existing_agent_id: gp },
			{ key: 'frontend-designer', name: 'Frontend Designer', existing_agent_id: fd }
		]
	};
}

beforeEach(async () => {
	vi.clearAllMocks();
	try {
		sessionStorage.clear();
	} catch {
		/* no storage */
	}
	agents = [];
	teamTemplates = [starter()];
	installFails = false;
	assistantState.set(null);
	agentIdentities.set([]);
	mocks.get.mockImplementation(async (path: string) => {
		if (path === '/agents') return agents;
		if (path === '/agents/team-templates') {
			if (teamTemplates instanceof Error) throw teamTemplates;
			return teamTemplates;
		}
		if (path === '/settings/subagent-default') throw { status: 404, detail: 'Not Found' };
		return [];
	});
	mocks.post.mockImplementation(async (path: string) => {
		if (path !== INSTALL) return {};
		if (installFails) throw { status: 500, detail: 'disk full' };
		agents = [...agents, agent('gp-1', 'General Purpose'), agent('fd-1', 'Frontend Designer')];
		teamTemplates = [starter('gp-1', 'fd-1')];
		return starter('gp-1', 'fd-1');
	});
	await appData.loadProfiles();
});
afterEach(() => cleanup());

const installButtons = () => screen.queryAllByRole('button', { name: 'Install starter team' });
const list = () => within(screen.getByRole('region', { name: 'Agent list' }));

describe('starter team', () => {
	it('offers the install in the empty state and the list header', async () => {
		render(ProfileCard);
		await screen.findByRole('heading', { level: 2, name: 'Make your first agent' });
		await waitFor(() => expect(installButtons()).toHaveLength(2));
		expect(
			screen.getByText('Start blank, or install a starter team: General Purpose and Frontend Designer.')
		).toBeTruthy();
		// Starting blank stays available beside it.
		const details = within(screen.getByRole('region', { name: 'Details' }));
		expect(details.getByRole('button', { name: 'New agent' })).toBeTruthy();
		expect(details.getByRole('button', { name: 'Install starter team' })).toBeTruthy();
	});

	it('installs, lists the new agents and drops the offer', async () => {
		render(ProfileCard);
		await waitFor(() => expect(installButtons()).toHaveLength(2));
		await fireEvent.click(installButtons()[0]);
		await waitFor(() => expect(mocks.post).toHaveBeenCalledWith(INSTALL));
		await waitFor(() => expect(installButtons()).toHaveLength(0));
		expect(list().getByRole('button', { name: /^General Purpose/ })).toBeTruthy();
		expect(list().getByRole('button', { name: /^Frontend Designer/ })).toBeTruthy();
		expect(mocks.notify).toHaveBeenCalledWith('Installed the starter team.', { severity: 'success' });
		// The first new agent opens.
		await screen.findByRole('heading', { level: 2, name: 'General Purpose' });
	});

	it('hides the header offer when any starter role already exists', async () => {
		agents = [agent('fd-1', 'Frontend Designer')];
		teamTemplates = [starter(null, 'fd-1')];
		await appData.loadProfiles();
		render(ProfileCard);
		await screen.findByRole('heading', { level: 2, name: 'Frontend Designer' });
		await waitFor(() => expect(mocks.get).toHaveBeenCalledWith('/agents/team-templates'));
		expect(installButtons()).toHaveLength(0);
	});

	it('offers nothing on a worker without team templates', async () => {
		teamTemplates = Object.assign(new Error('Not Found'), { status: 404 });
		render(ProfileCard);
		await screen.findByRole('heading', { level: 2, name: 'Make your first agent' });
		await waitFor(() => expect(mocks.get).toHaveBeenCalledWith('/agents/team-templates'));
		expect(installButtons()).toHaveLength(0);
		expect(screen.getByText('Yours go here.')).toBeTruthy();
	});

	it('says why an install failed and lets the owner retry', async () => {
		installFails = true;
		render(ProfileCard);
		await waitFor(() => expect(installButtons()).toHaveLength(2));
		await fireEvent.click(installButtons()[1]);
		await waitFor(() =>
			expect(mocks.notify).toHaveBeenCalledWith('Failed to install the starter team: disk full', {
				severity: 'error'
			})
		);
		await waitFor(() => expect(installButtons()).toHaveLength(2));
		for (const b of installButtons()) expect((b as HTMLButtonElement).disabled).toBe(false);
	});
});
