import {beforeEach, expect, it, vi} from 'vitest';
import {PreferenceSync} from '$lib/services/preference-sync';
let values: Record<string,unknown>;
const puts=vi.fn();
async function device() {
 vi.resetModules(); localStorage.clear();
 const sync=new PreferenceSync({
  all:async()=>structuredClone(values),
  get:async key=>key in values ? {value:structuredClone(values[key])} : null,
  put:async(key,value,onlyIfMissing)=>{
   puts(key,value);
   if (!onlyIfMissing || !(key in values)) values[key]=structuredClone(value);
   return {value:structuredClone(values[key])};
  },
 });
 vi.doMock('$lib/services/preferences',()=>({preferenceSync:sync}));
 const {composerPreferences:p,projectWorktreeKey:key}=await import('./composer-preferences');
 sync.start(); await sync.hydrate();
 return {p,key,sync};
}
beforeEach(()=>{values={};puts.mockClear();});
it('syncs only the chosen project and restores on a second device by shared UUID', async()=>{
 const {p,key,sync}=await device();
 const a={projectId:'clone-on-first-device',projectV2Id:'shared-a'};
 const b={projectId:'b',projectV2Id:'shared-b'};
 p.rememberWorktreeIntent(a,'worktree');
 p.rememberCheckout(a,{...a,kind:'branch',branch:'topic/a',baseBranch:'develop',createNew:true});
 await sync.hydrate();
 expect(values[key(a)]).toEqual({intent:'worktree',checkout:expect.objectContaining({branch:'topic/a',baseBranch:'develop',createNew:true})});
 expect(values).not.toHaveProperty(key(b));
 expect(puts.mock.calls.some(([key])=>key==='composer.worktreeIntent'||key==='composer.checkout')).toBe(false);
 sync.stop(false);
 const {p:other,sync:otherSync}=await device();
 expect(other.worktreeForProject({...a,projectId:'different-local-clone'})).toEqual({intent:'worktree',checkout:expect.objectContaining({branch:'topic/a'})});
 expect(other.worktreeForProject(b)).toEqual({intent:'local',checkout:null});
 other.rememberWorktreeIntent({...a,projectId:'different-local-clone'},'local');
 await otherSync.hydrate();
 expect(values[key(a)]).toMatchObject({intent:'local'});
 otherSync.stop();
 expect(other.worktreeForProject(a)).toEqual({intent:'local',checkout:null});
});
it.each([false,true])('links a just-discovered project without losing its pick or overwriting an existing shared choice (saved=%s)',async saved=>{
 const {p,key,sync}=await device();
 const local={projectId:'a',projectV2Id:null};
 const shared={projectId:'a',projectV2Id:'shared-a'};
 p.rememberWorktreeIntent(local,'worktree');
 p.rememberCheckout(local,{...local,kind:'branch',branch:'topic/a',baseBranch:'develop',createNew:true});
 await sync.hydrate();
 if (saved) values[key(shared)]={intent:'local',checkout:null};
 p.set('project',shared);
 await sync.hydrate();
 expect(p.worktreeForProject(shared)).toEqual(saved ? {intent:'local',checkout:null}
  : {intent:'worktree',checkout:expect.objectContaining({...shared,branch:'topic/a',baseBranch:'develop',createNew:true})});
 expect(p.worktreeForProject(local)).toEqual({intent:'local',checkout:null});
 sync.stop();
});
it.each(['consume','release'] as const)('moves intent immediately but defers the claimed branch until %s',async action=>{
 const {p,sync}=await device();
 const local={projectId:'a',projectV2Id:null}, shared={...local,projectV2Id:'shared-a'};
 p.set('project',local);
 p.rememberWorktreeIntent(local,'worktree');
 p.rememberCheckout(local,{...local,kind:'branch',branch:'topic/a',baseBranch:'main',createNew:true});
 const claim=p.claimBranchChoice(local,'topic/a',true);
 p.linkWorktreeProject(shared);
 expect(p.worktreeForProject(shared)).toEqual({intent:'worktree',checkout:null});
 claim[action]();
 expect(p.worktreeForProject(local).checkout).toBeNull();
 expect(p.worktreeForProject(shared)).toEqual({intent:'worktree',checkout:action === 'consume' ? null : expect.objectContaining({...shared,branch:'topic/a'})});
 await sync.hydrate();
 expect(p.worktreeForProject(shared).intent).toBe('worktree');
 sync.stop();
});
it('ignores old server-wide values instead of migrating them into every project',async()=>{
 values={'composer.worktreeIntent':'worktree','composer.checkout':{kind:'branch',projectId:'a',projectV2Id:null,branch:'old',baseBranch:'main',createNew:true}};
 const {p,sync}=await device();
 for (const projectId of ['a','b','']) expect(p.worktreeForProject({projectId,projectV2Id:null})).toEqual({intent:'local',checkout:null});
 expect(puts.mock.calls.some(([key])=>key.startsWith('composer.projectWorktree.'))).toBe(false);
 sync.stop();
});
