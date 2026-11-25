import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/svelte';
import { tick } from 'svelte';
import { page } from '$app/state';
import { writable, type Writable } from 'svelte/store';
vi.mock('$app/navigation', () => ({ pushState: vi.fn(), goto: vi.fn() }));
vi.mock('$lib/services/watch-resources', () => ({ watchResources: vi.fn() }));
vi.mock('$lib/stores/app-data.svelte', async () => {
 const { writable } = await import('svelte/store');
 return { projects: writable([]), appData: { deleteProject: vi.fn(), updateProject: vi.fn() } };
});
vi.mock('$lib/stores/projects-overview', async () => {
 const { writable } = await import('svelte/store');
 return { projectsOverview: writable([]), projectsOverviewSupported: writable(false), loadProjectsOverview: vi.fn().mockResolvedValue(undefined), patchOverviewWorker: vi.fn() };
});
vi.mock('$lib/stores/worker-presence', async () => {
 const { writable } = await import('svelte/store');
 return { workerInstalls: writable(null), refreshWorkerPresence: vi.fn().mockResolvedValue([]) };
});
vi.mock('$lib/stores/layout.svelte', async () => {
 const { writable } = await import('svelte/store');
 return { activeView: writable({ type: 'project' }), layout: { current: { sidebarOverlay: true, sidebarWidth: 320 }, setSidebarCollapsed: vi.fn() } };
});
vi.mock('$lib/stores/auth.svelte', async () => ({ isAdmin: (await import('svelte/store')).writable(true) }));
vi.mock('$lib/api/git', () => ({ getGitStatus: vi.fn().mockResolvedValue({ is_git_repo: true }) }));
vi.mock('$lib/components/git/InitRepoModal.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/stores/groups.svelte', async () => {
 const { writable } = await import('svelte/store');
 return { groups: Object.assign(writable({ projects: { groups: [], assignments: {} } }), { createGroup: vi.fn(), assignToGroup: vi.fn(), removeFromGroup: vi.fn(), deleteGroup: vi.fn() }) };
});
// A stub that records its props, so a test can read the worker table's project and answer for it.
vi.mock('./ProjectWorkersTab.svelte', async () => {
 const Empty = (await import('$lib/test-stubs/EmptyRouteDependency.svelte')).default as unknown as (...args: unknown[]) => unknown;
 return { default: vi.fn((...args: unknown[]) => Empty(...args)) };
});
vi.mock('./ProjectMcpTab.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('./ProjectAgentsTab.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('./ProjectHooksTab.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('./ProjectMemoryTab.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/api/project-workers', async (importOriginal) => ({ ...(await importOriginal<typeof import('$lib/api/project-workers')>()), getProjectRemovalPreview: vi.fn(), changeProjectRepository: vi.fn() }));
vi.mock('$lib/components/AddProjectModal.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
import ProjectCard from './ProjectCard.svelte';
import ProjectWorkersTab from './ProjectWorkersTab.svelte';
import { appData, projects } from '$lib/stores/app-data.svelte';
import { activeView } from '$lib/stores/layout.svelte';
import { platform } from '$lib/stores/platform.svelte';
import { projectsOverview, projectsOverviewSupported } from '$lib/stores/projects-overview';
const list = projects as unknown as Writable<any[]>;
const view = activeView as unknown as Writable<any>;
const overview = projectsOverview as Writable<any[]>;
const supported = projectsOverviewSupported as Writable<boolean>;
beforeEach(() => {
 platform.set('desktop'); page.state = {};
 vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
 vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
 Object.defineProperty(window, 'innerWidth', { value: 1440, configurable: true });
 list.set([{ id: 'one', name: 'One', path: '/one', session_count: 2 }, { id: 'two', name: 'Two', path: '/two' }]);
 view.set({ type: 'project' }); overview.set([]); supported.set(false);
});
afterEach(() => { cleanup(); platform.set('desktop'); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
describe('Projects master/detail', () => {
 it('has no raw path box, puts the one danger zone in Project settings and offers New chat', async () => {
  render(ProjectCard);
  await screen.findByRole('heading', { level: 2, name: 'One' });
  expect(screen.queryByPlaceholderText('Project path')).toBeNull();
  expect(screen.queryByRole('heading', { name: 'Danger zone' })).toBeNull();
  expect(screen.getByRole('button', { name: 'New chat' })).toBeTruthy();
  await fireEvent.click(screen.getByRole('button', { name: 'Project settings' }));
  const drawer = screen.getByRole('dialog', { name: 'Project settings' });
  expect(within(drawer).getAllByRole('heading', { name: 'Danger zone' })).toHaveLength(1);
  expect(within(drawer).queryByPlaceholderText('Project path')).toBeNull();
  const removal = within(drawer).getByText('Files stay on disk.', { exact: false }).closest('.st-row');
  expect(removal?.classList.contains('st-wrap-hint')).toBe(true);
 });
 it('discards A’s dirty name when switching to B, but preserves same-project refreshes', async () => {
  render(ProjectCard);
  await fireEvent.click(await screen.findByRole('button', { name: 'Project settings' }));
  const name = screen.getByPlaceholderText('Project name');
  await fireEvent.input(name, { target: { value: 'Unsaved A' } });
  list.set([{ id: 'one', name: 'Refreshed A', path: '/refreshed-a' }, { id: 'two', name: 'Two', path: '/two' }]);
  await tick();
  expect(screen.getByDisplayValue('Unsaved A')).toBeTruthy();
  await fireEvent.click(screen.getByRole('button', { name: 'Open Two' }));
  expect(screen.queryByRole('dialog', { name: 'Project settings' })).toBeNull();
  await fireEvent.click(screen.getByRole('button', { name: 'Project settings' }));
  expect((screen.getByPlaceholderText('Project name') as HTMLInputElement).value).toBe('Two');
  await fireEvent.input(screen.getByPlaceholderText('Project name'), { target: { value: 'Saved B' } });
  await fireEvent.keyDown(screen.getByPlaceholderText('Project name'), { key: 'Enter' });
  expect(appData.updateProject).toHaveBeenCalledWith('two', { name: 'Saved B' });
  expect(appData.updateProject).not.toHaveBeenCalledWith('two', { name: 'Unsaved A' });
 });
 it('opens the first project on desktop and keeps the searchable side list mounted', async () => {
  render(ProjectCard);
  await screen.findByRole('heading', { level: 2, name: 'One' });
  expect(screen.getByRole('heading', { level: 1, name: 'Projects' })).toBeTruthy();
  expect(screen.getByRole('button', { name: 'New project' }).title).toBe('New project');
  await fireEvent.click(screen.getByRole('button', { name: 'Open Two' }));
  expect(screen.getByRole('heading', { level: 2, name: 'Two' })).toBeTruthy();
  await fireEvent.input(screen.getByRole('searchbox', { name: 'Search projects' }), { target: { value: 'One' } });
  expect(screen.queryByRole('button', { name: 'Open Two' })).toBeNull();
  expect(screen.getByRole('heading', { level: 2, name: 'Two' })).toBeTruthy();
 });
 it.each(['narrow browser', 'phone app'])('starts with the list and uses shared back navigation on %s', async mode => {
  if (mode === 'phone app') platform.set('mobile');
  else Object.defineProperty(window, 'innerWidth', { value: 390, configurable: true });
  const historyBack = vi.spyOn(history, 'back').mockImplementation(() => {});
  const result = render(ProjectCard); await tick();
  expect(result.container.querySelector('[data-pane="panel"]')).toBeTruthy();
  await fireEvent.click(screen.getByRole('button', { name: 'Open Two' }));
  expect(result.container.querySelector('[data-pane="detail"]')).toBeTruthy();
  await fireEvent.click(screen.getByRole('button', { name: 'Back to Projects' }));
  expect(historyBack).toHaveBeenCalledOnce();
 });
 it('honours a deep link after project data arrives, including on phone', async () => {
  platform.set('mobile'); list.set([]); view.set({ type: 'project', itemId: 'two' });
  render(ProjectCard); await tick();
  list.set([{ id: 'two', name: 'Two', path: '/two' }]);
  expect(await screen.findByRole('heading', { level: 2, name: 'Two' })).toBeTruthy();
 });
 it('opens remote projects on Workers, says how to set them up here, and keeps here/all filtering', async () => {
  supported.set(true);
  overview.set([{ project_v2_id: 'remote', local_project_id: null, name: 'Remote', kind: 'git', remote: 'github.com/example/remote', workers: [
   { install_id: 'self', device_name: 'Here', is_self: true, online: true, state: 'missing' },
   { install_id: 'peer', device_name: 'Peer', is_self: false, online: true, state: 'ready' }
  ] }]);
  render(ProjectCard);
  expect(await screen.findByRole('heading', { level: 2, name: 'Remote' })).toBeTruthy();
  expect(screen.getByRole('tab', { name: /^Workers/ }).getAttribute('aria-selected')).toBe('true');
  expect(screen.getByRole('tab', { name: /^Workers/ }).textContent).toContain('1 missing');
  expect(screen.queryByRole('tab', { name: 'General' })).toBeNull();
  // The Workers tab says it once (ProjectWorkersTab.test); Resources offers the setup too.
  await fireEvent.click(screen.getByRole('tab', { name: 'Resources' }));
  expect(screen.getByText(/Remote isn't on Here yet/)).toBeTruthy();
  await fireEvent.click(screen.getByRole('button', { name: 'Set up here' }));
  expect(screen.getByRole('tab', { name: /^Workers/ }).getAttribute('aria-selected')).toBe('true');
  // The chat starts where the project is: Peer.
  expect(screen.getByRole('button', { name: 'New chat on Peer' })).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Repository github.com/example/remote. Change it on all workers' })).toBeTruthy();
  expect(screen.queryByRole('heading', { name: 'Danger zone' })).toBeNull();
  await fireEvent.click(screen.getByRole('button', { name: 'On Here' }));
  expect(screen.queryByRole('button', { name: 'Open Remote (not on Here)' })).toBeNull();
  await fireEvent.click(screen.getByRole('button', { name: 'All workers' }));
  expect(screen.getByRole('button', { name: 'Open Remote (not on Here)' })).toBeTruthy();
 });
 it('offers Change repository for a Git project whose repository is not reported', async () => {
  supported.set(true);
  overview.set([{ project_v2_id: 'bare', local_project_id: null, name: 'Bare', kind: 'git', remote: null, workers: [
   { install_id: 'peer', device_name: 'Peer', is_self: false, online: true, state: 'ready' }
  ] }]);
  render(ProjectCard);
  const chip = await screen.findByRole('button', { name: 'Repository not reported. Set it on all workers' });
  expect(chip.textContent).toContain('Repository not reported');
  await fireEvent.click(chip);
  const dialog = screen.getByRole('dialog', { name: 'Change repository on all workers' });
  expect(dialog.textContent).toContain('None');
 });
 it('shows a local project the overview has not listed in the worker table by its local id', async () => {
  supported.set(true);
  overview.set([]);
  const workersTab = vi.mocked(ProjectWorkersTab as unknown as (...args: unknown[]) => unknown);
  workersTab.mockClear();
  render(ProjectCard);
  await screen.findByRole('heading', { level: 2, name: 'One' });
  expect(screen.getByRole('tab', { name: /^Workers/ })).toBeTruthy();
  const props = workersTab.mock.calls.at(-1)![1] as { projectRef: unknown };
  expect(props.projectRef).toEqual({ project_id: 'one' });
  // The one folder editor is the worker table's: Settings sends folder changes there.
  await fireEvent.click(screen.getByRole('button', { name: 'Project settings' }));
  expect(within(screen.getByRole('dialog', { name: 'Project settings' })).queryByRole('button', { name: 'Change folder' })).toBeNull();
 });
 it('changes the folder in Project settings when this worker has no worker list for a local project', async () => {
  supported.set(true);
  overview.set([]);
  const workersTab = vi.mocked(ProjectWorkersTab as unknown as (...args: unknown[]) => unknown);
  workersTab.mockClear();
  render(ProjectCard);
  await screen.findByRole('heading', { level: 2, name: 'One' });
  const props = workersTab.mock.calls.at(-1)![1] as { onUnavailable?: () => void };
  props.onUnavailable?.();
  await tick();
  expect(screen.queryByRole('tab', { name: /^Workers/ })).toBeNull();
  await fireEvent.click(screen.getByRole('button', { name: 'Project settings' }));
  const drawer = screen.getByRole('dialog', { name: 'Project settings' });
  await fireEvent.click(within(drawer).getByRole('button', { name: 'Change folder' }));
  await fireEvent.input(within(drawer).getByLabelText(/^New folder on/), { target: { value: '/workspace/example/one-moved' } });
  await fireEvent.click(within(drawer).getByRole('button', { name: 'Save folder' }));
  expect(appData.updateProject).toHaveBeenCalledWith('one', { path: '/workspace/example/one-moved' });
 });
 it('changes the folder in Project settings when the overview is unavailable', async () => {
  render(ProjectCard);
  await screen.findByRole('heading', { level: 2, name: 'One' });
  await fireEvent.click(screen.getByRole('button', { name: 'Project settings' }));
  expect(within(screen.getByRole('dialog', { name: 'Project settings' })).getByRole('button', { name: 'Change folder' })).toBeTruthy();
 });
 it('opens Change repository as one dialog for every worker', async () => {
  supported.set(true);
  overview.set([{ project_v2_id: 'remote', local_project_id: null, name: 'Remote', kind: 'git', remote: 'github.com/example/remote', workers: [
   { install_id: 'peer', device_name: 'Peer', is_self: false, online: true, state: 'ready' }
  ] }]);
  render(ProjectCard);
  await fireEvent.click(await screen.findByRole('button', { name: /^Repository github.com\/example\/remote/ }));
  expect(screen.getByRole('dialog', { name: 'Change repository on all workers' })).toBeTruthy();
 });
 it('shows the filled New project action when empty', async () => {
  list.set([]); render(ProjectCard); await tick();
  expect(screen.getByRole('heading', { level: 1, name: 'Projects' })).toBeTruthy();
  expect(screen.getAllByRole('button', { name: 'New project' }).some(b => b.classList.contains('primary'))).toBe(true);
 });
});
