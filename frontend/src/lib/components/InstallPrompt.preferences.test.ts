import { beforeEach, afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render } from '@testing-library/svelte';
import { flushSync } from 'svelte';
const mocks = vi.hoisted(() => ({ adapters: new Map<string, {apply(value: unknown): void; read(): unknown}>(), save: vi.fn(), epoch:0 }));
vi.mock('$lib/services/preferences', () => ({ preferenceSync: { register: (key: string, adapter: any) => mocks.adapters.set(key, adapter), save: mocks.save } }));
vi.mock('$lib/stores/session-owner', () => ({sessionOwnerEpoch:()=>mocks.epoch}));
let pwa: (typeof import('$lib/stores/pwa.svelte'))['pwa'];
beforeEach(async () => {
 vi.useFakeTimers(); for (const adapter of mocks.adapters.values()) adapter.apply(undefined); mocks.save.mockClear(); mocks.epoch=0; localStorage.clear();
 Object.defineProperty(window,'matchMedia',{configurable:true,value:vi.fn(()=>({matches:false}))});
 pwa = (await import('$lib/stores/pwa.svelte')).pwa;
 pwa.init();
});
afterEach(() => {cleanup();pwa.destroy();vi.clearAllTimers();vi.useRealTimers();});
function installEvent(choice: Promise<{outcome:string}> = Promise.resolve({outcome:'dismissed'})) {
 const event = Object.assign(new Event('beforeinstallprompt',{cancelable:true}), {prompt:vi.fn(async()=>{}),userChoice:choice});
 window.dispatchEvent(event);
 return event;
}
async function delayPrompt() { flushSync(); await vi.advanceTimersByTimeAsync(3000); flushSync(); }

it('applies startup suppression, remote reset and updates without discarding the device install event', async () => {
 const adapter=mocks.adapters.get('install_prompt')!;
 adapter.apply({never:true,dismissedAt:null});
 const event=installEvent();
 const page=render((await import('./InstallPrompt.svelte')).default);
 await delayPrompt();
 expect(page.queryByRole('dialog')).toBeNull();
 flushSync(()=>adapter.apply(undefined));
 await delayPrompt();
 expect(page.getByRole('dialog')).toBeTruthy();
 expect(pwa.current.installPromptEvent).toBe(event);
 flushSync(()=>adapter.apply({never:false,dismissedAt:Date.now()}));
 expect(page.queryByRole('dialog')).toBeNull();
 expect(pwa.current.installPromptEvent).toBe(event);
 expect(mocks.save).not.toHaveBeenCalled();
});

it.each(['Not now',"Don't show again"])('syncs the existing %s action', async label => {
 installEvent();
 const page=render((await import('./InstallPrompt.svelte')).default);
 await delayPrompt();
 await fireEvent.click(page.getByRole('button',{name:label}));
 expect(page.queryByRole('dialog')).toBeNull();
 expect(mocks.save).toHaveBeenCalledExactlyOnceWith('install_prompt',label==='Not now'
  ? {never:false,dismissedAt:Date.now()} : {never:true,dismissedAt:null});
});

it('snoozes browser dismissal but cannot write a late old-account outcome into the next account', async () => {
 installEvent();
 expect(await pwa.promptInstall()).toBe(false);
 expect(mocks.save).toHaveBeenCalledExactlyOnceWith('install_prompt',{never:false,dismissedAt:Date.now()});
 mocks.save.mockClear();
 let resolve!: (value:{outcome:string})=>void;
 installEvent(new Promise(done=>{resolve=done;}));
 const pending=pwa.promptInstall();
 mocks.epoch++;
 mocks.adapters.get('install_prompt')!.apply(undefined);
 resolve({outcome:'dismissed'});
 await pending;
 expect(mocks.save).not.toHaveBeenCalled();
 expect(mocks.adapters.get('install_prompt')!.read()).toEqual({never:false,dismissedAt:null});
});
