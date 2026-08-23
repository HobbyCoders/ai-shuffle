/**
 * The tool picker's selection rules (rulings T2 / T6).
 *
 * Two things have to hold no matter how the UI is wired: the tri-state group
 * checkbox reports all/some/none over the rows a user may actually grant, and
 * a whole MCP server saves as `mcp__<server>__*` while a partial selection
 * saves names.
 */
import { describe, it, expect } from 'vitest';
import type { ToolSurfaceGroup } from '$lib/api/toolSurface';
import {
	countAllowedTools,
	groupBlockedReason,
	groupPattern,
	groupSelectionState,
	matchesToolEntry,
	profileToolCount,
	resolveToolMode,
	selectableTools,
	sortGroups,
	toggleGroupSelection,
	toggleToolSelection,
	toolBlockedReason,
	type SelectabilityContext
} from './toolSurface';

const OPEN: SelectabilityContext = { computerUseEnabled: true, isAdmin: true };
const LOCKED: SelectabilityContext = { computerUseEnabled: false, isAdmin: false };

const harness: ToolSurfaceGroup = {
	id: 'harness',
	label: 'Harness builtins',
	kind: 'harness',
	server: null,
	tools: [
		{ name: 'Read', description: 'Read a file.', risk: 'read' },
		{ name: 'Edit', description: 'Edit a file.', risk: 'write' },
		{ name: 'Bash', description: 'Run a command.', risk: 'exec' }
	]
};

const aiTools: ToolSurfaceGroup = {
	id: 'ai',
	label: 'AI tools',
	kind: 'ai',
	server: 'ai-tools',
	tools: [
		{ name: 'mcp__ai-tools__generateImage', description: 'Make an image.', available: true },
		{ name: 'mcp__ai-tools__generateVideo', description: 'Make a video.', available: false }
	]
};

const github: ToolSurfaceGroup = {
	id: 'mcp:github',
	label: 'github',
	kind: 'mcp',
	server: 'github',
	tools: [
		{ name: 'mcp__github__create_issue', description: 'Open an issue.', risk: 'network' },
		{ name: 'mcp__github__list_issues', description: 'List issues.', risk: 'network' },
		{ name: 'mcp__github__close_issue', description: 'Close an issue.', risk: 'network' }
	]
};

const computerUse: ToolSurfaceGroup = {
	id: 'computer_use',
	label: 'Computer use',
	kind: 'computer_use',
	server: 'computer-use',
	available: true,
	tools: [{ name: 'mcp__computer-use__screenshot', description: 'Grab the screen.' }]
};

const appConfig: ToolSurfaceGroup = {
	id: 'app_config',
	label: 'App configuration',
	kind: 'app_config',
	server: 'app-config',
	admin_only: true,
	tools: [{ name: 'mcp__app-config__create_hook', description: 'Add a hook.' }]
};

describe('resolveToolMode', () => {
	it('takes the saved mode verbatim', () => {
		expect(resolveToolMode({ tool_selection_mode: 'allow', allowed_tools: [] })).toBe('allow');
		expect(resolveToolMode({ tool_selection_mode: 'none' })).toBe('none');
		expect(resolveToolMode({ tool_selection_mode: 'disallow' })).toBe('disallow');
		expect(resolveToolMode({ tool_selection_mode: 'all', disallowed_tools: ['Bash'] })).toBe('all');
	});

	it('falls back to the legacy reading for rows saved before the mode existed', () => {
		expect(resolveToolMode({ allowed_tools: [] })).toBe('none');
		expect(resolveToolMode({ allowed_tools: ['Read'] })).toBe('allow');
		expect(resolveToolMode({ disallowed_tools: ['Bash'] })).toBe('disallow');
		expect(resolveToolMode({})).toBe('all');
		expect(resolveToolMode(null)).toBe('all');
	});

	it('ignores a mode value that is not one of the four', () => {
		expect(resolveToolMode({ tool_selection_mode: 'whatever', allowed_tools: ['Read'] })).toBe('allow');
	});
});

describe('group ordering', () => {
	it('puts the fixed kinds first and MCP servers last, alphabetically', () => {
		const zed: ToolSurfaceGroup = { ...github, id: 'mcp:zed', label: 'zed' };
		const ordered = sortGroups([zed, appConfig, github, harness, aiTools, computerUse]);
		expect(ordered.map((g) => g.id)).toEqual([
			'harness',
			'ai',
			'computer_use',
			'app_config',
			'mcp:github',
			'mcp:zed'
		]);
	});
});

describe('availability', () => {
	it('greys the computer-use group until the profile switch is on', () => {
		expect(groupBlockedReason(computerUse, LOCKED)).toMatch(/enable computer use above/i);
		expect(groupBlockedReason(computerUse, OPEN)).toBeNull();
	});

	it('greys admin-only groups for a non-admin', () => {
		expect(groupBlockedReason(appConfig, LOCKED)).toMatch(/admin only/i);
		expect(groupBlockedReason(appConfig, OPEN)).toBeNull();
	});

	it('greys an AI tool with no API key, but not its siblings', () => {
		expect(toolBlockedReason(aiTools, aiTools.tools[1], OPEN)).toMatch(/no api key/i);
		expect(toolBlockedReason(aiTools, aiTools.tools[0], OPEN)).toBeNull();
		expect(selectableTools(aiTools, OPEN).map((t) => t.name)).toEqual([
			'mcp__ai-tools__generateImage'
		]);
	});

	it('offers nothing at all from a blocked group', () => {
		expect(selectableTools(computerUse, LOCKED)).toEqual([]);
		expect(selectableTools(appConfig, LOCKED)).toEqual([]);
	});
});

describe('tri-state group checkbox', () => {
	it('is none when nothing in the group is picked', () => {
		expect(groupSelectionState(harness, [], OPEN)).toBe('none');
		expect(groupSelectionState(harness, ['mcp__github__list_issues'], OPEN)).toBe('none');
	});

	it('is some when part of the group is picked', () => {
		expect(groupSelectionState(harness, ['Read'], OPEN)).toBe('some');
		expect(groupSelectionState(harness, ['Read', 'Bash'], OPEN)).toBe('some');
	});

	it('is all when every row is picked', () => {
		expect(groupSelectionState(harness, ['Read', 'Edit', 'Bash'], OPEN)).toBe('all');
	});

	it('reads the server pattern as the whole group', () => {
		expect(groupSelectionState(github, ['mcp__github__*'], OPEN)).toBe('all');
	});

	it('ignores unselectable rows when deciding all-ness', () => {
		// generateVideo has no key, so picking generateImage IS the whole group.
		expect(groupSelectionState(aiTools, ['mcp__ai-tools__generateImage'], OPEN)).toBe('all');
	});

	it('is none for a group nobody can pick from', () => {
		expect(groupSelectionState(computerUse, ['mcp__computer-use__screenshot'], LOCKED)).toBe('none');
	});
});

describe('pattern vs names on save', () => {
	it('gives only MCP server groups a pattern', () => {
		expect(groupPattern(github)).toBe('mcp__github__*');
		expect(groupPattern(harness)).toBeNull();
		expect(groupPattern(aiTools)).toBeNull();
		expect(groupPattern(computerUse)).toBeNull();
	});

	it('saves the pattern when a whole MCP server is selected', () => {
		expect(toggleGroupSelection([], github, OPEN)).toEqual(['mcp__github__*']);
	});

	it('saves names when a whole non-MCP group is selected', () => {
		expect(toggleGroupSelection([], harness, OPEN)).toEqual(['Read', 'Edit', 'Bash']);
	});

	it('never writes an unavailable tool into the list', () => {
		expect(toggleGroupSelection([], aiTools, OPEN)).toEqual(['mcp__ai-tools__generateImage']);
		expect(toggleGroupSelection([], computerUse, LOCKED)).toEqual([]);
		expect(toggleGroupSelection([], appConfig, LOCKED)).toEqual([]);
		expect(toggleToolSelection([], computerUse, 'mcp__computer-use__screenshot', LOCKED)).toEqual([]);
	});

	it('clears both shapes when a selected group is toggled off', () => {
		expect(toggleGroupSelection(['mcp__github__*', 'Read'], github, OPEN)).toEqual(['Read']);
		expect(
			toggleGroupSelection(
				['mcp__github__create_issue', 'mcp__github__list_issues', 'mcp__github__close_issue'],
				github,
				OPEN
			)
		).toEqual([]);
	});

	it('expands the pattern to names when one tool is unchecked', () => {
		const next = toggleToolSelection(['mcp__github__*'], github, 'mcp__github__close_issue', OPEN);
		expect(next).toEqual(['mcp__github__create_issue', 'mcp__github__list_issues']);
		expect(next).not.toContain('mcp__github__*');
	});

	it('collapses names back to the pattern once the group is whole again', () => {
		const partial = ['mcp__github__create_issue', 'mcp__github__list_issues'];
		expect(toggleToolSelection(partial, github, 'mcp__github__close_issue', OPEN)).toEqual([
			'mcp__github__*'
		]);
	});

	it('leaves other groups alone while one is edited', () => {
		const next = toggleToolSelection(['Read', 'mcp__github__*'], github, 'mcp__github__list_issues', OPEN);
		expect(next).toContain('Read');
		expect(next).toEqual(['Read', 'mcp__github__create_issue', 'mcp__github__close_issue']);
	});

	it('adds and removes a single non-MCP tool', () => {
		expect(toggleToolSelection([], harness, 'Bash', OPEN)).toEqual(['Bash']);
		expect(toggleToolSelection(['Read', 'Bash'], harness, 'Bash', OPEN)).toEqual(['Read']);
	});
});

describe('entry matching', () => {
	it('matches exact names and `*` patterns only', () => {
		expect(matchesToolEntry('Read', 'Read')).toBe(true);
		expect(matchesToolEntry('Read', 'ReadFile')).toBe(false);
		expect(matchesToolEntry('mcp__github__*', 'mcp__github__list_issues')).toBe(true);
		expect(matchesToolEntry('mcp__github__*', 'mcp__gitlab__list_issues')).toBe(false);
		expect(matchesToolEntry('*', 'anything')).toBe(true);
	});
});

describe('counts', () => {
	const groups = [harness, aiTools, github];
	// 3 harness + 1 available AI tool + 3 github = 7 selectable rows.

	it('counts the whole surface in all mode and nothing in none mode', () => {
		expect(countAllowedTools('all', groups, [], OPEN)).toBe(7);
		expect(countAllowedTools('none', groups, [], OPEN)).toBe(0);
	});

	it('counts what an allow list actually matches, patterns included', () => {
		expect(countAllowedTools('allow', groups, ['Read', 'mcp__github__*'], OPEN)).toBe(4);
		expect(countAllowedTools('allow', groups, [], OPEN)).toBe(0);
	});

	it('counts the remainder for a disallow list', () => {
		expect(countAllowedTools('disallow', groups, ['Bash'], OPEN)).toBe(6);
		expect(countAllowedTools('disallow', groups, ['mcp__github__*'], OPEN)).toBe(4);
	});

	it('drops a blocked group from the denominator', () => {
		expect(countAllowedTools('all', [harness, computerUse], [], LOCKED)).toBe(3);
		expect(countAllowedTools('all', [harness, computerUse], [], OPEN)).toBe(4);
	});
});

describe('profileToolCount', () => {
	const surface = { groups: [harness, github], discovered_at: '', errors: [] };

	it('reads the saved mode, not the shape of the list', () => {
		expect(profileToolCount({ tool_selection_mode: 'all', allowed_tools: [] }, surface, true)).toBe(6);
		expect(profileToolCount({ tool_selection_mode: 'none' }, surface, true)).toBe(0);
		expect(
			profileToolCount(
				{ tool_selection_mode: 'allow', allowed_tools: ['Read', 'mcp__github__*'] },
				surface,
				true
			)
		).toBe(4);
		expect(
			profileToolCount({ tool_selection_mode: 'disallow', disallowed_tools: ['Bash'] }, surface, true)
		).toBe(5);
	});

	it('still reads a pre-migration row correctly', () => {
		expect(profileToolCount({ allowed_tools: [] }, surface, true)).toBe(0);
		expect(profileToolCount({ allowed_tools: ['Read'] }, surface, true)).toBe(1);
		expect(profileToolCount({}, surface, true)).toBe(6);
	});
});
