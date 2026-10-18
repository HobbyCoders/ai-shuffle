import {beforeEach, expect, it, vi} from 'vitest';
const mocks = vi.hoisted(() => ({adapters:new Map<string,{read:()=>unknown; apply:(v:unknown)=>void}>(), prefixes:new Map<string,any>(), save:vi.fn(), epoch:0}));
vi.mock('./session-owner', () => ({sessionOwnerEpoch:()=>mocks.epoch}));
vi.mock('$lib/services/preferences', () => ({preferenceSync:{
 register:(key:string, adapter:any)=>mocks.adapters.set(key,adapter), registerPrefix:(prefix:string,factory:any)=>mocks.prefixes.set(prefix,factory), save:mocks.save, reconnect:vi.fn(async()=>{})
}}));
beforeEach(() => {vi.resetModules(); localStorage.clear(); mocks.adapters.clear(); mocks.prefixes.clear(); mocks.save.mockClear(); mocks.epoch=0;});

it('uses the server benchmark default only before any per-model choice, including null', async () => {
 const {hydrateModelRegistry} = await import('$lib/constants/models');
 const {composerPreferences:p} = await import('./composer-preferences');
 const {api} = await import('$lib/api/client');
 vi.spyOn(api,'get').mockResolvedValue({models:[], default:'', legacy_map:{}, providers:[{
  id:'codex',name:'Subscription',provider_type:'chatgpt',default_model:'gpt-6.1-sol', models:[
   {id:'gpt-6.1-sol',label:'Sol',effort_levels:['low','high'],default_effort:'low',initial_effort:'high'},
   {id:'other',label:'Other',effort_levels:['low','high'],default_effort:'high'}]}]});
 await hydrateModelRegistry();
 const {resolveModelEffort} = await import('$lib/constants/model-effort');
 expect(resolveModelEffort('prov:codex:gpt-6.1-sol').defaultValue).toBe('low'); // Auto slider, web + mobile
 expect(p.effortForModel('prov:codex:gpt-6.1-sol')).toBe('high');
 expect(p.effortForModel('prov:codex:other')).toBeNull();
 expect(p.current.modelEfforts).toEqual({}); // implicit default never saves a pick
 p.rememberEffort('prov:codex:gpt-6.1-sol','low');
 expect(p.effortForModel('prov:codex:gpt-6.1-sol')).toBe('low');
 p.rememberEffort('prov:codex:gpt-6.1-sol',null);
 expect(p.effortForModel('prov:codex:gpt-6.1-sol')).toBeNull();
});

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
 mocks.adapters.get('composer.project')!.apply({projectId:'host-a',projectV2Id:'repo'});
 mocks.prefixes.get('composer.projectWorktree.')('composer.projectWorktree.shared%3Arepo').apply({intent:'worktree',checkout});
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
 mocks.adapters.get((await import('./composer-preferences')).projectWorktreeKey(project))!.apply({intent:'local',checkout:{...choice}});
 mocks.save.mockClear();
 consume();
 expect(p.current.checkout).toBeNull();
 expect(p.current.project).toEqual(project); expect(p.current.model).toBe('model-to-keep');
 const key=(await import('./composer-preferences')).projectWorktreeKey(project);
 expect(JSON.parse(localStorage.getItem('ai-shuffle-composer:projectWorktrees')!)[key].checkout).toBeNull();
 expect(mocks.save).toHaveBeenCalledExactlyOnceWith(key,{intent:'local',checkout:null});
 vi.resetModules();
 const {composerPreferences:reloaded} = await import('./composer-preferences');
 expect(reloaded.current.checkout).toBeNull(); expect(reloaded.current.model).toBe('model-to-keep');
});

it.each(['explicit-repick','remote-change-and-return','account-change'])('protects later intent across %s', async (change) => {
 const {composerPreferences:p} = await import('./composer-preferences');
 const project={projectId:'local',projectV2Id:null};
 p.set('project',project);
 const choice={...project,kind:'branch' as const,branch:'topic/new',baseBranch:'main',createNew:true};
 p.set('checkout',choice);
 const consume=p.claimBranchChoice(project,choice.branch,true).consume;
 if(change==='explicit-repick') p.set('checkout',{...choice});
 if(change==='remote-change-and-return') {
  mocks.adapters.get((await import('./composer-preferences')).projectWorktreeKey(project))!.apply({intent:'local',checkout:{...choice,branch:'topic/other'}});
  mocks.adapters.get((await import('./composer-preferences')).projectWorktreeKey(project))!.apply({intent:'local',checkout:{...choice}});
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
 p.set('project',project);
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
 p.set('project',project);
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
 p.set('project',project);
 const choice={...project,kind:'branch' as const,branch:'topic/new',baseBranch:'main',createNew:false};
 p.set('checkout',choice);
 expect(p.checkoutClaimed).toBe(false);
 const claim=p.claimBranchChoice(project,choice.branch,false);
 expect(p.checkoutClaimed).toBe(true);
 // The same account snapshot echoed back is still the claimed choice.
 mocks.adapters.get((await import('./composer-preferences')).projectWorktreeKey(project))!.apply({intent:'local',checkout:{...choice}});
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

it('round-trips the Standard context cap per canonical model through the account adapter', async () => {
 const {composerPreferences:p, contextCapRevision} = await import('./composer-preferences');
 let revisions = 0;
 const stop = contextCapRevision.subscribe(() => { revisions += 1; });
 expect(p.contextCapForModel('opus')).toBeNull();
 p.rememberContextCap('prov:api:claude-opus-5[1m]', 450_000);
 expect(mocks.save).toHaveBeenLastCalledWith('composer.modelContextCap.claude-opus-5', 450_000);
 vi.resetModules();
 const {composerPreferences:reloaded} = await import('./composer-preferences');
 for (const alias of ['opus', 'prov:api:claude-opus-5', 'opus[1m]']) expect(reloaded.contextCapForModel(alias)).toBe(450_000);
 mocks.adapters.get('composer.modelContextCap.claude-opus-5')!.apply(600_000);
 expect(reloaded.contextCapForModel('opus')).toBe(600_000);
 // Junk from another device is ignored rather than trusted.
 mocks.adapters.get('composer.modelContextCap.claude-opus-5')!.apply('lots');
 expect(reloaded.contextCapForModel('opus')).toBeNull();
 reloaded.rememberContextCap('opus', null);
 expect(mocks.save).toHaveBeenLastCalledWith('composer.modelContextCap.claude-opus-5', null);
 expect(revisions).toBeGreaterThan(1);
 stop();
});

it('keeps per-project settings across reload, explicit Off, and remote updates', async () => {
 const {composerPreferences:p, projectWorktreeKey:key}=await import('./composer-preferences');
 const a={projectId:'a',projectV2Id:'shared-a'}, b={projectId:'b',projectV2Id:null};
 const checkout={...a,kind:'branch' as const,branch:'topic/a',baseBranch:'develop',createNew:true};
 p.rememberWorktreeIntent(a,'worktree'); p.rememberCheckout(a,checkout);
 expect(p.worktreeForProject(b)).toEqual({intent:'local',checkout:null});
 expect(mocks.save).toHaveBeenLastCalledWith(key(a),{intent:'worktree',checkout});
 p.rememberWorktreeIntent(b,'local');
 expect(p.worktreeForProject(a)).toEqual({intent:'worktree',checkout});
 vi.resetModules(); const {composerPreferences:reload}=await import('./composer-preferences');
 expect(reload.worktreeForProject(a)).toEqual({intent:'worktree',checkout});
 expect(reload.worktreeForProject(b)).toEqual({intent:'local',checkout:null});
 mocks.save.mockClear(); mocks.adapters.get(key(a))!.apply({intent:'local',checkout});
 expect(reload.worktreeForProject(a).intent).toBe('local');
 expect(mocks.save).not.toHaveBeenCalled();
 for (const adapter of mocks.adapters.values()) adapter.apply(undefined);
 expect(reload.worktreeForProject(a)).toEqual({intent:'local',checkout:null});
});
it('seeds legacy On only for the last selected project and moves checkout under its own tagged project', async () => {
 localStorage.setItem('aihub_executionMode','worktree');
 localStorage.setItem('ai-shuffle-composer:worktreeIntent',JSON.stringify('worktree'));
 localStorage.setItem('ai-shuffle-composer:checkout',JSON.stringify({kind:'branch',projectId:'b',projectV2Id:null,branch:'old',baseBranch:'main',createNew:true}));
 localStorage.setItem('ai-shuffle-composer:project',JSON.stringify({projectId:'a',projectV2Id:null}));
 const {composerPreferences:p}=await import('./composer-preferences');
 expect(p.worktreeForProject({projectId:'a',projectV2Id:null})).toEqual({intent:'worktree',checkout:null});
 expect(p.worktreeForProject({projectId:'b',projectV2Id:null})).toEqual({intent:'local',checkout:expect.objectContaining({branch:'old'})});
 for (const projectId of ['c','']) expect(p.worktreeForProject({projectId,projectV2Id:null})).toEqual({intent:'local',checkout:null});
 expect(localStorage.getItem('aihub_executionMode')).toBeNull();
 expect(localStorage.getItem('ai-shuffle-composer:worktreeIntent')).toBeNull();
 expect(localStorage.getItem('ai-shuffle-composer:checkout')).toBeNull();
 expect(mocks.adapters.has('composer.worktreeIntent')).toBe(false);
 expect(mocks.adapters.has('composer.checkout')).toBe(false);
 expect(mocks.save).not.toHaveBeenCalled();
});
it('does not spread legacy On with no recorded project or overwrite new scoped choices', async () => {
 localStorage.setItem('aihub_executionMode','worktree');
 const {composerPreferences:p,projectWorktreeKey:key}=await import('./composer-preferences');
 expect(p.worktreeForProject({projectId:'a',projectV2Id:null}).intent).toBe('local');
 p.set('project',{projectId:'a',projectV2Id:null}); p.rememberWorktreeIntent(p.current.project,'local');
 localStorage.setItem('aihub_executionMode','worktree');
 localStorage.setItem('ai-shuffle-composer:checkout',JSON.stringify({kind:'branch',projectId:'a',projectV2Id:null,branch:'old',baseBranch:'main',createNew:true}));
 vi.resetModules();
 const {composerPreferences:reload}=await import('./composer-preferences');
 expect(reload.worktreeForProject({projectId:'a',projectV2Id:null})).toEqual({intent:'local',checkout:null});
 expect(mocks.adapters.has(key({projectId:'a',projectV2Id:null}))).toBe(true);
});
it('consumes only A after switching to B while creation is in flight', async () => {
 const {composerPreferences:p}=await import('./composer-preferences');
 const a={projectId:'a',projectV2Id:null}, b={projectId:'b',projectV2Id:null};
 p.set('project',a);
 p.rememberCheckout(a,{...a,kind:'branch',branch:'topic/a',baseBranch:'main',createNew:true});
 const claim=p.claimBranchChoice(a,'topic/a',true);
 expect(p.checkoutClaimed).toBe(true);
 p.set('project',b);
 expect(p.checkoutClaimed).toBe(false);
 p.rememberCheckout(b,{...b,kind:'branch',branch:'topic/b',baseBranch:'develop',createNew:false});
 claim.consume();
 expect(p.worktreeForProject(a).checkout).toBeNull();
 expect(p.current.checkout?.branch).toBe('topic/b');
});

it('does not revive a deferred local branch after a newer explicit clear on the shared project', async () => {
 const {composerPreferences:p}=await import('./composer-preferences');
 const local={projectId:'a',projectV2Id:null}, shared={...local,projectV2Id:'shared-a'};
 p.set('project',local);
 p.rememberWorktreeIntent(local,'worktree');
 p.rememberCheckout(local,{...local,kind:'branch',branch:'topic/a',baseBranch:'main',createNew:true});
 const claim=p.claimBranchChoice(local,'topic/a',true);
 p.linkWorktreeProject(shared);
 expect(p.worktreeForProject(shared)).toEqual({intent:'worktree',checkout:null});
 // This is a new user decision even though the deferred checkout is already null.
 p.rememberCheckout(shared,null);
 claim.release();
 expect(p.worktreeForProject(shared)).toEqual({intent:'worktree',checkout:null});
 expect(p.worktreeForProject(local).checkout).toBeNull();
});
it('migrates the legacy fast flag only for the last selected model', async () => {
 localStorage.setItem('aihub_lastModel','prov:api:claude-opus-5'); localStorage.setItem('aihub_lastFastMode','1');
 const {composerPreferences:p} = await import('./composer-preferences');
 expect(p.fastForModel('opus')).toBe(true);
 expect(p.fastForModel('gpt-6.1-sol')).toBeNull();
 expect(mocks.save).not.toHaveBeenCalled();
});
