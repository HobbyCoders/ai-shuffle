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
 expect(active.current.worktreeIntent).toBe('local');
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
 expect(active.current).toMatchObject({sessionId:'existing',project:'a',worktreeIntent:'worktree',worktreeId:'wt',worktreeBranch:'session/branch'});
});
