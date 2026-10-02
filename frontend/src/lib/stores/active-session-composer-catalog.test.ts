import { beforeEach, expect, it, vi } from 'vitest';
const mocks = vi.hoisted(() => ({
 adapters: new Map<string, { apply(value: unknown): void }>(),
 catalog: { projects: [] as {id: string}[], clusterProjects: [] as Record<string, unknown>[], profiles: [], sessions: [] },
 catalogLoaded: false,
}));
vi.mock('$lib/services/preferences', () => ({ preferenceSync: {
 register: (key: string, adapter: { apply(value: unknown): void }) => mocks.adapters.set(key, adapter),
 save: vi.fn(), reconnect: vi.fn(async () => {}),
} }));
vi.mock('./app-data.svelte', () => ({ appData: { get current() { return mocks.catalog; }, get projectCatalogLoaded() { return mocks.catalogLoaded; } } }));
vi.mock('./session/ws-connection', () => ({ createWsConnection: () => ({
 connect: vi.fn(), disconnect: vi.fn(), send: vi.fn(), isConnected: () => false,
 resetLastMessageTime: vi.fn(), resetReconnectAttempts: vi.fn(), destroy: vi.fn(),
}) }));
let active: (typeof import('./active-session'))['activeSession'];
const linked = { project_v2_id: 'repo', local_project_id: 'local-clone', remote_only: false };
beforeEach(async () => {
 vi.resetModules(); localStorage.clear(); sessionStorage.clear(); mocks.adapters.clear();
 mocks.catalog.projects = []; mocks.catalog.clusterProjects = []; mocks.catalogLoaded = false;
 active = (await import('./active-session')).activeSession;
 Object.assign(active.current, { mode: 'code', sessionId: null, draft: '', worktreeBranch: '' });
});
async function receiveDefaults(projectId = 'foreign-clone') {
 mocks.adapters.get('composer.project')!.apply({ projectId, projectV2Id: 'repo' });
 mocks.adapters.get('composer.worktreeIntent')!.apply('worktree');
 mocks.adapters.get('composer.checkout')!.apply({ kind: 'branch', projectId, projectV2Id: 'repo',
  branch: 'topic/remote', baseBranch: 'develop', createNew: true });
 mocks.adapters.get('composer.model')!.apply('sonnet');
 mocks.adapters.get('composer.effort')!.apply('max');
 await Promise.resolve();
}
it.each(['local-first', 'cluster-first'])('defers a branch until both catalog halves resolve the local project (%s)', async order => {
 await receiveDefaults();
 expect(active.current).toMatchObject({ project: '', projectV2Id: 'repo', worktreeBranch: '', modelOverride: 'sonnet', effortLevelOverride: 'max' });
 if (order === 'local-first') mocks.catalog.projects = [{ id: 'local-clone' }];
 else mocks.catalog.clusterProjects = [linked];
 active.applyComposerDefaults();
 expect(active.current.worktreeBranch).toBe('');
 mocks.catalog.projects = [{ id: 'local-clone' }]; mocks.catalog.clusterProjects = [linked];
 active.applyComposerDefaults();
 expect(active.current).toMatchObject({ project: 'local-clone', projectV2Id: 'repo',
  worktreeBranch: 'topic/remote', worktreeBaseBranch: 'develop', worktreeCreateNewBranch: true,
  modelOverride: 'sonnet', effortLevelOverride: 'max' });
});
it('restores a known remote-only project branch without requiring a local clone', async () => {
 await receiveDefaults('');
 expect(active.current.worktreeBranch).toBe('');
 mocks.catalog.clusterProjects = [{ project_v2_id: 'repo', local_project_id: null, remote_only: true }];
 active.applyComposerDefaults();
 expect(active.current).toMatchObject({ project: '', projectV2Id: 'repo', worktreeBranch: 'topic/remote' });
});
it.each(['draft', 'branch'])('preserves a local %s staged while catalogs are loading', async kind => {
 await receiveDefaults();
 if (kind === 'draft') active.setDraft('Do not reroute this draft');
 else active.setWorktreeBranch('topic/mine');
 mocks.catalog.projects = [{ id: 'local-clone' }]; mocks.catalog.clusterProjects = [linked];
 active.applyComposerDefaults();
 expect(active.current.project).toBe('');
 expect(kind === 'draft' ? active.current.draft : active.current.worktreeBranch)
  .toBe(kind === 'draft' ? 'Do not reroute this draft' : 'topic/mine');
});
// #1636 review: a send held for the catalog must leave on the remembered
// branch, not an auto-named one, even when text was typed while it loaded.
it('finishes the remembered project and branch under a draft typed while the catalog loaded', async () => {
 await receiveDefaults();
 expect(active.composerAwaitingProjectCatalog).toBe(true);
 active.setDraft('typed before the catalog answered');
 mocks.catalog.projects = [{ id: 'local-clone' }]; mocks.catalog.clusterProjects = [linked]; mocks.catalogLoaded = true;
 expect(active.composerAwaitingProjectCatalog).toBe(false);
 active.resolveComposerDefaultsAfterCatalog();
 expect(active.current).toMatchObject({ project: 'local-clone', projectV2Id: 'repo', worktreeBranch: 'topic/remote',
  worktreeBaseBranch: 'develop', draft: 'typed before the catalog answered' });
 // Idempotent: a second call (the held send and startup both ask) changes nothing.
 active.resolveComposerDefaultsAfterCatalog();
 expect(active.current).toMatchObject({ project: 'local-clone', worktreeBranch: 'topic/remote' });
});
it('keeps a branch the user staged while the catalog loaded', async () => {
 await receiveDefaults();
 active.setDraft('mine'); active.setWorktreeBranch('topic/mine');
 expect(active.composerAwaitingProjectCatalog).toBe(false);
 mocks.catalog.projects = [{ id: 'local-clone' }]; mocks.catalog.clusterProjects = [linked]; mocks.catalogLoaded = true;
 active.resolveComposerDefaultsAfterCatalog();
 expect(active.current).toMatchObject({ project: '', worktreeBranch: 'topic/mine' });
});
it('does not hold sends once the catalog has answered', async () => {
 mocks.catalog.projects = [{ id: 'local-clone' }]; mocks.catalog.clusterProjects = [linked]; mocks.catalogLoaded = true;
 await receiveDefaults();
 expect(active.composerAwaitingProjectCatalog).toBe(false);
 expect(active.current).toMatchObject({ project: 'local-clone', worktreeBranch: 'topic/remote' });
});
