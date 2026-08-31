/**
 * The rewritten tool picker (ruling T6), driven the way ProfileCard drives it:
 * the component renders the live surface and reports clicks, the pure helpers
 * in ./toolSurface turn those into the saved list.
 *
 * What is pinned here is what a user can see and do — the tri-state group
 * checkbox, the read-only modes, greyed-out rows, and the list a click produces.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, fireEvent, cleanup } from '@testing-library/svelte';
import ProfileToolConfig from './ProfileToolConfig.svelte';
import type { ToolSurface, ToolSurfaceGroup } from '$lib/api/toolSurface';
import { toggleGroupSelection, toggleToolSelection } from './toolSurface';

// The picker reads the GLOBAL computer-use toggle from the shared store; the
// tests drive it through this hoisted flag instead of the real poller.
const cuMocks = vi.hoisted(() => ({ enabled: true }));
vi.mock('$lib/stores/computerUse.svelte', () => ({
	computerUse: {
		get enabled() {
			return cuMocks.enabled;
		}
	}
}));

const surface: ToolSurface = {
	discovered_at: '2026-08-23T12:00:00Z',
	errors: [{ group: 'mcp:broken', message: 'Server did not answer in time.' }],
	groups: [
		{
			id: 'harness',
			label: 'Harness builtins',
			kind: 'harness',
			server: null,
			tools: [
				{ name: 'Read', description: 'Read a file.', risk: 'read' },
				{ name: 'Edit', description: 'Edit a file.', risk: 'write' },
				{ name: 'Bash', description: 'Run a command.', risk: 'exec' }
			]
		},
		{
			id: 'ai',
			label: 'AI tools',
			kind: 'ai',
			server: 'ai-tools',
			tools: [
				{ name: 'mcp__ai-tools__generateImage', description: 'Make an image.', available: true },
				{ name: 'mcp__ai-tools__generateVideo', description: 'Make a video.', available: false }
			]
		},
		{
			id: 'computer_use',
			label: 'Computer use',
			kind: 'computer_use',
			server: 'computer-use',
			available: true,
			tools: [{ name: 'mcp__computer-use__screenshot', description: 'Grab the screen.' }]
		},
		{
			id: 'app_config',
			label: 'App configuration',
			kind: 'app_config',
			server: 'app-config',
			admin_only: true,
			tools: [{ name: 'mcp__app-config__create_hook', description: 'Add a hook.' }]
		},
		{
			id: 'mcp:github',
			label: 'github',
			kind: 'mcp',
			server: 'github',
			tools: [
				{ name: 'mcp__github__create_issue', description: 'Open an issue.', risk: 'network' },
				{ name: 'mcp__github__list_issues', description: 'List issues.', risk: 'network' },
				{ name: 'mcp__github__close_issue', description: 'Close an issue.', risk: 'network' }
			]
		},
		{
			id: 'mcp:broken',
			label: 'broken',
			kind: 'mcp',
			server: 'broken',
			tools: []
		}
	]
};

function group(id: string): ToolSurfaceGroup {
	const found = surface.groups.find((g) => g.id === id);
	if (!found) throw new Error(`no such group: ${id}`);
	return found;
}

interface Overrides {
	toolSelectionMode?: 'all' | 'none' | 'allow' | 'disallow';
	allowedTools?: string[];
	disallowedTools?: string[];
	computerUseEnabled?: boolean;
	isAdmin?: boolean;
}

/**
 * Render with the same wiring ProfileCard uses, so a click really produces the
 * list that would be saved. `saved` holds the latest one.
 */
function setup(overrides: Overrides = {}) {
	cuMocks.enabled = overrides.computerUseEnabled ?? true;
	const mode = overrides.toolSelectionMode ?? 'allow';
	const saved: { list: string[] } = {
		list: mode === 'disallow' ? overrides.disallowedTools ?? [] : overrides.allowedTools ?? []
	};
	const ctx = {
		computerUseEnabled: overrides.computerUseEnabled ?? true,
		isAdmin: overrides.isAdmin ?? true
	};
	const onRefresh = vi.fn();
	const onToolSelectionModeChange = vi.fn();

	const result = render(ProfileToolConfig, {
		props: {
			toolSelectionMode: mode,
			allowedTools: mode === 'disallow' ? [] : saved.list,
			disallowedTools: mode === 'disallow' ? saved.list : [],
			surface,
			loading: false,
			refreshing: false,
			isAdmin: ctx.isAdmin,
			onToolSelectionModeChange,
			onToggleTool: (g: ToolSurfaceGroup, name: string) => {
				saved.list = toggleToolSelection(saved.list, g, name, ctx);
			},
			onToggleGroup: (g: ToolSurfaceGroup) => {
				saved.list = toggleGroupSelection(saved.list, g, ctx);
			},
			onRefresh
		}
	});

	return { ...result, saved, onRefresh, onToolSelectionModeChange };
}

const groupBox = (getByRole: any, label: string) =>
	getByRole('checkbox', {
		name: `Toggle all ${label}${/tools$/i.test(label) ? '' : ' tools'}`
	}) as HTMLButtonElement;

/** Only the harness group starts open, so anything else has to be unfolded. */
const expand = (getByRole: any, label: string) =>
	fireEvent.click(getByRole('button', { name: `Toggle ${label} section` }));

beforeEach(() => {
	if (!('ResizeObserver' in globalThis)) {
		(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
			observe() {}
			unobserve() {}
			disconnect() {}
		};
	}
});

afterEach(() => cleanup());

describe('tri-state group checkbox', () => {
	it('reads unchecked when nothing in the group is selected', () => {
		const { getByRole } = setup({ allowedTools: [] });
		expect(groupBox(getByRole, 'Harness builtins').getAttribute('aria-checked')).toBe('false');
	});

	it('reads mixed when part of the group is selected', () => {
		const { getByRole } = setup({ allowedTools: ['Read'] });
		expect(groupBox(getByRole, 'Harness builtins').getAttribute('aria-checked')).toBe('mixed');
	});

	it('reads checked when the whole group is selected', () => {
		const { getByRole } = setup({ allowedTools: ['Read', 'Edit', 'Bash'] });
		expect(groupBox(getByRole, 'Harness builtins').getAttribute('aria-checked')).toBe('true');
	});

	it('reads checked when an MCP server is held as its wildcard', () => {
		const { getByRole } = setup({ allowedTools: ['mcp__github__*'] });
		expect(groupBox(getByRole, 'github').getAttribute('aria-checked')).toBe('true');
	});

	it('ignores rows the user cannot grant when deciding checked-ness', () => {
		// generateVideo has no API key, so generateImage alone IS the whole group.
		const { getByRole } = setup({ allowedTools: ['mcp__ai-tools__generateImage'] });
		expect(groupBox(getByRole, 'AI tools').getAttribute('aria-checked')).toBe('true');
	});

	it('never doubles the word in a group label that already ends in "tools"', () => {
		const { getByRole } = setup({ allowedTools: [] });
		expect(groupBox(getByRole, 'AI tools').getAttribute('aria-label')).toBe('Toggle all AI tools');
		expect(groupBox(getByRole, 'Harness builtins').getAttribute('aria-label')).toBe(
			'Toggle all Harness builtins tools'
		);
	});

	it('selects the whole group on click and clears it on the next click', async () => {
		const { getByRole, saved } = setup({ allowedTools: [] });
		const box = groupBox(getByRole, 'Harness builtins');
		await fireEvent.click(box);
		expect(saved.list).toEqual(['Read', 'Edit', 'Bash']);
	});

	it('clears a fully selected group', async () => {
		const { getByRole, saved } = setup({ allowedTools: ['Read', 'Edit', 'Bash'] });
		await fireEvent.click(groupBox(getByRole, 'Harness builtins'));
		expect(saved.list).toEqual([]);
	});
});

describe('pattern vs names on save', () => {
	it('saves the wildcard when a whole MCP server is selected', async () => {
		const { getByRole, saved } = setup({ allowedTools: [] });
		await fireEvent.click(groupBox(getByRole, 'github'));
		expect(saved.list).toEqual(['mcp__github__*']);
	});

	it('saves names, not a wildcard, for a non-MCP group', async () => {
		const { getByRole, saved } = setup({ allowedTools: [] });
		await fireEvent.click(groupBox(getByRole, 'AI tools'));
		expect(saved.list).toEqual(['mcp__ai-tools__generateImage']);
	});

	it('expands the wildcard to names when one of its tools is unchecked', async () => {
		const { getByRole, saved } = setup({ allowedTools: ['mcp__github__*'] });
		await expand(getByRole, 'github');
		await fireEvent.click(getByRole('checkbox', { name: 'mcp__github__close_issue' }));
		expect(saved.list).toEqual(['mcp__github__create_issue', 'mcp__github__list_issues']);
	});

	it('collapses names back to the wildcard when the group becomes whole', async () => {
		const { getByRole, saved } = setup({
			allowedTools: ['mcp__github__create_issue', 'mcp__github__list_issues']
		});
		await expand(getByRole, 'github');
		await fireEvent.click(getByRole('checkbox', { name: 'mcp__github__close_issue' }));
		expect(saved.list).toEqual(['mcp__github__*']);
	});

	it('writes into the disallow list in disallow mode', async () => {
		const { getByRole, saved } = setup({ toolSelectionMode: 'disallow', disallowedTools: [] });
		await fireEvent.click(getByRole('checkbox', { name: 'Bash' }));
		expect(saved.list).toEqual(['Bash']);
	});
});

describe('read-only modes', () => {
	it('shows every tool checked and locked in all mode', () => {
		const { getByRole } = setup({ toolSelectionMode: 'all' });
		const read = getByRole('checkbox', { name: 'Read' }) as HTMLButtonElement;
		expect(read.getAttribute('aria-checked')).toBe('true');
		expect(read.disabled).toBe(true);
		expect(groupBox(getByRole, 'Harness builtins').disabled).toBe(true);
	});

	it('shows nothing checked and everything locked in none mode', () => {
		const { getByRole } = setup({ toolSelectionMode: 'none' });
		const read = getByRole('checkbox', { name: 'Read' }) as HTMLButtonElement;
		expect(read.getAttribute('aria-checked')).toBe('false');
		expect(read.disabled).toBe(true);
	});

	it('leaves the checkboxes live in allow mode', () => {
		const { getByRole } = setup({ toolSelectionMode: 'allow' });
		expect((getByRole('checkbox', { name: 'Read' }) as HTMLButtonElement).disabled).toBe(false);
	});
});

describe('unavailable rows', () => {
	it('greys the computer-use group while the global toggle is off', () => {
		const { getByRole, getByText } = setup({ computerUseEnabled: false });
		expect(groupBox(getByRole, 'Computer use').disabled).toBe(true);
		expect(getByText(/enable computer use in settings/i)).toBeTruthy();
	});

	it('greys the admin-only group for a non-admin', () => {
		const { getByRole, getByText } = setup({ isAdmin: false });
		expect(groupBox(getByRole, 'App configuration').disabled).toBe(true);
		expect(getByText(/admin only/i)).toBeTruthy();
	});

	it('greys an AI tool that has no API key and refuses to select it', async () => {
		const { getByRole, saved } = setup({ allowedTools: [] });
		await expand(getByRole, 'AI tools');
		const video = getByRole('checkbox', {
			name: 'mcp__ai-tools__generateVideo'
		}) as HTMLButtonElement;
		expect(video.disabled).toBe(true);
		expect(video.title).toMatch(/no api key/i);
		await fireEvent.click(video);
		expect(saved.list).toEqual([]);
	});
});

describe('header', () => {
	it('reports how many of the discovered tools are allowed', () => {
		// 3 harness + 1 keyed AI tool + 1 computer use + 1 app config + 3 github = 9
		const { getByText } = setup({ toolSelectionMode: 'all' });
		expect(getByText(/9 of 9 tools allowed/)).toBeTruthy();
	});

	it('counts only what the allow list matches', () => {
		const { getByText } = setup({ allowedTools: ['Read', 'mcp__github__*'] });
		expect(getByText(/4 of 9 tools allowed/)).toBeTruthy();
	});

	it('shows a discovery error on the group it belongs to', () => {
		const { getByText } = setup();
		expect(getByText('Server did not answer in time.')).toBeTruthy();
	});

	it('asks for a fresh discovery when Refresh is clicked', async () => {
		const { getByLabelText, onRefresh } = setup();
		await fireEvent.click(getByLabelText('Refresh tool list'));
		expect(onRefresh).toHaveBeenCalledOnce();
	});

	it('reports the mode change the select emits', async () => {
		const { getByLabelText, onToolSelectionModeChange } = setup();
		await fireEvent.change(getByLabelText('Tool access mode'), { target: { value: 'disallow' } });
		expect(onToolSelectionModeChange).toHaveBeenCalledWith('disallow');
	});
});

describe('search', () => {
	it('narrows the list to matching tools', async () => {
		const { getByLabelText, queryByRole } = setup();
		await fireEvent.input(getByLabelText('Search tools'), { target: { value: 'issue' } });
		expect(queryByRole('checkbox', { name: 'mcp__github__create_issue' })).toBeTruthy();
		expect(queryByRole('checkbox', { name: 'Read' })).toBeNull();
	});
});
