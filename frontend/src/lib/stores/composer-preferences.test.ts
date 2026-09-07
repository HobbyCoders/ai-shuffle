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

it('does not let an old queued callback consume a newer account snapshot', async () => {
 const {composerPreferences:p} = await import('./composer-preferences');
 const seen:unknown[]=[]; p.onRemote(()=>seen.push(p.current.model));
 mocks.adapters.get('composer.model')!.apply('old-account-model');
 mocks.epoch++;
 mocks.adapters.get('composer.model')!.apply('new-account-model');
 await Promise.resolve();
 expect(seen).toEqual(['new-account-model']);
});
