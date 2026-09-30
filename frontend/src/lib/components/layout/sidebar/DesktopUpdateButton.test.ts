import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api, getErrorMessage: (error: Error) => error.message }));
const updateAuth = vi.hoisted(() => ({current: {status: {preview_access: false}}}));
vi.mock('$lib/stores/auth.svelte', () => ({auth: updateAuth}));
const notice = vi.hoisted(() => ({ info: vi.fn(), warning: vi.fn(), error: vi.fn() }));
vi.mock('$lib/stores/notifications', () => ({ notify: notice }));
import DesktopUpdateButton from './DesktopUpdateButton.svelte';
const worker = { device_id:'local', device_name:'Current worker', online:true, supported:true, allow_updates:true, phase:'available', version:'abc123' };
const state = { allow_updates:true, update_all_workers:false, workers:[worker] };
beforeEach(() => { localStorage.clear(); updateAuth.current.status.preview_access = false; vi.resetAllMocks(); api.get.mockResolvedValue(state); api.post.mockResolvedValue({results:[{device_name:'Current worker',accepted:true,reason:'Request queued'}]}); });
afterEach(cleanup);

it('produces the ready bell action from the desktop bridge and executes canonical coordination', async () => {
 Object.defineProperty(window,'matchMedia',{configurable:true,value:()=>({matches:false,addEventListener(){},removeEventListener(){}})});
 const install=vi.fn();
 let report!: (state: import('$lib/services/desktop').DesktopUpdateState)=>void;
 window.shuffleDesktop={version:'preview',platform:'linux',window:{getState:async()=>({maximized:false,fullscreen:false,focused:true}),onStateChange:()=>()=>{},setTitleBarTheme(){}},shell:{onCommand:()=>()=>{}},updates:{getState:async()=>({phase:'idle',version:null,percent:null,error:null,checkedAt:null}),onState:listener=>{report=listener;return()=>{};},install}};
 api.get.mockResolvedValue({...state,update_all_workers:true,workers:[{...worker,phase:'ready'}]});
 render(DesktopUpdateButton);
 await screen.findByRole('button',{name:'Update and restart'});
 const {initDesktop}=await import('$lib/services/desktop');
 const stop=initDesktop();
 try {
  report({phase:'ready',version:'notification-produced',percent:100,error:null,checkedAt:null});
  await waitFor(()=>expect(notice.info).toHaveBeenCalledWith(expect.any(String),expect.objectContaining({category:'desktop-update'})));
  const options=notice.info.mock.calls.find(call=>call[1]?.category==='desktop-update')![1];
  options.action.onClick();
  await waitFor(()=>expect(api.post).toHaveBeenCalledExactlyOnceWith('/settings/application-updates',{action:'install'}));
  expect(install).not.toHaveBeenCalled();
 } finally {stop();delete window.shuffleDesktop;}
});


it.each([false, true])('routes a desktop ready notification through fresh canonical scope (all=%s)', async all => {
 api.get.mockResolvedValue({...state, workers:[{...worker,phase:'ready'}]});
 render(DesktopUpdateButton);
 await screen.findByRole('button',{name:'Update and restart'});
 // Policy changes after mount: a ready local shell must prepare a newly selected peer.
 api.get.mockResolvedValue({...state, update_all_workers:all, workers:all
  ? [{...worker,phase:'ready'}, {...worker,device_id:'peer',device_name:'Peer',phase:'available'}]
  : [{...worker,phase:'ready'}]});
 window.dispatchEvent(new Event('application-update-request'));
 await waitFor(()=>expect(api.post).toHaveBeenCalledExactlyOnceWith('/settings/application-updates',{action:all?'download':'install'}));
 expect(notice.info).toHaveBeenCalledWith(expect.stringContaining(all?'All online workers':'Current worker'),expect.anything());
});
it('shows per-worker refusals from a notification request', async () => {
 api.get.mockResolvedValue({...state, update_all_workers:true, workers:[{...worker,phase:'ready'}]});
 api.post.mockResolvedValue({results:[{device_name:'Peer',accepted:false,reason:'Worker is offline; not replayed'}]});
 render(DesktopUpdateButton);
 await screen.findByRole('button',{name:'Update and restart'});
 window.dispatchEvent(new Event('application-update-request'));
 await waitFor(()=>expect(notice.warning).toHaveBeenCalledWith(expect.stringContaining('Peer: Worker is offline; not replayed'),expect.anything()));
});
it.each(['policy-read','disabled','preview','post'])('never bypasses %s failure with a local install', async reason => {
 api.get.mockResolvedValue({...state, workers:[{...worker,phase:'ready'}]});
 render(DesktopUpdateButton);
 await screen.findByRole('button',{name:'Update and restart'});
 if(reason==='policy-read') api.get.mockRejectedValue(new Error('Scope unavailable'));
 if(reason==='disabled') api.get.mockResolvedValue({...state,allow_updates:false});
 if(reason==='preview') updateAuth.current.status.preview_access=true;
 if(reason==='post') api.post.mockRejectedValue(new Error('Worker refused the update'));
 window.dispatchEvent(new Event('application-update-request'));
 await waitFor(()=>expect(notice.error).toHaveBeenCalled());
 if(reason!=='post') expect(api.post).not.toHaveBeenCalled();
});
it('does not replay a second notification click while its scope read is pending', async () => {
 render(DesktopUpdateButton);
 await screen.findByRole('button',{name:'Download update'});
 let complete!: (value: typeof state) => void;
 api.get.mockReturnValueOnce(new Promise(resolve=>complete=resolve));
 window.dispatchEvent(new Event('application-update-request'));
 window.dispatchEvent(new Event('application-update-request'));
 complete({...state,workers:[{...worker,phase:'ready'}]});
 await waitFor(()=>expect(api.post).toHaveBeenCalledTimes(1));
});

it('does not poll or show update controls in an agent preview', async () => {
 vi.useFakeTimers();
 try {
  updateAuth.current.status.preview_access = true;
  render(DesktopUpdateButton);
  await vi.advanceTimersByTimeAsync(60_000);
  window.dispatchEvent(new Event('application-update-policy'));
  expect(api.get).not.toHaveBeenCalled();
  expect(screen.queryByRole('button')).toBeNull();
 } finally { cleanup(); vi.useRealTimers(); }
});
it.each(['idle','unsupported','error'])('hides %s without a known update', async phase => {
 api.get.mockResolvedValue({...state,workers:[{...worker,phase,version:null}]});
 render(DesktopUpdateButton); await waitFor(()=>expect(api.get).toHaveBeenCalledOnce());
 expect(screen.queryByRole('button')).toBeNull(); expect(api.post).not.toHaveBeenCalled();
});
it('uses the same button for Docker or desktop and only restarts on a ready click', async () => {
 render(DesktopUpdateButton);
 const download = await screen.findByRole('button',{name:'Download update'});
 expect(download.title).toContain('Current worker');
 api.get.mockResolvedValue({...state,workers:[{...worker,phase:'ready'}]});
 await fireEvent.click(download);
 expect(api.post).toHaveBeenLastCalledWith('/settings/application-updates',{action:'download'});
 const restart = await screen.findByRole('button',{name:'Update and restart'});
 await fireEvent.click(restart);
 expect(api.post).toHaveBeenLastCalledWith('/settings/application-updates',{action:'install'});
});
it('uses all-worker scope without adding a second action button and reports partial outcomes', async () => {
 api.get.mockResolvedValue({...state,update_all_workers:true,workers:[worker,{...worker,device_id:'peer',device_name:'Peer',phase:'ready'}]});
 api.post.mockResolvedValue({results:[{device_name:'Current worker',accepted:true,reason:'Request queued'},{device_name:'Peer',accepted:false,reason:'Offline; not queued'}]});
 render(DesktopUpdateButton);
 const button = await screen.findByRole('button',{name:'Download update'});
 expect(button.title).toContain('All online workers'); expect(screen.getAllByRole('button')).toHaveLength(1);
 await fireEvent.click(button);
 await waitFor(()=>expect(button.title).toContain('Peer: Offline; not queued'));
});
it('blocks duplicate clicks and preserves transport failures for retry',async()=>{
 let reject!: (error:Error)=>void;
 api.post.mockReturnValue(new Promise((_,no)=>{reject=no;}));
 render(DesktopUpdateButton); const button=await screen.findByRole('button',{name:'Download update'});
 await fireEvent.click(button);await fireEvent.click(button);
 expect(api.post).toHaveBeenCalledOnce();expect(button.getAttribute('aria-disabled')).toBe('true');
 reject(new Error('Worker unavailable'));
 await waitFor(()=>expect(screen.getByRole('alert').textContent).toContain('Worker unavailable'));
 expect(button.getAttribute('aria-disabled')).toBe('false');
});
it('refreshes the saved scope immediately after the settings event', async()=>{
 render(DesktopUpdateButton);const button=await screen.findByRole('button',{name:'Download update'});
 api.get.mockResolvedValue({...state,update_all_workers:true});window.dispatchEvent(new Event('application-update-policy'));
 await waitFor(()=>expect(button.title).toContain('All online workers'));
});
it('pauses all actions when allow updates is disabled',async()=>{
 api.get.mockResolvedValue({...state,allow_updates:false});render(DesktopUpdateButton);
 await waitFor(()=>expect(api.get).toHaveBeenCalledOnce());expect(screen.queryByRole('button')).toBeNull();
});

const operation = { id: 'install-1', phase: 'waiting', workers: [
 {device_id:'local',device_name:'Desktop',status:'verified',message:'Updated and verified'},
 {device_id:'peer',device_name:'Worker 2',status:'waiting',message:'Waiting for startup…'}
] };
const settled = {...state,workers:[{...worker,phase:'idle',version:null}],operation:{...operation,phase:'complete',workers:operation.workers.map(w=>({...w,status:'verified',message:'Updated and verified'}))}};

it('restores waiting after reload instead of offering a stale ready update', async()=>{
 api.get.mockResolvedValue({...state,workers:[{...worker,phase:'ready'}],operation});
 const view=render(DesktopUpdateButton);
 let button=await screen.findByRole('button',{name:'Waiting on all workers to finish startup'});
 expect(button.title).toContain('Worker 2: Waiting for startup');
 await fireEvent.click(button);expect(api.post).not.toHaveBeenCalled();
 await fireEvent.focus(button);
 expect(screen.getByRole('tooltip').textContent).toContain('Worker 2');
 view.unmount();render(DesktopUpdateButton);
 button=await screen.findByRole('button',{name:'Waiting on all workers to finish startup'});
 expect(button.getAttribute('aria-disabled')).toBe('true');
 expect(screen.queryByRole('button',{name:'Update and restart'})).toBeNull();
});

it('pops once on completion, hides, and does not celebrate again after remount', async()=>{
 vi.useFakeTimers();
 try {
  api.get.mockResolvedValue({...state,operation});
  const view=render(DesktopUpdateButton);await vi.advanceTimersByTimeAsync(0);
  api.get.mockResolvedValue(settled);await vi.advanceTimersByTimeAsync(2000);
  const button=screen.getByRole('button',{name:'Update complete'});
  expect(button.parentElement?.classList.contains('complete')).toBe(true);
  expect(document.querySelectorAll('.spark')).toHaveLength(8);
  expect(localStorage.getItem('application-update-completed')).toBe('install-1');
  await vi.advanceTimersByTimeAsync(1850);
  expect(button.parentElement?.classList.contains('leaving')).toBe(true);
  await vi.advanceTimersByTimeAsync(300);expect(screen.queryByRole('button')).toBeNull();
  view.unmount();render(DesktopUpdateButton);await vi.advanceTimersByTimeAsync(0);
  expect(screen.queryByRole('button')).toBeNull();
 } finally { cleanup();vi.useRealTimers(); }
});

it('keeps waiting through transport loss without enabling another install', async()=>{
 vi.useFakeTimers();
 try {
  api.get.mockResolvedValue({...state,operation});render(DesktopUpdateButton);await vi.advanceTimersByTimeAsync(0);
  api.get.mockRejectedValue(new Error('restarting'));await vi.advanceTimersByTimeAsync(6000);
  const button=screen.getByRole('button',{name:'Waiting on all workers to finish startup'});
  await fireEvent.click(button);expect(api.post).not.toHaveBeenCalled();
 } finally { cleanup();vi.useRealTimers(); }
});

it('shows startup failures for retry without a success animation', async()=>{
 api.get.mockResolvedValue({...state,workers:[{...worker,phase:'ready'}],operation:{...operation,phase:'error'}});
 render(DesktopUpdateButton);const button=await screen.findByRole('button',{name:'Update and restart'});
 expect(button.title).toContain('startup could not be confirmed');
 expect(document.querySelector('.complete')).toBeNull();
 await fireEvent.click(button);expect(api.post).toHaveBeenCalledWith('/settings/application-updates',{action:'install'});
});

it('suppresses stale updater availability for the running revision, but shows a newer update', async()=>{
 localStorage.setItem('application-update-completed','install-1');
 api.get.mockResolvedValue({...settled,workers:[{...worker,phase:'ready',version:'bbbbbbbbbbbb',running_version:'b'.repeat(40)}]});
 render(DesktopUpdateButton);await waitFor(()=>expect(api.get).toHaveBeenCalledOnce());
 expect(screen.queryByRole('button')).toBeNull();
 api.get.mockResolvedValue({...settled,workers:[{...worker,version:'cccccccccccc',running_version:'b'.repeat(40)}]});
 window.dispatchEvent(new Event('application-update-policy'));
 expect(await screen.findByRole('button',{name:'Download update'})).toBeTruthy();
});

it('cleans up animation timers on unmount', async()=>{
 vi.useFakeTimers();
 try {
  api.get.mockResolvedValue(settled);const view=render(DesktopUpdateButton);await vi.advanceTimersByTimeAsync(0);
  expect(screen.getByRole('button',{name:'Update complete'})).toBeTruthy();
  view.unmount();expect(vi.getTimerCount()).toBe(0);
 } finally { cleanup();vi.useRealTimers(); }
});

it('shows the offered release name and its signed notes in the update tooltip', async()=>{
 api.get.mockResolvedValue({...state,workers:[{...worker,release:'0.9.0',notes:'- Faster startup\n- Fixes the sidebar'}]});
 render(DesktopUpdateButton);const button=await screen.findByRole('button',{name:'Download update'});
 await fireEvent.mouseEnter(button);
 const tip=await screen.findByRole('tooltip');
 expect(tip.textContent).toContain("What's new in 0.9.0");
 expect(tip.querySelector('.notes-text')?.textContent).toBe('- Faster startup\n- Fixes the sidebar');
});

it('shows no notes section when the offered update has none', async()=>{
 render(DesktopUpdateButton);const button=await screen.findByRole('button',{name:'Download update'});
 await fireEvent.mouseEnter(button);
 expect((await screen.findByRole('tooltip')).querySelector('.release-notes')).toBeNull();
});

it('polls once a minute while nothing is moving, and every 2 s while an install is verified', async()=>{
 vi.useFakeTimers();
 try {
  api.get.mockResolvedValue(settled);render(DesktopUpdateButton);await vi.advanceTimersByTimeAsync(0);
  expect(api.get).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(10_000);expect(api.get).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(50_000);expect(api.get).toHaveBeenCalledTimes(2);
  api.get.mockResolvedValue({...state,operation});
  await vi.advanceTimersByTimeAsync(60_000);expect(api.get).toHaveBeenCalledTimes(3);
  await vi.advanceTimersByTimeAsync(4000);expect(api.get).toHaveBeenCalledTimes(5);
 } finally { cleanup();vi.useRealTimers(); }
});

it('polls quickly after a click until the requested work shows up', async()=>{
 vi.useFakeTimers();
 try {
  render(DesktopUpdateButton);await vi.advanceTimersByTimeAsync(0);
  await fireEvent.click(screen.getByRole('button',{name:'Download update'}));await vi.advanceTimersByTimeAsync(0);
  const afterClick=api.get.mock.calls.length;
  await vi.advanceTimersByTimeAsync(10_000);expect(api.get.mock.calls.length).toBeGreaterThanOrEqual(afterClick+4);
  await vi.advanceTimersByTimeAsync(60_000);const settledCount=api.get.mock.calls.length;
  await vi.advanceTimersByTimeAsync(30_000);expect(api.get.mock.calls.length).toBe(settledCount);
 } finally { cleanup();vi.useRealTimers(); }
});

it('stops polling while the window is hidden and refreshes when it returns', async()=>{
 vi.useFakeTimers();
 let hidden=false;
 const spy=vi.spyOn(document,'hidden','get').mockImplementation(()=>hidden);
 try {
  api.get.mockResolvedValue({...state,operation});render(DesktopUpdateButton);await vi.advanceTimersByTimeAsync(0);
  hidden=true;document.dispatchEvent(new Event('visibilitychange'));
  const before=api.get.mock.calls.length;
  await vi.advanceTimersByTimeAsync(120_000);expect(api.get.mock.calls.length).toBe(before);
  hidden=false;document.dispatchEvent(new Event('visibilitychange'));await vi.advanceTimersByTimeAsync(0);
  expect(api.get.mock.calls.length).toBe(before+1);
 } finally { spy.mockRestore();cleanup();vi.useRealTimers(); }
});
