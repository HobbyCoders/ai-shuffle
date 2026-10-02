import {beforeEach, expect, it, vi} from 'vitest';
const mocks = vi.hoisted(() => ({adapters:new Map<string,{read:()=>unknown; apply:(v:unknown)=>void}>(), save:vi.fn(), epoch:0}));
vi.mock('./session-owner', () => ({sessionOwnerEpoch:()=>mocks.epoch}));
vi.mock('$lib/services/preferences', () => ({preferenceSync:{
 register:(key:string, adapter:any)=>mocks.adapters.set(key,adapter), save:mocks.save, reconnect:vi.fn(async()=>{})
}}));
beforeEach(() => {vi.resetModules(); localStorage.clear(); mocks.adapters.clear(); mocks.save.mockClear(); mocks.epoch=0;});

it('applies account snapshots and explicit clears without echoing saves', async () => {
 localStorage.setItem('aihub_lastEffort','high');
 const {composerPreferences:p} = await import('./composer-preferences');
 expect(p.current.effort).toBe('high');
 const changed=vi.fn(); p.onRemote(changed);
 mocks.adapters.get('composer.effort')!.apply('max');
 expect(p.current.effort).toBe('max');
 expect(localStorage.getItem('aihub_lastEffort')).toBe('max');
 mocks.adapters.get('composer.permissionMode')!.apply(null);
 expect(p.current.permissionMode).toBeNull();
 expect(mocks.save).not.toHaveBeenCalled();
 p.set('profile','agent-a');
 expect(mocks.save).toHaveBeenCalledExactlyOnceWith('composer.profile','agent-a');
});

it.each(['default','auto','acceptEdits','bypassPermissions'])('remembers explicit %s picks across reload and account sync', async (mode) => {
 const {composerPreferences:p} = await import('./composer-preferences');
 p.set('permissionMode',mode);
 expect(p.current.permissionMode).toBe(mode);
 expect(mocks.save).toHaveBeenCalledWith('composer.permissionMode',mode);
 expect(localStorage.getItem('aihub_lastPermissionMode')).toBe(mode);
 vi.resetModules();
 const {composerPreferences:reloaded} = await import('./composer-preferences');
 expect(reloaded.current.permissionMode).toBe(mode);
 mocks.adapters.get('composer.permissionMode')!.apply('default');
 mocks.adapters.get('composer.permissionMode')!.apply(mode);
 expect(reloaded.current.permissionMode).toBe(mode);
 expect(localStorage.getItem('ai-shuffle-composer:permissionMode')).toBe(JSON.stringify(mode));
});

it('migrates legacy bypass without silently replacing the user choice', async () => {
 localStorage.setItem('aihub_lastPermissionMode','bypassPermissions');
 const {composerPreferences:p} = await import('./composer-preferences');
 expect(p.current.permissionMode).toBe('bypassPermissions');
 mocks.adapters.get('composer.permissionMode')!.apply('plan');
 expect(p.current.permissionMode).toBe('default');
});

it('resolves only the stable project UUID and never copies a foreign path or guesses by name', async () => {
 const {resolveComposerProject} = await import('./composer-preferences');
 const pick={projectId:'host-a-project',projectV2Id:'shared-repo'};
 const cluster=[{project_v2_id:'shared-repo',local_project_id:'host-b-project',name:'same',origin:'https://example/repo',remote_only:false,eligible:[]}];
 expect(resolveComposerProject(pick,[{id:'host-b-project'}],cluster)).toEqual({project:'host-b-project',projectV2Id:'shared-repo'});
 expect(resolveComposerProject(pick,[{id:'unrelated'}],[])).toEqual({project:'',projectV2Id:'shared-repo'});
});

it('publishes a complete snapshot once before a staged branch can guard later changes', async () => {
 const {composerPreferences:p} = await import('./composer-preferences');
 const seen:unknown[]=[];
 p.onRemote(()=>seen.push(structuredClone(p.current)));
 const checkout={kind:'branch',projectId:'host-a',projectV2Id:'repo',branch:'topic/new',baseBranch:'main',createNew:true};
 // The registered order applies checkout before model and effort.
 mocks.adapters.get('composer.checkout')!.apply(checkout);
 mocks.adapters.get('composer.model')!.apply('sonnet');
 mocks.adapters.get('composer.effort')!.apply('max');
 mocks.adapters.get('composer.permissionMode')!.apply('acceptEdits');
 expect(seen).toEqual([]);
 await Promise.resolve();
 expect(seen).toHaveLength(1);
 expect(seen[0]).toMatchObject({checkout,model:'sonnet',effort:'max',permissionMode:'acceptEdits'});
 expect(mocks.save).not.toHaveBeenCalled();
});

it('retires a queued old-account notification and permits the new account reset', async () => {
 const {composerPreferences:p} = await import('./composer-preferences');
 const changed=vi.fn(); p.onRemote(changed);
 mocks.adapters.get('composer.model')!.apply('old-account-model');
 mocks.epoch++;
 await Promise.resolve();
 expect(changed).not.toHaveBeenCalled();
 for(const adapter of mocks.adapters.values()) adapter.apply(undefined);
 await Promise.resolve();
 expect(changed).toHaveBeenCalledTimes(1);
 expect(p.current.model).toBeNull();
 expect(mocks.save).not.toHaveBeenCalled();
});

it('clears only the successfully consumed choice in storage and account sync, not other preferences', async () => {
 const {composerPreferences:p} = await import('./composer-preferences');
 const project={projectId:'local',projectV2Id:'shared'};
 const choice={...project,kind:'branch' as const,branch:'topic/new',baseBranch:'main',createNew:true};
 p.set('project',project); p.set('checkout',choice); p.set('model','model-to-keep');
 const consume=p.claimBranchChoice(project,choice.branch,true).consume;
 // The same account snapshot arriving during POST is not a new selection.
 mocks.adapters.get('composer.checkout')!.apply({...choice});
 mocks.save.mockClear();
 consume();
 expect(p.current.checkout).toBeNull();
 expect(p.current.project).toEqual(project); expect(p.current.model).toBe('model-to-keep');
 expect(localStorage.getItem('ai-shuffle-composer:checkout')).toBe('null');
 expect(mocks.save).toHaveBeenCalledExactlyOnceWith('composer.checkout',null);
 vi.resetModules();
 const {composerPreferences:reloaded} = await import('./composer-preferences');
 expect(reloaded.current.checkout).toBeNull(); expect(reloaded.current.model).toBe('model-to-keep');
});

it.each(['explicit-repick','remote-change-and-return','account-change'])('protects later intent across %s', async (change) => {
 const {composerPreferences:p} = await import('./composer-preferences');
 const project={projectId:'local',projectV2Id:null};
 const choice={...project,kind:'branch' as const,branch:'topic/new',baseBranch:'main',createNew:true};
 p.set('checkout',choice);
 const consume=p.claimBranchChoice(project,choice.branch,true).consume;
 if(change==='explicit-repick') p.set('checkout',{...choice});
 if(change==='remote-change-and-return') {
  mocks.adapters.get('composer.checkout')!.apply({...choice,branch:'topic/other'});
  mocks.adapters.get('composer.checkout')!.apply({...choice});
 }
 if(change==='account-change') mocks.epoch++;
 mocks.save.mockClear();
 consume();
 expect(p.current.checkout).toEqual(choice);
 expect(mocks.save).not.toHaveBeenCalled();
});

it('does not consume another project, branch, or existing-worktree preference', async () => {
 const {composerPreferences:p} = await import('./composer-preferences');
 const project={projectId:'local',projectV2Id:null};
 const choice={...project,kind:'branch' as const,branch:'topic/new',baseBranch:'main',createNew:true};
 p.set('checkout',choice); mocks.save.mockClear();
 p.claimBranchChoice({...project,projectId:'different'},choice.branch,true).consume();
 p.claimBranchChoice(project,'topic/different',true).consume();
 p.claimBranchChoice(project,choice.branch,false).consume();
 expect(p.current.checkout).toEqual(choice); expect(mocks.save).not.toHaveBeenCalled();
 const existing={...project,kind:'existing' as const,worktreeId:'wt',branch:choice.branch};
 p.set('checkout',existing); mocks.save.mockClear();
 p.claimBranchChoice(project,choice.branch,true).consume();
 expect(p.current.checkout).toEqual(existing); expect(mocks.save).not.toHaveBeenCalled();
});

it('consumes any remembered branch pick, including an existing branch, but not a legacy worktree pick (#1528)', async () => {
 const {composerPreferences:p} = await import('./composer-preferences');
 const project={projectId:'local',projectV2Id:null};
 // New branch... creates the branch up front, so it is remembered like a listed branch.
 const listed={...project,kind:'branch' as const,branch:'live/1528-explicit',baseBranch:'main',createNew:false};
 p.set('checkout',listed);
 // A request that would create the branch does not match the staged pick.
 p.claimBranchChoice(project,listed.branch,true).consume();
 expect(p.current.checkout).toEqual(listed);
 p.claimBranchChoice(project,listed.branch,false).consume();
 expect(p.current.checkout).toBeNull();
 const legacy={...project,kind:'existing' as const,worktreeId:'wt',branch:'feature/legacy'};
 p.set('checkout',legacy);
 p.claimBranchChoice(project,legacy.branch,false).consume();
 expect(p.current.checkout).toEqual(legacy);
});

it('marks the choice claimed while a create is in flight and keeps it after a failure', async () => {
 const {composerPreferences:p} = await import('./composer-preferences');
 const project={projectId:'local',projectV2Id:null};
 const choice={...project,kind:'branch' as const,branch:'topic/new',baseBranch:'main',createNew:false};
 p.set('checkout',choice);
 expect(p.checkoutClaimed).toBe(false);
 const claim=p.claimBranchChoice(project,choice.branch,false);
 expect(p.checkoutClaimed).toBe(true);
 // The same account snapshot echoed back is still the claimed choice.
 mocks.adapters.get('composer.checkout')!.apply({...choice});
 expect(p.checkoutClaimed).toBe(true);
 mocks.save.mockClear();
 claim.release();
 claim.consume();
 expect(p.checkoutClaimed).toBe(false);
 expect(p.current.checkout).toEqual(choice); expect(mocks.save).not.toHaveBeenCalled();
 // A newer pick is not covered by an older claim.
 const again=p.claimBranchChoice(project,choice.branch,false);
 p.set('checkout',{...choice,branch:'topic/next'});
 expect(p.checkoutClaimed).toBe(false);
 again.consume();
 expect(p.current.checkout?.branch).toBe('topic/next');
});

it('does not let an old queued callback consume a newer account snapshot', async () => {
 const {composerPreferences:p} = await import('./composer-preferences');
 const seen:unknown[]=[]; p.onRemote(()=>seen.push(p.current.model));
 mocks.adapters.get('composer.model')!.apply('old-account-model');
 mocks.epoch++;
 mocks.adapters.get('composer.model')!.apply('new-account-model');
 await Promise.resolve();
 expect(seen).toEqual(['new-account-model']);
});
