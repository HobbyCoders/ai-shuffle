import {beforeEach, expect, it, vi} from 'vitest';
const mocks = vi.hoisted(() => ({
 git: {projectId:null as string | null, hydrated:true, stale:false, status:null as {is_git_repo:boolean} | null},
 gitListeners: new Set<(state:any)=>void>(),
 catalog: {projects:[{id:'a'},{id:'b'},{id:'notes'}], clusterProjects:[], profiles:[], sessions:[]},
}));
vi.mock('./git.svelte', () => ({git:{
 get current() { return mocks.git; },
 subscribe(listener:any) { mocks.gitListeners.add(listener); listener(mocks.git); return () => mocks.gitListeners.delete(listener); },
}}));
vi.mock('./app-data.svelte', () => ({appData:{get current() { return mocks.catalog; }, projectCatalogLoaded:true}}));
vi.mock('./session/ws-connection', () => ({createWsConnection: () => ({
 connect:vi.fn(), disconnect:vi.fn(), send:vi.fn(), isConnected:()=>false,
 resetLastMessageTime:vi.fn(), resetReconnectAttempts:vi.fn(), destroy:vi.fn(),
})}));
let active: (typeof import('./active-session'))['activeSession'];
function status(projectId:string, is_git_repo:boolean, stale=false) {
 mocks.git = {projectId, hydrated:true, stale, status:{is_git_repo}};
 for (const listener of mocks.gitListeners) listener(mocks.git);
}
beforeEach(async () => {
 vi.resetModules(); localStorage.clear(); sessionStorage.clear(); mocks.gitListeners.clear();
 mocks.git = {projectId:null, hydrated:true, stale:false, status:null};
 active = (await import('./active-session')).activeSession;
 Object.assign(active.current, {mode:'code', sessionId:null, project:'', projectV2Id:null, draft:'', worktreeBranch:'', messages:[]});
});
it('remembers A without enabling B, and re-reads branch/base/new-branch picks on project switches', () => {
 status('a',true); active.setProject('a'); active.setWorktreeIntent('worktree');
 active.setWorktreeBranch('topic/a'); active.setWorktreeBaseBranch('develop'); active.setWorktreeCreateNewBranch(true);
 status('b',true); active.setProject('b');
 expect(active.current).toMatchObject({project:'b',worktreeIntent:'local',worktreeBranch:'',worktreeBaseBranch:'main',worktreeCreateNewBranch:false});
 active.setWorktreeIntent('worktree'); active.setWorktreeBranch('topic/b');
 status('a',true); active.setProject('a');
 expect(active.current).toMatchObject({project:'a',worktreeIntent:'worktree',worktreeBranch:'topic/a',worktreeBaseBranch:'develop',worktreeCreateNewBranch:true});
 status('b',true); active.setProject('b');
 expect(active.current).toMatchObject({project:'b',worktreeBranch:'topic/b',worktreeBaseBranch:'main',worktreeCreateNewBranch:false});
});
it('restores project settings after reload and late Git status, including under a typed draft', async () => {
 status('a',true); active.setProject('a'); active.setWorktreeIntent('worktree');
 active.setWorktreeBranch('topic/a'); active.setWorktreeBaseBranch('develop');
 vi.resetModules(); mocks.gitListeners.clear(); mocks.git={projectId:null, hydrated:true, stale:false, status:null};
 active=(await import('./active-session')).activeSession;
 Object.assign(active.current, {mode:'code',sessionId:null,draft:'',worktreeBranch:''});
 active.applyComposerDefaults();
 expect(active.current).toMatchObject({project:'a',worktreeIntent:'local',worktreeBranch:''});
 active.setDraft('keep this draft');
 status('a',true);
 expect(active.current).toMatchObject({project:'a',worktreeIntent:'worktree',worktreeBranch:'topic/a',worktreeBaseBranch:'develop',draft:'keep this draft'});
});
it('never enables no-project, unknown, non-repo, or another project’s stale status', async () => {
 active.setWorktreeIntent('worktree'); expect(active.current.worktreeIntent).toBe('local');
 const {composerPreferences:p}=await import('./composer-preferences');
 p.rememberWorktreeIntent({projectId:'notes',projectV2Id:null},'worktree');
 status('a',true); active.setProject('notes');
 expect(active.current.worktreeIntent).toBe('local');
 status('notes',false); active.setWorktreeIntent('worktree');
 expect(active.current.worktreeIntent).toBe('local');
 status('notes',true,true); active.applyComposerDefaults();
 expect(active.current.worktreeIntent).toBe('worktree');
 status('notes',true); expect(active.current.worktreeIntent).toBe('worktree');
 status('notes',false); expect(active.current.worktreeIntent).toBe('local');
 active.setProject(''); expect(active.current.worktreeIntent).toBe('local');
});
it('uses canonical Git status for a linked project, never another checkout’s status', async () => {
 const {composerPreferences:p}=await import('./composer-preferences');
 p.rememberWorktreeIntent({projectId:'a',projectV2Id:'shared-a'},'worktree');
 active.setProject('a','shared-a');
 expect(active.current.worktreeIntent).toBe('local');
 status('shared-a',true);
 expect(active.current.worktreeIntent).toBe('worktree');
 status('shared-a',false);
 expect(active.current.worktreeIntent).toBe('local');
});
it('does not change an existing session when Git or account defaults change', async () => {
 status('a',true); active.setProject('a'); active.setWorktreeIntent('worktree');
 Object.assign(active.current, {sessionId:'existing',worktreeId:'wt',worktreeBranch:'session/branch'});
 const {composerPreferences:p}=await import('./composer-preferences');
 p.rememberWorktreeIntent({projectId:'a',projectV2Id:null},'local');
 active.applyComposerDefaults(); status('a',false); active.setProject('b');
 expect(active.current).toMatchObject({sessionId:'existing',project:'b',worktreeIntent:'worktree',worktreeId:'wt',worktreeBranch:'session/branch'});
});

it('uses the same project’s cached Git capability before a refresh or a quick new-chat send', () => {
 status('a',true); active.setProject('a'); active.setWorktreeIntent('worktree');
 status('b',true); active.setProject('b');
 status('a',true,true); active.setProject('a');
 active.newChat();
 expect(active.current).toMatchObject({project:'a',worktreeIntent:'worktree'});
});
it('restores A’s saved On when its stale Git status arrives after selecting A from B', () => {
 status('a',true); active.setProject('a'); active.setWorktreeIntent('worktree');
 status('b',true); active.setProject('b');
 active.setProject('a');
 expect(active.current).toMatchObject({project:'a',worktreeIntent:'local'});
 status('a',true,true);
 expect(active.current).toMatchObject({project:'a',worktreeIntent:'worktree'});
});
it('does not borrow same-project Git status scoped to an existing session', async () => {
 const {composerPreferences:p}=await import('./composer-preferences');
 p.rememberWorktreeIntent({projectId:'a',projectV2Id:null},'worktree');
 status('a',true);
 Object.assign(mocks.git,{sessionId:'another-session'});
 active.setProject('a');
 expect(active.current).toMatchObject({project:'a',sessionId:null,worktreeIntent:'local'});
});
it('keeps an explicit On while Git is unknown, but refuses a confirmed non-repo', async () => {
 active.setProject('a'); active.setWorktreeIntent('worktree');
 const {composerPreferences:p}=await import('./composer-preferences');
 expect(p.worktreeForProject({projectId:'a',projectV2Id:null}).intent).toBe('worktree');
 status('a',true,true); expect(active.current.worktreeIntent).toBe('worktree');
 status('a',false); active.setWorktreeIntent('worktree');
 expect(active.current.worktreeIntent).toBe('local');
});
it('applies B’s remembered branch while A has a branch claim in flight', async () => {
 const {composerPreferences:p}=await import('./composer-preferences');
 status('a',true); active.setProject('a'); active.setWorktreeIntent('worktree'); active.setWorktreeBranch('topic/a');
 const claim=p.claimBranchChoice({projectId:'a',projectV2Id:null},'topic/a',false);
 const b={projectId:'b',projectV2Id:null};
 p.rememberWorktreeIntent(b,'worktree');
 p.rememberCheckout(b,{...b,kind:'branch',branch:'topic/b',baseBranch:'develop',createNew:true});
 status('b',true); active.setProject('b');
 expect(p.checkoutClaimed).toBe(false);
 expect(active.current).toMatchObject({project:'b',worktreeIntent:'worktree',worktreeBranch:'topic/b',worktreeBaseBranch:'develop',worktreeCreateNewBranch:true});
 claim.release();
});
it('links A’s discovered shared ID while B is selected without switching the composer to A', async () => {
 status('a',true); active.setProject('a'); active.setWorktreeIntent('worktree');
 status('b',true); active.setProject('b');
 active.linkDiscoveredProjects([{local_project_id:'a',project_v2_id:'shared-a'}]);
 const {composerPreferences:p}=await import('./composer-preferences');
 expect(p.worktreeForProject({projectId:'a',projectV2Id:'shared-a'}).intent).toBe('worktree');
 expect(p.current.project).toEqual({projectId:'b',projectV2Id:null});
 expect(active.current).toMatchObject({project:'b',projectV2Id:null,worktreeIntent:'local'});
});
it('links a catalog-discovered shared ID without reselecting the project or touching an existing chat', async () => {
 status('a',true); active.setProject('a'); active.setWorktreeIntent('worktree');
 active.linkDiscoveredProjects([{local_project_id:'a',project_v2_id:'shared-a'}]);
 const {composerPreferences:p}=await import('./composer-preferences');
 expect(p.worktreeForProject({projectId:'a',projectV2Id:'shared-a'}).intent).toBe('worktree');
 expect(p.current.project).toEqual({projectId:'a',projectV2Id:'shared-a'});
 expect(active.current).toMatchObject({project:'a',projectV2Id:'shared-a',worktreeIntent:'worktree'});
});
