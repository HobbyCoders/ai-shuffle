import { beforeEach, expect, it, vi } from 'vitest';
const mocks = vi.hoisted(() => ({ adapters:new Map<string,{read():unknown;apply(value:unknown):void}>(), save:vi.fn() }));
vi.mock('$lib/services/preferences',()=>({preferenceSync:{register:(key:string,adapter:any)=>mocks.adapters.set(key,adapter),save:mocks.save}}));
beforeEach(()=>{vi.resetModules();localStorage.clear();mocks.adapters.clear();mocks.save.mockClear();});

it('migrates existing local choices and sanitizes remote reset values without echo writes',async()=>{
 localStorage.setItem('ai-shuffle-build-subtab','hooks');
 localStorage.setItem('ai-shuffle-install-never','1');
 localStorage.setItem('ai-shuffle-install-dismissed','1700000000000');
 const {buildPreference}=await import('./build-preference.svelte');
 const {installPromptPreference}=await import('./install-prompt-preference.svelte');
 expect(mocks.adapters.get('build_subtab')!.read()).toBe('hooks');
 expect(mocks.adapters.get('install_prompt')!.read()).toEqual({never:true,dismissedAt:1700000000000});
 mocks.adapters.get('build_subtab')!.apply('../editor');
 mocks.adapters.get('install_prompt')!.apply({never:'yes',dismissedAt:NaN,deviceId:'ignored'});
 expect(buildPreference.current).toBe('profile');
 expect(installPromptPreference.current).toEqual({never:false,dismissedAt:null});
 expect(localStorage.getItem('ai-shuffle-install-never')).toBeNull();
 expect(localStorage.getItem('ai-shuffle-install-dismissed')).toBeNull();
 expect(mocks.save).not.toHaveBeenCalled();
});

it('keeps the existing seven-day snooze duration and permanent suppression',async()=>{
 const {installPromptPreference:preference}=await import('./install-prompt-preference.svelte');
 const adapter=mocks.adapters.get('install_prompt')!;
 adapter.apply({never:false,dismissedAt:1000});
 expect(preference.isSuppressed(1000+7*86400000-1)).toBe(true);
 expect(preference.isSuppressed(1000+7*86400000)).toBe(false);
 adapter.apply({never:true,dismissedAt:1000});
 expect(preference.isSuppressed(1000+8*86400000)).toBe(true);
 adapter.apply(undefined);
 expect(preference.isSuppressed()).toBe(false);
});
