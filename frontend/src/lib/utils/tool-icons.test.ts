import { describe, it, expect } from 'vitest';
import { displayToolName, getToolIconType } from './tool-icons';

describe('displayToolName', () => {
	it('strips the mcp__<server>__ prefix', () => {
		// The app's builtins are MCP-bridged on both legs, so the raw wire name
		// leaked into the card header as "mcp__builtins__TaskGet".
		expect(displayToolName('mcp__builtins__TaskGet')).toBe('TaskGet');
		expect(displayToolName('mcp__browser-tools__browser_navigate')).toBe('browser_navigate');
	});

	it('keeps a tool name that itself contains a double underscore', () => {
		expect(displayToolName('mcp__srv__a__b')).toBe('a__b');
	});

	it('drops the delegated leg mcp__fluid_bundled__ wrapper first', () => {
		// Claude Code reports every app tool through the one bundled server, so
		// a bridged builtin arrives double-wrapped.
		expect(displayToolName('mcp__fluid_bundled__mcp__builtins__Task')).toBe('Task');
		expect(displayToolName('mcp__fluid_bundled__Glob')).toBe('Glob');
		expect(displayToolName('mcp__fluid_bundled__mcp__ai-tools__generate_image')).toBe('generate_image');
		// Other MCP servers are unchanged.
		expect(displayToolName('mcp__browser__browser_navigate')).toBe('browser_navigate');
	});

	it('passes non-MCP names through untouched', () => {
		expect(displayToolName('Read')).toBe('Read');
		expect(displayToolName('')).toBe('');
	});
});

describe('getToolIconType', () => {
	it('resolves bare names directly', () => {
		expect(getToolIconType('Read')).toBe('file');
		expect(getToolIconType('Bash')).toBe('terminal');
	});

	it('resolves MCP-prefixed names on their bare suffix', () => {
		expect(getToolIconType('mcp__builtins__TaskOutput')).toBe('eye');
		expect(getToolIconType('mcp__builtins__Monitor')).toBe('eye');
	});

	it('gives delegated-leg bundled tools the same icon as on other legs', () => {
		expect(getToolIconType('mcp__fluid_bundled__mcp__builtins__Task')).toBe(getToolIconType('Task'));
		expect(getToolIconType('mcp__fluid_bundled__mcp__builtins__Monitor')).toBe('eye');
		expect(getToolIconType('mcp__fluid_bundled__Glob')).toBe(getToolIconType('Glob'));
		expect(getToolIconType('mcp__fluid_bundled__mcp__builtins__Task')).not.toBe('cog');
	});

	it('falls back to the cog for anything unknown', () => {
		expect(getToolIconType('SomethingNew')).toBe('cog');
	});
});
