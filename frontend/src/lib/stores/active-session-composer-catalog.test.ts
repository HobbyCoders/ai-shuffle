import { beforeEach, expect, it, vi } from 'vitest';
const mocks = vi.hoisted(() => ({
 adapters: new Map<string, { apply(value: unknown): void }>(),
 prefixes: new Map<string, any>(),
 catalog: { projects: [] as {id: string}[], clusterProjects: [] as Record<string, unknown>[], profiles: [], sessions: [] },
 catalogLoaded: false,
}));
vi.mock('$lib/services/preferences', () => ({ preferenceSync: {
 register: (key: string, adapter: { apply(value: unknown): void }) => mocks.adapters.set(key, adapter),
 save: vi.fn(), registerPrefix: (key:string, factory:any) => mocks.prefixes.set(key,factory), reconnect: vi.fn(async () => {}),
} }));
vi.mock('./app-data.svelte', () => ({ appData: { get current() { return mocks.catalog; }, get projectCatalogLoaded() { return mocks.catalogLoaded; } } }));
vi.mock('./git.svelte', () => ({git:{get current() { return {projectId:'local-clone', hydrated:true, stale:false, status:{is_git_repo:true}}; }, subscribe:vi.fn()}}));
vi.mock('./session/ws-connection', () => ({ createWsConnection: () => ({
 connect: vi.fn(), disconnect: vi.fn(), send: vi.fn(), isConnected: () => false,
 resetLastMessageTime: vi.fn(), resetReconnectAttempts: vi.fn(), destroy: vi.fn(),
}) }));
let active: (typeof import('./active-session'))['activeSession'];
const linked = { project_v2_id: 'repo', local_project_id: 'local-clone', remote_only: false, origin:'https://example.com/repo' };
beforeEach(async () => {
 vi.resetModules(); localStorage.clear(); sessionStorage.clear(); mocks.adapters.clear();
 mocks.catalog.projects = []; mocks.catalog.clusterProjects = []; mocks.catalogLoaded = false;
 active = (await import('./active-session')).activeSession;
 Object.assign(active.current, { mode: 'code', sessionId: null, draft: '', worktreeBranch: '' });
});
async function receiveDefaults(projectId = 'foreign-clone') {
 mocks.adapters.get('composer.project')!.apply({ projectId, projectV2Id: 'repo' });
 mocks.prefixes.get('composer.projectWorktree.')('composer.projectWorktree.shared%3Arepo').apply({intent:'worktree', checkout:{ kind: 'branch', projectId, projectV2Id: 'repo',
  branch: 'topic/remote', baseBranch: 'develop', createNew: true }});
 mocks.adapters.get('composer.model')!.apply('sonnet');
 mocks.adapters.get('composer.modelEfforts')!.apply({sonnet:'max'});
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
 mocks.catalog.clusterProjects = [{ project_v2_id: 'repo', local_project_id: null, remote_only: true, origin:'https://example.com/repo' }];
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
// #434: a new session whose project was deleted (here, or on another tab or
// worker) must not keep sending into it, even under a typed draft.
it.each([
 ['local', { projectId: 'local-clone', projectV2Id: 'repo' }],
 ['remote-only', { projectId: '', projectV2Id: 'repo' }],
])('drops a deleted %s project from a new session', async (_kind, pick) => {
 mocks.catalog.projects = [{ id: 'local-clone' }]; mocks.catalog.clusterProjects = [linked]; mocks.catalogLoaded = true;
 active.setProject(pick.projectId, pick.projectV2Id);
 active.setDraft('typed for the old project');
 expect(active.current).toMatchObject({ project: pick.projectId, projectV2Id: 'repo' });

 mocks.catalog.projects = []; mocks.catalog.clusterProjects = [];
 active.dropMissingProject();

 expect(active.current).toMatchObject({ project: '', projectV2Id: null, draft: 'typed for the old project' });
});
it('keeps a project that is still listed, and waits for the catalog before judging', async () => {
 mocks.catalog.projects = [{ id: 'local-clone' }]; mocks.catalog.clusterProjects = [linked]; mocks.catalogLoaded = true;
 active.setProject('local-clone', 'repo');
 active.dropMissingProject();
 expect(active.current).toMatchObject({ project: 'local-clone', projectV2Id: 'repo' });

 mocks.catalog.projects = []; mocks.catalogLoaded = false;
 active.dropMissingProject();
 expect(active.current).toMatchObject({ project: 'local-clone', projectV2Id: 'repo' });
});
it('never unbinds an open chat whose project was deleted', async () => {
 mocks.catalog.projects = []; mocks.catalog.clusterProjects = []; mocks.catalogLoaded = true;
 Object.assign(active.current, { sessionId: 'open-chat', project: 'gone', projectV2Id: 'pv2-gone' });
 active.dropMissingProject();
 expect(active.current).toMatchObject({ sessionId: 'open-chat', project: 'gone', projectV2Id: 'pv2-gone' });
});
it('follows a proven local catalog identity replacement without losing the branch', async () => {
 const previous = { ...linked, project_v2_id: 'local-identity', origin: 'local:local-clone' };
 mocks.catalog.projects = [{ id: 'local-clone' }]; mocks.catalog.clusterProjects = [previous]; mocks.catalogLoaded = true;
 active.setProject('local-clone', 'local-identity');
 const {composerPreferences:p} = await import('./composer-preferences');
 p.rememberWorktreeIntent({projectId:'local-clone', projectV2Id:'local-identity'}, 'worktree');
 p.rememberCheckout({projectId:'local-clone', projectV2Id:'local-identity'}, {kind:'branch', projectId:'local-clone', projectV2Id:'local-identity', branch:'topic/keep', baseBranch:'main', createNew:true});
 active.applyComposerDefaults();
 active.setDraft('keep my draft');

 active.linkDiscoveredProjects([linked]); // Called before app-data replaces the previous catalog.
 mocks.catalog.clusterProjects = [linked];
 active.dropMissingProject();
 expect(active.current).toMatchObject({project:'local-clone', projectV2Id:'repo', draft:'keep my draft', worktreeBranch:'topic/keep'});
 expect(p.current.project).toEqual({projectId:'local-clone', projectV2Id:'repo'});
 expect(p.worktreeForProject({projectId:'local-clone', projectV2Id:'repo'}).checkout).toMatchObject({projectV2Id:'repo', branch:'topic/keep'});
});
// What live-sync does after a project catalog change: re-apply the defaults, then drop a gone project.
it('keeps an idle New Chat on the project across the identity replacement and the refresh after it', async () => {
 const previous = { ...linked, project_v2_id: 'local-identity', origin: 'local:local-clone' };
 mocks.catalog.projects = [{ id: 'local-clone' }]; mocks.catalog.clusterProjects = [previous]; mocks.catalogLoaded = true;
 active.setProject('local-clone', 'local-identity', { user: true });
 active.linkDiscoveredProjects([linked]);
 mocks.catalog.clusterProjects = [linked];
 active.applyComposerDefaults();
 active.dropMissingProject();
 expect(active.current).toMatchObject({ mode: 'code', project: 'local-clone', projectV2Id: 'repo' });
});
it.each(['unproven', 'still-listed'])('does not reroute a %s shared identity by a coincidentally matching local ID', async kind => {
 mocks.catalog.projects = [{ id: 'local-clone' }]; mocks.catalogLoaded = true;
 const previous = { ...linked, project_v2_id: 'old-identity' };
 mocks.catalog.clusterProjects = kind === 'unproven' ? [] : [previous];
 active.setProject('local-clone', 'old-identity');
 active.linkDiscoveredProjects(kind === 'still-listed' ? [previous, linked] : [linked]);
 const {composerPreferences:p} = await import('./composer-preferences');
 expect(p.current.project).toEqual({projectId:'local-clone', projectV2Id:'old-identity'});
 expect(active.current).toMatchObject({project:'local-clone', projectV2Id:'old-identity'});
});
it('keeps an open chat bound to its original identity when its project links', async () => {
 mocks.catalog.projects = [{ id: 'local-clone' }]; mocks.catalog.clusterProjects = [{ ...linked, project_v2_id:'local-identity' }]; mocks.catalogLoaded = true;
 active.setProject('local-clone', 'local-identity');
 Object.assign(active.current, { sessionId:'bound-chat' });
 active.linkDiscoveredProjects([linked]);
 expect(active.current).toMatchObject({sessionId:'bound-chat', project:'local-clone', projectV2Id:'local-identity'});
});
it('moves a New Chat compose parked behind an open chat, keeping its draft and branch', async () => {
 vi.stubGlobal('fetch', vi.fn(async () => new Response('{}'))); // New Chat persists in the background.
 const previous = { ...linked, project_v2_id: 'local-identity', origin: 'local:local-clone' };
 mocks.catalog.projects = [{ id: 'local-clone' }]; mocks.catalog.clusterProjects = [previous]; mocks.catalogLoaded = true;
 active.setProject('local-clone', 'local-identity', { user: true });
 active.setDraft('parked draft'); active.setWorktreeBranch('topic/parked');
 active.newChat(); // Caches the compose under the New Chat key.
 Object.assign(active.current, { sessionId: 'open-chat', draft: '' }); // Another chat on the same project is open.
 active.linkDiscoveredProjects([linked]);
 mocks.catalog.clusterProjects = [linked];
 expect(active.current).toMatchObject({ sessionId: 'open-chat', projectV2Id: 'local-identity' });
 active.newChat();
 const { sessionEphemeralCache } = await import('./session-cache');
 expect(sessionEphemeralCache.restore('open-chat')).toMatchObject({ project: 'local-clone', projectV2Id: 'local-identity' });
 active.applyComposerDefaults(); active.dropMissingProject();
 expect(active.current).toMatchObject({ sessionId: null, project: 'local-clone', projectV2Id: 'repo', draft: 'parked draft', worktreeBranch: 'topic/parked' });
 vi.unstubAllGlobals();
});
it('drops a deleted extra folder from a new session', async () => {
 mocks.catalog.projects = [{ id: 'local-clone' }, { id: 'extra' }]; mocks.catalog.clusterProjects = [linked]; mocks.catalogLoaded = true;
 active.setProject('local-clone', 'repo');
 Object.assign(active.current, { workspaceProjectIds: ['extra', 'gone'] });
 active.dropMissingProject();
 expect(active.current).toMatchObject({ project: 'local-clone', workspaceProjectIds: ['extra'] });
});
