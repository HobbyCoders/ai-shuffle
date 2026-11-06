import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/svelte';
import { writable } from 'svelte/store';
const actions = vi.hoisted(() => ({ load: vi.fn(), save: vi.fn(), remove: vi.fn(), toggle: vi.fn(), test: vi.fn(), preview: vi.fn() }));
vi.mock('$lib/services/watch-resources', () => ({ watchResources: vi.fn() }));
vi.mock('$lib/stores/projectAgents.svelte', async () => {
 const { writable } = await import('svelte/store');
 return { projectAgents: { loadAgents: actions.load, deleteAgent: actions.remove, clearError: vi.fn() }, allProjectAgents: writable([]), projectAgentsLoading: writable(false), projectAgentsError: writable(null), projectAgentCount: writable(0) };
});
vi.mock('$lib/stores/projectMemory.svelte', async () => {
 const { writable } = await import('svelte/store');
 return { projectMemory: { loadFiles: actions.load, saveFile: actions.save, deleteFile: actions.remove, clearError: vi.fn() }, allMemoryFiles: writable([]), memoryLoading: writable(false), memorySaving: writable(false), memoryError: writable(null) };
});
vi.mock('$lib/api/projectMemory', () => ({ previewMemoryImport: actions.preview, applyMemoryImport: vi.fn() }));
vi.mock('$lib/stores/hooks.svelte', async () => {
 const { writable } = await import('svelte/store');
 return { hooks: { loadAll: actions.load, updateHook: actions.toggle, deleteHook: actions.remove, testHook: actions.test, clearError: vi.fn() }, projectHooks: writable([]), pluginHooks: writable([]), hooksLoading: writable(false), hooksError: writable(null), hookTesting: writable({}), hookSaving: writable({}), hookStats: writable({ totalCustom: 1, totalPlugin: 0 }) };
});
import ProjectAgentsTab from './ProjectAgentsTab.svelte';
import ProjectHooksTab from './ProjectHooksTab.svelte';
import ProjectMemoryTab from './ProjectMemoryTab.svelte';
import { allProjectAgents, projectAgentsLoading, projectAgentsError } from '$lib/stores/projectAgents.svelte';
import { allMemoryFiles } from '$lib/stores/projectMemory.svelte';
import { projectHooks } from '$lib/stores/hooks.svelte';
const set = (store: any, value: any) => store.set(value);
beforeEach(() => { vi.clearAllMocks(); actions.load.mockResolvedValue(undefined); set(projectAgentsLoading, false); set(projectAgentsError, null); });
afterEach(cleanup);
describe('Project tab row grammar', () => {
 it('renders agent rows and preserves the inline delete confirmation', async () => {
  set(allProjectAgents, [{ name: 'Reviewer', description: 'Review changes', model: null, tools: ['Read'] }]);
  render(ProjectAgentsTab, { projectId: 'p' });
  expect(screen.getByRole('heading', { name: /Agents/ })).toBeTruthy();
  await fireEvent.click(screen.getByRole('button', { name: 'Delete agent' }));
  expect(screen.getByText('Delete “Reviewer”?')).toBeTruthy();
  await fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
  expect(actions.remove).toHaveBeenCalledWith('p', 'Reviewer');
 });
 it('shows loading and error states for agent loads', async () => {
  set(projectAgentsLoading, true); const result = render(ProjectAgentsTab, { projectId: 'p' });
  expect(screen.getByRole('status').textContent).toContain('Loading agents');
  set(projectAgentsLoading, false); set(projectAgentsError, 'Cannot read agents');
  expect(await screen.findByText('Cannot read agents')).toBeTruthy();
 });
 it('keeps hook switches, test and delete reachable in Settings rows', async () => {
  set(projectHooks, [{ id: 'h', name: 'Check', event: 'PostToolUse', hook_type: 'bash', command: 'true', enabled: true, failure_count: 0 }]);
  actions.test.mockResolvedValue({ success: true, duration_ms: 2 });
  render(ProjectHooksTab, { projectId: 'p' });
  await fireEvent.click(screen.getByRole('switch', { name: 'Disable hook' }));
  expect(actions.toggle).toHaveBeenCalledWith('h', { enabled: false });
  await fireEvent.click(screen.getByRole('button', { name: 'Test hook' }));
  expect(await screen.findByText('Test passed (2ms)')).toBeTruthy();
  await fireEvent.click(screen.getByRole('button', { name: 'Delete hook' }));
  expect(actions.remove).toHaveBeenCalledWith('h');
 });
 it('creates memory through the editor, saves, and previews provider import', async () => {
  set(allMemoryFiles, [{ location: 'project', exists: false, content: '', file_path: 'AGENTS.md', size: 0, updated_at: null }]);
  actions.save.mockResolvedValue(undefined); actions.preview.mockResolvedValue({ ready_count: 0, conflict_count: 0, source_dir: null, items: [] });
  render(ProjectMemoryTab, { projectId: 'p' });
  await fireEvent.click(screen.getByRole('button', { name: 'Create' }));
  await fireEvent.input(screen.getByRole('textbox', { name: 'Edit Project memory' }), { target: { value: 'Keep tests focused.' } });
  await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  expect(actions.save).toHaveBeenCalledWith('p', 'project', 'Keep tests focused.');
  await fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
  expect(await screen.findByText('Nothing found for this provider.')).toBeTruthy();
  expect(actions.preview).toHaveBeenCalledWith('p', 'claude');
 });
});
