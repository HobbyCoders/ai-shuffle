import {beforeEach, expect, it, vi} from 'vitest';
const mocks = vi.hoisted(() => ({adapters:new Map<string,{read:()=>unknown; apply:(v:unknown)=>void}>(), prefixes:new Map<string,any>(), save:vi.fn(), epoch:0}));
vi.mock('./session-owner', () => ({sessionOwnerEpoch:()=>mocks.epoch}));
vi.mock('$lib/services/preferences', () => ({preferenceSync:{
 register:(key:string, adapter:any)=>mocks.adapters.set(key,adapter), registerPrefix:(prefix:string,factory:any)=>mocks.prefixes.set(prefix,factory), save:mocks.save, reconnect:vi.fn(async()=>{})
}}));
beforeEach(() => {vi.resetModules(); localStorage.clear(); mocks.adapters.clear(); mocks.prefixes.clear(); mocks.save.mockClear(); mocks.epoch=0;});

it('migrates the legacy effort only for the last selected model', async () => {
 localStorage.setItem('aihub_lastModel','prov:old:sol'); localStorage.setItem('aihub_lastEffort','xhigh');
 const {composerPreferences:p} = await import('./composer-preferences');
 expect(p.effortForModel('prov:new:sol')).toBe('xhigh');
 expect(p.effortForModel('unrelated')).toBeNull();
});

it('round-trips model efforts through the account adapter and restores independently of provider', async () => {
 const {composerPreferences:p} = await import('./composer-preferences');
 p.rememberEffort('prov:first:sol', 'xhigh');
 p.rememberEffort('opus', 'low');
 expect(mocks.save).toHaveBeenLastCalledWith('composer.modelEffort.claude-opus-5', 'low');
 expect(mocks.save).not.toHaveBeenCalledWith('composer.modelEfforts', expect.anything());
 vi.resetModules();
 const {composerPreferences:reloaded} = await import('./composer-preferences');
 expect(reloaded.effortForModel('prov:second:sol')).toBe('xhigh');
 expect(reloaded.effortForModel('opus')).toBe('low');
 expect(reloaded.effortForModel('never-used')).toBeNull();
 mocks.adapters.get('composer.modelEffort.sol')!.apply('high');
 mocks.adapters.get('composer.modelEffort.claude-opus-5')!.apply('medium');
 expect(reloaded.effortForModel('prov:first:sol')).toBe('high');
 expect(reloaded.current.modelEfforts).toEqual({sol:'high','claude-opus-5':'medium'});
 reloaded.rememberEffort('opus', null);
 expect(reloaded.effortForModel('opus')).toBeNull();
 mocks.adapters.get('composer.modelEfforts')!.apply(undefined);
 expect(reloaded.effortForModel('prov:first:sol')).toBeNull();
});

it.each(['opus', 'prov:api:claude-opus-5', 'opus[1m]', 'prov:api:claude-opus-5[1m]'])('shares canonical model effort for %s and preserves clears over legacy maps', async model => {
 const {composerPreferences:p} = await import('./composer-preferences');
 p.rememberEffort(model, 'low');
 expect(mocks.save).toHaveBeenLastCalledWith('composer.modelEffort.claude-opus-5', 'low');
 for (const alias of ['opus', 'prov:api:claude-opus-5', 'opus[1m]']) expect(p.effortForModel(alias)).toBe('low');
 mocks.adapters.get('composer.modelEffort.claude-opus-5')!.apply(null);
 mocks.adapters.get('composer.modelEfforts')!.apply({opus:'max'});
 expect(p.effortForModel(model)).toBeNull();
 p.rememberEffort(model, null);
 expect(mocks.save).toHaveBeenLastCalledWith('composer.modelEffort.claude-opus-5', null);
});

it.each(['claude-opus-4-6', 'claude-opus-4-7', 'claude-opus-4-8'])('keeps %s independent from Opus 5, aliases, legacy fallbacks and clears', async older => {
 const {composerPreferences:p} = await import('./composer-preferences');
 const legacy = mocks.prefixes.get('composer.modelEffort.')('composer.modelEffort.opus');
 expect(legacy.readOnly).toBe(true); legacy.apply('high');
 expect(p.effortForModel(older)).toBe('high');
 p.rememberEffort(older, 'low'); p.rememberEffort('opus', 'max');
 expect(mocks.save).toHaveBeenLastCalledWith('composer.modelEffort.claude-opus-5', 'max');
 mocks.adapters.get('composer.modelEfforts')!.apply({[older]:'max', opus:'max'});
 for (const alias of [older, older+'-20260205', 'prov:api:'+older+'[1m]', 'anthropic/'+older])
  expect(p.effortForModel(alias)).toBe('low');
 expect(p.effortForModel('claude-opus-5')).toBe('max');
 p.rememberEffort(older, null);
 expect(p.effortForModel(older)).toBeNull();
 vi.resetModules(); const {composerPreferences:reload} = await import('./composer-preferences');
 expect(reload.effortForModel(older)).toBeNull();
 expect(reload.effortForModel('claude-opus-5')).toBe('max');
 expect(reload.effortForModel('claude-opus-4-9')).toBe('high');
 expect(mocks.save.mock.calls.some(([key])=>key==='composer.modelEffort.opus')).toBe(false);
});

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

it('round-trips fast mode per canonical model through the account adapter', async () => {
 const {composerPreferences:p} = await import('./composer-preferences');
 expect(p.fastForModel('opus')).toBeNull();
 p.rememberFast('prov:api:claude-opus-5[1m]', true);
 expect(mocks.save).toHaveBeenLastCalledWith('composer.modelFast.claude-opus-5', true);
 p.rememberFast('prov:codex:gpt-6.1-sol', false);
 expect(mocks.save).toHaveBeenLastCalledWith('composer.modelFast.gpt-6.1-sol', false);
 vi.resetModules();
 const {composerPreferences:reloaded} = await import('./composer-preferences');
 for (const alias of ['opus', 'prov:api:claude-opus-5', 'opus[1m]']) expect(reloaded.fastForModel(alias)).toBe(true);
 expect(reloaded.fastForModel('gpt-6.1-sol')).toBe(false);
 expect(reloaded.fastForModel('never-used')).toBeNull();
 mocks.adapters.get('composer.modelFast.claude-opus-5')!.apply(false);
 expect(reloaded.fastForModel('opus')).toBe(false);
 mocks.adapters.get('composer.modelFast.claude-opus-5')!.apply(null);
 expect(reloaded.fastForModel('opus')).toBeNull();
 reloaded.rememberFast('opus', null);
 expect(mocks.save).toHaveBeenLastCalledWith('composer.modelFast.claude-opus-5', null);
});

it('migrates the legacy fast flag only for the last selected model', async () => {
 localStorage.setItem('aihub_lastModel','prov:api:claude-opus-5'); localStorage.setItem('aihub_lastFastMode','1');
 const {composerPreferences:p} = await import('./composer-preferences');
 expect(p.fastForModel('opus')).toBe(true);
 expect(p.fastForModel('gpt-6.1-sol')).toBeNull();
 expect(mocks.save).not.toHaveBeenCalled();
});
