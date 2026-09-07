import {beforeEach, expect, it, vi} from 'vitest';
const mocks = vi.hoisted(() => ({adapters:new Map<string,{read:()=>unknown; apply:(v:unknown)=>void}>(), save:vi.fn()}));
vi.mock('$lib/services/preferences', () => ({preferenceSync:{
 register:(key:string, adapter:any)=>mocks.adapters.set(key,adapter), save:mocks.save, reconnect:vi.fn(async()=>{})
}}));
beforeEach(() => {vi.resetModules(); localStorage.clear(); mocks.adapters.clear(); mocks.save.mockClear();});

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
