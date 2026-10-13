import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import HookForm from './hooks/HookForm.svelte';
import McpServerForm from './mcp/McpServerForm.svelte';
import type { Hook } from '$lib/api/client';
import type { McpServerInfo } from '$lib/api/mcp';

vi.mock('$lib/stores/app-data.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { projects: writable([]) };
});
afterEach(cleanup);
const hook = { id: 'hook-1', name: 'Before command', event: 'PreToolUse', hook_type: 'bash', command: 'echo before', enabled: true, timeout: 30000 } as Hook;
const server = { name: 'docs', type: 'stdio', command: 'docs-server', scope: 'local', enabled: true } as McpServerInfo;

describe('mounted resource editor guards', () => {
	it('refreshes a clean hook in place and preserves a dirty buffer on change and deletion', async () => {
		const { component } = render(HookForm, { hook, onSave: vi.fn() });
		const input = screen.getByPlaceholderText('e.g., Slack Notification') as HTMLInputElement;
		expect(component.refreshRemote({ ...hook, name: 'Remote name' })).toBe(true);
		await tick();
		expect(input.value).toBe('Remote name');
		await fireEvent.input(input, { target: { value: 'Unsaved name' } });
		expect(component.refreshRemote({ ...hook, name: 'Changed again' })).toBe(false);
		expect(component.refreshRemote(undefined)).toBe(false);
		await tick();
		expect(input.value).toBe('Unsaved name');
		expect(screen.getByRole('status').textContent).toContain('deleted elsewhere');
	});

	it('does not replace hook fields during a save', async () => {
		const { component } = render(HookForm, { hook, onSave: vi.fn() });
		expect(component.refreshRemote({ ...hook, name: 'Remote name' }, true)).toBe(false);
		await tick();
		expect((screen.getByPlaceholderText('e.g., Slack Notification') as HTMLInputElement).value).toBe(hook.name);
	});

	it('guards MCP form prop refreshes and retains text when the remote server disappears', async () => {
		const { component, rerender } = render(McpServerForm, { show: true, server, scope: 'local', onSave: vi.fn(), onClose: vi.fn() });
		expect(component.canRefreshRemote()).toBe(true);
		await rerender({ server: { ...server, name: 'renamed' } });
		const input = screen.getByDisplayValue('renamed') as HTMLInputElement;
		await fireEvent.input(input, { target: { value: 'Local server name' } });
		expect(component.canRefreshRemote(true)).toBe(false);
		await tick();
		expect(input.value).toBe('Local server name');
		expect(screen.getByRole('status').textContent).toContain('deleted elsewhere');
	});
});
