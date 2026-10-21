import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api, getErrorMessage: (error: Error) => error.message }));
const updateAuth = vi.hoisted(() => ({current: {status: {preview_access: false}}}));
vi.mock('$lib/stores/auth.svelte', () => ({auth: updateAuth}));
const notice = vi.hoisted(() => ({ info: vi.fn(), warning: vi.fn(), error: vi.fn() }));
vi.mock('$lib/stores/notifications', () => ({ notify: notice }));
import DesktopUpdateButton from './DesktopUpdateButton.svelte';
import DesktopUpdateButtonHarness from '$lib/test-stubs/DesktopUpdateButtonHarness.svelte';
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
// The app restarted into this install a moment ago, so a fresh mount may celebrate it.
const justSettled = {...settled,server_time:1_000_030,operation:{...settled.operation,completed_at:1_000_000}};

it('restores waiting after reload instead of offering a stale ready update', async()=>{
 api.get.mockResolvedValue({...state,workers:[{...worker,phase:'ready'}],operation});
 const view=render(DesktopUpdateButton);
 let button=await screen.findByRole('button',{name:'Waiting on all workers to finish startup'});
 expect(button.title).toContain('Worker 2: Waiting for startup');
 // A first start after an update can take minutes: say so rather than look hung.
 expect(button.title).toContain('Starting the new version can take a few minutes.');
 await fireEvent.click(button);expect(api.post).not.toHaveBeenCalled();
 await fireEvent.focus(button);
 expect(screen.getByRole('tooltip').textContent).toContain('Worker 2');
 expect(screen.getByRole('tooltip').textContent).toContain('can take a few minutes');
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

it('offers a newly staged update plainly once the worker settled an old failed install as replaced', async()=>{
 // app/core/update_progress.py settles a failed install whose worker staged a
 // different update: complete, with nothing verified, so nothing celebrates.
 const replaced={...operation,phase:'complete',workers:operation.workers.map(w=>({...w,status:'replaced',message:'Update 24b1105c7276 replaces this one'}))};
 api.get.mockResolvedValue({...state,workers:[{...worker,phase:'ready',version:'24b1105c7276'}],operation:replaced});
 render(DesktopUpdateButton);const button=await screen.findByRole('button',{name:'Update and restart'});
 expect(button.classList.contains('failed')).toBe(false);expect(button.classList.contains('ready')).toBe(true);
 expect(button.title).not.toContain('startup could not be confirmed');expect(button.title).toContain('Update ready');
 expect(document.querySelector('.complete')).toBeNull();
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
  api.get.mockResolvedValue(justSettled);const view=render(DesktopUpdateButton);await vi.advanceTimersByTimeAsync(0);
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

// #1623: readiness aggregates over every included worker, each row shows its real phase.
const peer = {...worker,device_id:'peer',device_name:'ServerWorker'};
it('waits for every included worker before offering restart and labels each phase', async()=>{
 api.get.mockResolvedValue({...state,update_all_workers:true,workers:[{...worker,phase:'ready'},{...peer,phase:'downloading',message:'Building frontend…'}]});
 render(DesktopUpdateButton);
 const button=await screen.findByRole('button',{name:'Downloading update'});
 expect(button.getAttribute('aria-disabled')).toBe('true');
 expect(button.title).toContain('1 of 2 staged');
 expect(button.title).toContain('Current worker: Staged, awaiting restart');
 expect(button.title).toContain('ServerWorker: Building frontend…');
 await fireEvent.click(button);expect(api.post).not.toHaveBeenCalled();
 api.get.mockResolvedValue({...state,update_all_workers:true,workers:[{...worker,phase:'ready'},{...peer,phase:'checking'}]});
 window.dispatchEvent(new Event('application-update-policy'));
 await waitFor(()=>expect(button.title).toContain('ServerWorker: Checking for updates…'));
 expect(screen.queryByRole('button',{name:'Update and restart'})).toBeNull();
 api.get.mockResolvedValue({...state,update_all_workers:true,workers:[{...worker,phase:'ready'},{...peer,phase:'ready',message:'Update ready. Choose Update and restart.'}]});
 window.dispatchEvent(new Event('application-update-policy'));
 const restart=await screen.findByRole('button',{name:'Update and restart'});
 expect(restart.title).toContain('ServerWorker: Staged, awaiting restart');
 expect(restart.title).not.toContain('running');
 await fireEvent.focus(restart);
 const rows=[...screen.getByRole('tooltip').querySelectorAll('.worker-row')].map(row=>row.textContent);
 expect(rows).toEqual(['Current workerStaged, awaiting restart','ServerWorkerStaged, awaiting restart']);
});
it('lists excluded workers with a reason without counting them ready or letting them block', async()=>{
 api.get.mockResolvedValue({...state,update_all_workers:true,workers:[
  {...worker,phase:'ready'},
  {...peer,device_id:'off',device_name:'Laptop',online:false,supported:false,phase:'unsupported',reason:'Offline; not queued'},
  {...peer,device_id:'cur',device_name:'Desk',phase:'ready',version:'bbbbbbbbbbbb',running_version:'b'.repeat(40)},
  {...peer,device_id:'old',device_name:'Old',supported:false,phase:'unsupported',version:null},
  {...peer,device_id:'off2',device_name:'Locked',allow_updates:false,phase:'available'},
  {...peer,device_id:'idle',device_name:'Idle',phase:'idle',version:null},
  // The container updater names the running revision on this phase (#1901).
  {...peer,device_id:'img',device_name:'Docker',phase:'image-required',message:null,version:'bbbbbbbbbbbb',running_version:'b'.repeat(40)},
 ]});
 render(DesktopUpdateButton);
 const button=await screen.findByRole('button',{name:'Update and restart'});
 for (const text of ['Laptop: Offline; not queued','Desk: Already up to date','Old: No application updater; not included',
  'Locked: Updates disabled on this worker; not included','Idle: No update offered','Docker: Needs a new Docker image; not included']) expect(button.title).toContain(text);
 expect(button.title).not.toContain('Docker: Already up to date');
});
it.each([
 ['an unreadable status',{phase:'unknown',supported:false,reason:'Worker unavailable or does not support application updates'},'ServerWorker (Status unavailable)'],
 ['a failed check',{phase:'error',version:null,error:'Feed unreachable'},'Feed unreachable'],
])('never offers restart while an included worker has %s', async(_label,change,text)=>{
 api.get.mockResolvedValue({...state,update_all_workers:true,workers:[{...worker,phase:'ready'},{...peer,...change}]});
 render(DesktopUpdateButton);
 const button=await screen.findByRole('button',{name:'Check for updates'});
 expect(button.title).toContain(text);
 expect(screen.getByRole('alert').textContent).toContain(text);
 await fireEvent.click(button);
 expect(api.post).toHaveBeenCalledWith('/settings/application-updates',{action:'check'});
});
it('does not keep showing accepted requests once workers report their phase', async()=>{
 render(DesktopUpdateButton);
 const button=await screen.findByRole('button',{name:'Download update'});
 api.get.mockResolvedValue({...state,workers:[{...worker,phase:'ready'}]});
 await fireEvent.click(button);
 const restart=await screen.findByRole('button',{name:'Update and restart'});
 expect(restart.title).not.toContain('Request queued');
 expect(restart.title).toContain('Current worker: Staged, awaiting restart');
});

// #1623: a reload while staged must not replay a historical completion.
const history = {...settled,server_time:2_000_000,operation:{...settled.operation,completed_at:1_000_000}};
it.each([
 ['an old completion', history],
 ['a completion from an older worker without a timestamp', settled],
 ['a recent completion', justSettled],
])('reloading with fresh storage while staged skips %s and shows restart', async(_label,snap)=>{
 vi.useFakeTimers();
 try {
  api.get.mockResolvedValue({...snap,workers:[{...worker,phase:'ready',version:'cccccccccccc',running_version:'b'.repeat(40)}]});
  render(DesktopUpdateButton);await vi.advanceTimersByTimeAsync(0);
  expect(document.querySelector('.complete')).toBeNull();
  expect(screen.queryByRole('button',{name:'Update complete'})).toBeNull();
  expect(screen.getByRole('button',{name:'Update and restart'})).toBeTruthy();
  expect(localStorage.getItem('application-update-completed')).toBe('install-1');
  await vi.advanceTimersByTimeAsync(2500);
  expect(document.querySelector('.complete')).toBeNull();
  expect(screen.getByRole('button',{name:'Update and restart'})).toBeTruthy();
 } finally { cleanup();vi.useRealTimers(); }
});
it.each([['an old completion', history], ['an untimed completion', settled]])('a fresh browser never celebrates %s', async(_label,snap)=>{
 api.get.mockResolvedValue(snap);
 render(DesktopUpdateButton);await waitFor(()=>expect(api.get).toHaveBeenCalledOnce());
 expect(screen.queryByRole('button')).toBeNull();
 expect(localStorage.getItem('application-update-completed')).toBe('install-1');
});
it('celebrates a just-finished install once after the app restarts into it', async()=>{
 localStorage.setItem('application-update-completed','install-0');
 api.get.mockResolvedValue(justSettled);
 const view=render(DesktopUpdateButton);
 expect(await screen.findByRole('button',{name:'Update complete'})).toBeTruthy();
 view.unmount();render(DesktopUpdateButton);await waitFor(()=>expect(api.get).toHaveBeenCalledTimes(2));
 expect(screen.queryByRole('button',{name:'Update complete'})).toBeNull();
});
it('does not let a peer outside a current-worker update hold or join the restart', async()=>{
 // A peer read only to verify an earlier all-worker install stays out of a current-worker update.
 api.get.mockResolvedValue({...state,workers:[{...worker,is_self:true,phase:'ready'},{...peer,is_self:false,phase:'unknown',supported:false}]});
 render(DesktopUpdateButton);
 const button=await screen.findByRole('button',{name:'Update and restart'});
 expect(button.title).not.toContain('Restart waits');
 expect(button.title).toContain('ServerWorker: Not in scope; current worker only');
});
it('never celebrates a recent completion while an update is staged but held for an unknown worker', async()=>{
 api.get.mockResolvedValue({...justSettled,update_all_workers:true,workers:[{...worker,phase:'ready',version:'cccccccccccc',running_version:'b'.repeat(40)},{...peer,phase:'unknown',supported:false}]});
 render(DesktopUpdateButton);
 const button=await screen.findByRole('button',{name:'Check for updates'});
 expect(button.title).toContain('Restart waits for ServerWorker');
 expect(document.querySelector('.complete')).toBeNull();
 expect(screen.queryByRole('button',{name:'Update complete'})).toBeNull();
});

// #1623 live test: a peer that refused the fanned-out request was left idle and
// the button offered restart from the initiating worker's state alone.
it('names a worker that refused the last request and holds restart for it', async()=>{
 api.get.mockResolvedValue({...state,update_all_workers:true,workers:[{...worker},{...peer,phase:'idle',version:null}]});
 api.post.mockResolvedValue({results:[
  {device_id:'peer',device_name:'ServerWorker',accepted:false,reason:'No application update available; current version left running'},
  {device_id:'local',device_name:'Current worker',accepted:true,reason:'Request queued'}]});
 render(DesktopUpdateButton);
 const download=await screen.findByRole('button',{name:'Download update'});
 api.get.mockResolvedValue({...state,update_all_workers:true,workers:[{...worker,phase:'ready'},{...peer,phase:'idle',version:null}]});
 await fireEvent.click(download);
 const button=await screen.findByRole('button',{name:'Check for updates'});
 expect(screen.queryByRole('button',{name:'Update and restart'})).toBeNull();
 expect(button.title).toContain('Restart waits for ServerWorker (Refused: No application update available');
 expect(screen.getByRole('alert').textContent).toContain('Refused: No application update available');
 await fireEvent.focus(button);
 const rows=[...screen.getByRole('tooltip').querySelectorAll('.worker-row')].map(row=>row.textContent);
 expect(rows).toContain('ServerWorkerRefused: No application update available; current version left running');
 // A check that reaches the peer clears the refusal; the peer then reports its own phase.
 api.post.mockResolvedValue({results:[{device_id:'peer',device_name:'ServerWorker',accepted:true,reason:'Request queued'},
  {device_id:'local',device_name:'Current worker',accepted:true,reason:'Already staged'}]});
 api.get.mockResolvedValue({...state,update_all_workers:true,workers:[{...worker,phase:'ready'},{...peer,phase:'available',version:'cccccccccccc'}]});
 await fireEvent.click(button);
 expect(api.post).toHaveBeenLastCalledWith('/settings/application-updates',{action:'check'});
 const next=await screen.findByRole('button',{name:'Download update'});
 expect(next.title).not.toContain('Refused');
});
it('counts a worker with a queued request it has not taken yet as preparing', async()=>{
 api.get.mockResolvedValue({...state,update_all_workers:true,workers:[{...worker,phase:'ready'},{...peer,phase:'idle',version:null,request_pending:true}]});
 render(DesktopUpdateButton);
 const button=await screen.findByRole('button',{name:'Downloading update'});
 expect(button.getAttribute('aria-disabled')).toBe('true');
 expect(button.title).toContain('ServerWorker: Request queued; waiting for the updater');
 expect(screen.queryByRole('button',{name:'Update and restart'})).toBeNull();
});
it('ignores an idle message that claims an update is in progress', async()=>{
 api.get.mockResolvedValue({...state,update_all_workers:true,workers:[{...worker,phase:'ready'},
  {...peer,phase:'idle',version:null,message:'Application update is running.'},
  {...peer,device_id:'desk',device_name:'Desk',phase:'idle',version:null,message:'Updated version is running.'}]});
 render(DesktopUpdateButton);
 const button=await screen.findByRole('button',{name:'Update and restart'});
 expect(button.title).toContain('ServerWorker: No update offered');
 expect(button.title).not.toContain('Application update is running');
 expect(button.title).toContain('Desk: Updated version is running.');
});

// #1623: a staged desktop offered restart while a peer that had not checked yet sat idle.
const desktopStaged = { ...worker, phase:'ready', version:'b'.repeat(12), running_version:'a'.repeat(40), checked_at: 2_000 };
const idlePeer = { ...worker, device_id:'peer', device_name:'ServerWorker', is_self:false, phase:'idle', version:null, running_version:'a'.repeat(40) };
it.each([
 ['never reported a check', { checked_at: null }, 'Download update'],
 ['checked before the update was found', { checked_at: 1_000 }, 'Download update'],
 ['checked after and found nothing', { checked_at: 3_000 }, 'Update and restart'],
 ['predates checked_at', {}, 'Update and restart'],
])('fallback: an idle peer that %s', async (_name, peer, action) => {
 api.get.mockResolvedValue({ ...state, update_all_workers:true, workers:[desktopStaged, { ...idlePeer, ...peer }] });
 render(DesktopUpdateButton);
 await screen.findByRole('button', { name: action });
});
it('uses the worker’s own role and label when the snapshot carries them', async () => {
 api.get.mockResolvedValue({ ...state, update_all_workers:true, workers:[
  { ...desktopStaged, role:'staged', label:'Staged, awaiting restart' },
  // The server's verdict wins even where the fallback would read it differently.
  { ...idlePeer, checked_at: 3_000, role:'available', label:'Not checked yet; will check and download' },
 ] });
 render(DesktopUpdateButton);
 const button = await screen.findByRole('button', { name: 'Download update' });
 expect(button.getAttribute('title')).toContain('ServerWorker: Not checked yet; will check and download');
 await fireEvent.click(button);
 await waitFor(() => expect(api.post).toHaveBeenCalledExactlyOnceWith('/settings/application-updates', { action:'download' }));
});

// #1623: the sidebar's More menu offers a manual check through the button.
describe('manual check from the host', () => {
 const idle = { ...state, workers:[{ ...worker, phase:'idle', version:null }] };
 const canCheck = () => screen.getByTestId('can-check').textContent;
 it('tells the host a check makes sense only while updates are allowed', async () => {
  api.get.mockResolvedValue(idle);
  render(DesktopUpdateButtonHarness);
  await waitFor(() => expect(canCheck()).toBe('yes'));
  cleanup();
  api.get.mockResolvedValue({ ...idle, allow_updates:false });
  render(DesktopUpdateButtonHarness);
  await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2));
  expect(canCheck()).toBe('no');
 });
 it('never offers the check from an agent preview', async () => {
  updateAuth.current.status.preview_access = true;
  render(DesktopUpdateButtonHarness);
  await Promise.resolve();
  expect(canCheck()).toBe('no');
  expect(api.get).not.toHaveBeenCalled();
 });
 it('asks every worker in scope to check and says so', async () => {
  api.get.mockResolvedValue(idle);
  render(DesktopUpdateButtonHarness);
  await waitFor(() => expect(canCheck()).toBe('yes'));
  await fireEvent.click(screen.getByRole('button', { name:'Manual check' }));
  await waitFor(() => expect(api.post).toHaveBeenCalledExactlyOnceWith('/settings/application-updates', { action:'check' }));
  expect(notice.info).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({ title:'Checking for updates' }));
 });
 it('reports an update already on offer instead of checking again', async () => {
  render(DesktopUpdateButtonHarness);
  await screen.findByRole('button', { name:'Download update' });
  await fireEvent.click(screen.getByRole('button', { name:'Manual check' }));
  await waitFor(() => expect(notice.info).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({ title:'Update available' })));
  expect(api.post).not.toHaveBeenCalled();
 });
 it('names a refused check rather than claiming it started', async () => {
  api.get.mockResolvedValue(idle);
  api.post.mockResolvedValue({ results:[{ device_name:'Current worker', accepted:false, reason:'Updater offline' }] });
  render(DesktopUpdateButtonHarness);
  await waitFor(() => expect(canCheck()).toBe('yes'));
  await fireEvent.click(screen.getByRole('button', { name:'Manual check' }));
  await waitFor(() => expect(notice.warning).toHaveBeenCalledWith(expect.stringContaining('Updater offline'), expect.objectContaining({ title:'Update check needs attention' })));
 });
});

// #1818: a restart refused because work is running says why at once.
const busyReceipt = {results:[{device_id:'local',device_name:'Current worker',accepted:false,reason:'Busy with running work: Fix login',
 busy:[{session_id:'s1',title:'Fix login',tasks:['Run tests']}]}]};
it('shows the reason, the running work and both actions when a restart is refused', async () => {
 api.get.mockResolvedValue({...state, workers:[{...worker,phase:'ready'}]});
 api.post.mockResolvedValue(busyReceipt);
 render(DesktopUpdateButton);
 await fireEvent.click(await screen.findByRole('button',{name:'Update and restart'}));
 const panel = await screen.findByRole('dialog',{name:'Work is still running'});
 expect(panel.textContent).toContain('Restart would stop it.');
 expect(panel.textContent).toContain('Fix login');
 expect(panel.textContent).toContain('Run tests');
 expect(notice.warning).toHaveBeenCalledWith(expect.stringContaining('Fix login (Run tests)'),
  expect.objectContaining({title:'Restart is waiting', action:expect.objectContaining({label:'Restart when work finishes'}),
   secondaryAction:expect.objectContaining({label:'Stop running work and restart'})}));
 api.post.mockResolvedValue({results:[{device_name:'Current worker',accepted:true,reason:'Restarts when running work finishes',deferred:true}]});
 await fireEvent.click(screen.getByRole('button',{name:'Restart when work finishes'}));
 expect(api.post).toHaveBeenLastCalledWith('/settings/application-updates',{action:'install',mode:'when_idle'});
 await waitFor(()=>expect(notice.info).toHaveBeenCalledWith('Restarts when running work finishes.',expect.objectContaining({title:'Restart scheduled'})));
 expect(screen.queryByRole('dialog')).toBeNull();
});
it('stops the running work and restarts from the bell action', async () => {
 api.get.mockResolvedValue({...state, workers:[{...worker,phase:'ready'}]});
 api.post.mockResolvedValue(busyReceipt);
 render(DesktopUpdateButton);
 await fireEvent.click(await screen.findByRole('button',{name:'Update and restart'}));
 await waitFor(()=>expect(notice.warning).toHaveBeenCalled());
 api.post.mockResolvedValue({results:[{device_name:'Current worker',accepted:true,reason:'Stopping running work, then restarting'}]});
 notice.warning.mock.calls.at(-1)![1].secondaryAction.onClick();
 await waitFor(()=>expect(api.post).toHaveBeenLastCalledWith('/settings/application-updates',{action:'install',mode:'stop'}));
});
it('offers to cancel a restart that waits for running work', async () => {
 api.get.mockResolvedValue({...state, workers:[{...worker,phase:'ready',role:'staged',label:'Restarts when work finishes',deferred:true}]});
 render(DesktopUpdateButton);
 await fireEvent.click(await screen.findByRole('button',{name:'Restarts when work finishes'}));
 const panel = await screen.findByRole('dialog',{name:'Restart scheduled'});
 expect(api.post).not.toHaveBeenCalled();
 await fireEvent.click(screen.getByRole('button',{name:'Cancel schedule'}));
 expect(api.post).toHaveBeenLastCalledWith('/settings/application-updates',{action:'cancel'});
 await waitFor(()=>expect(panel.isConnected).toBe(false));
});
it('tells a Docker worker ahead of time that a coming update needs a newer image, and still updates it', async()=>{
 const coming='A coming update needs a newer Docker image.';
 api.get.mockResolvedValue({...state,update_all_workers:true,workers:[
  {...worker,phase:'ready'},
  {...peer,device_id:'dock',device_name:'Docker',phase:'ready',notice:coming},
  {...peer,device_id:'img',device_name:'Old',phase:'image-required',message:null,notice:coming},
 ]});
 render(DesktopUpdateButton);
 const button=await screen.findByRole('button',{name:'Update and restart'});
 expect(button.title).toContain('Docker: Staged, awaiting restart; a coming update needs a newer Docker image');
 // A worker that needs it now says so once, not twice.
 expect(button.title).toContain('Old: Needs a new Docker image; not included');
 expect(button.title).not.toContain('Old: Needs a new Docker image; not included; a coming');
});

// A failed desktop update that only the latest installer can finish says so,
// links the installer, and never retries in the app (remedy from update_workers.py).
const NO_BUILD='No healthy source build is available. Install the latest AI Shuffle installer to recover. Previous files and data have been retained.';
it.each([true,false])('offers the latest installer instead of a retry (data kept=%s)', async kept => {
 api.get.mockResolvedValue({...state, workers:[{...worker,phase:'error',version:null,error:NO_BUILD,role:'unknown',label:NO_BUILD,remedy:'installer',data_kept:kept}]});
 const open=vi.spyOn(window,'open').mockReturnValue(null);
 try {
  render(DesktopUpdateButton);
  await fireEvent.click(await screen.findByRole('button',{name:'Update needs the latest installer'}));
  const panel=await screen.findByRole('dialog',{name:'Update needs the latest installer'});
  expect(panel.textContent).toContain('This update cannot finish inside the app.');
  expect(panel.textContent).toContain(NO_BUILD);
  expect(panel.textContent?.includes('Your data is kept as it was before the update.')).toBe(kept);
  const link=screen.getByRole('link',{name:'Download the latest installer'});
  expect(link.getAttribute('href')).toBe('https://github.com/HobbyCoders/ai-shuffle/releases/latest');
  expect(link.getAttribute('target')).toBe('_blank');
  expect(api.post).not.toHaveBeenCalled();
  // The desktop's ready notification asks the shared control; it opens the same panel.
  await fireEvent.click(screen.getByRole('button',{name:'Not now'}));
  window.dispatchEvent(new Event('application-update-request'));
  await screen.findByRole('dialog',{name:'Update needs the latest installer'});
  expect(api.post).not.toHaveBeenCalled();
  expect(open).not.toHaveBeenCalled();
 } finally { open.mockRestore(); }
});
// A failed first start whose installer is not newer than the running version
// (remedy null from update_workers.py): no dead-end installer link, a retry instead.
const FAILED_START='This revision failed to start. Check again after a newer update. Previous files and data have been retained.';
it('offers a retry, not the installer, when the installer would reinstall the same version', async () => {
 api.get.mockResolvedValue({...state, workers:[{...worker,phase:'error',version:null,error:FAILED_START,role:'unknown',label:FAILED_START,remedy:null,data_kept:false}]});
 render(DesktopUpdateButton);
 const button=await screen.findByRole('button',{name:'Check for updates'});
 expect(button.title).toContain(FAILED_START);
 expect(screen.queryByRole('button',{name:'Update needs the latest installer'})).toBeNull();
 await fireEvent.click(button);
 await waitFor(()=>expect(api.post).toHaveBeenCalledExactlyOnceWith('/settings/application-updates',{action:'check'}));
 expect(screen.queryByRole('link',{name:'Download the latest installer'})).toBeNull();
});
// The desktop offers the put-back update again and says why (its message is the row's label).
it('shows why a failed first start is offered again', async () => {
 const why='The last attempt to start this update did not finish, and AI Shuffle went back to the previous version with your data as it was. Download it to try again.';
 api.get.mockResolvedValue({...state, workers:[{...worker,phase:'available',message:why,role:'available',label:why}]});
 render(DesktopUpdateButton);
 const button=await screen.findByRole('button',{name:'Download update'});
 expect(button.title).toContain(why);
});
it('puts the installer action on a manual check that finds the installer state', async () => {
 vi.useFakeTimers({shouldAdvanceTime:true});
 try {
  api.get.mockResolvedValue({...state, workers:[{...worker,phase:'idle',version:null}]});
  render(DesktopUpdateButtonHarness);
  await waitFor(()=>expect(screen.getByTestId('can-check').textContent).toBe('yes'));
  api.get.mockResolvedValue({...state, workers:[{...worker,phase:'error',version:null,error:NO_BUILD,role:'unknown',label:NO_BUILD,remedy:'installer',data_kept:true}]});
  await fireEvent.click(screen.getByRole('button',{name:'Manual check'}));
  await vi.advanceTimersByTimeAsync(6000);
  await waitFor(()=>expect(notice.warning).toHaveBeenCalledWith(expect.stringContaining('Download the latest installer to finish it. Your data is kept'),
   expect.objectContaining({title:'Update check needs attention',action:expect.objectContaining({label:'Download the latest installer'})})));
 } finally { vi.useRealTimers(); }
});

// #344: restart waiting state, aggregate outcome and not-sent receipts.
describe('restart waiting state', () => {
 const svc = {session_id:'s1',title:'Fix login',kind:'service',task_id:'t1',label:'npm run preview -- --port 5173',port:5173,
  started_at:'2026-10-21T13:02:00Z',live:true,liveness:'live',running_turn:false,turn_ended_at:'2026-10-21T13:10:00Z',
  stop_will_stop:true,monitor_serving_idle_seconds:0,idle_stop_at:null};
 const ready = {...worker,phase:'ready',staged_version:'abc123',monitor_serving_idle_seconds:0};
 it('opens what still runs instead of sending a restart that would be refused, then stops it on request', async () => {
  api.get.mockResolvedValue({...state,workers:[{...ready,install_state:'blocked',wait_reason:'running_work',blocked_by:[svc]}]});
  render(DesktopUpdateButton);
  const button = await screen.findByRole('button',{name:'Ready; waiting for running work'});
  expect(button.title).toContain('Service npm run preview -- --port 5173 still running for Fix login on port 5173');
  await fireEvent.click(button);
  const panel = await screen.findByRole('dialog',{name:'Work is still running'});
  expect(panel.textContent).toContain('Downloaded and ready.');
  expect(panel.textContent).toContain('No known automatic stop time.');
  expect(api.post).not.toHaveBeenCalled();
  expect(screen.queryByRole('button',{name:/force/i})).toBeNull();
  api.post.mockResolvedValue({results:[{device_name:'Current worker',accepted:true,reason:'Stopping running work, then restarting'}]});
  await fireEvent.click(screen.getByRole('button',{name:'Stop running work and restart'}));
  expect(api.post).toHaveBeenCalledExactlyOnceWith('/settings/application-updates',{action:'install',mode:'stop'});
 });
 it('disables duplicate stop and restart while stopping', async () => {
  api.get.mockResolvedValue({...state,workers:[{...ready,install_state:'stopping',wait_reason:'stopping_work',blocked_by:[svc]}]});
  render(DesktopUpdateButton);
  const button = await screen.findByRole('button',{name:'Stopping running work…'});
  // Still operable (it opens the state); busy, not disabled.
  expect(button.getAttribute('aria-disabled')).toBe('false');
  expect(button.getAttribute('aria-busy')).toBe('true');
  await fireEvent.click(button);
  const panel = await screen.findByRole('dialog',{name:'Stopping running work'});
  expect(panel.textContent).toContain('Stop requested. Stopping running work, then requesting restart…');
  expect((screen.getByRole('button',{name:'Stop running work and restart'}) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole('button',{name:'Restart when work finishes'}) as HTMLButtonElement).disabled).toBe(true);
  expect(api.post).not.toHaveBeenCalled();
 });
 it('treats a queued restart as a delivery wait and sends nothing more', async () => {
  api.get.mockResolvedValue({...state,workers:[{...ready,install_state:'queued',wait_reason:'updater_acknowledgement',blocked_by:[]}]});
  render(DesktopUpdateButton);
  const button = await screen.findByRole('button',{name:'Restart requested'});
  expect(button.title).toContain('Restart requested; waiting for the updater…');
  await fireEvent.click(button);
  expect(api.post).not.toHaveBeenCalled();
 });
 it('shows a scheduled restart with its expiry and cancels the schedule', async () => {
  api.get.mockResolvedValue({...state,workers:[{...ready,install_state:'waiting_for_idle',wait_reason:'running_work',blocked_by:[svc],restart_expires_at:1_800_000_000,deferred:true}]});
  render(DesktopUpdateButton);
  await fireEvent.click(await screen.findByRole('button',{name:'Restarts when work finishes'}));
  const panel = await screen.findByRole('dialog',{name:'Restart scheduled'});
  expect(panel.textContent).toContain('Will restart when running work finishes.');
  expect(panel.textContent).toMatch(/Waits until .+ at most/);
  expect(screen.queryByRole('button',{name:'Restart when work finishes'})).toBeNull();
  await fireEvent.click(screen.getByRole('button',{name:'Cancel schedule'}));
  expect(api.post).toHaveBeenLastCalledWith('/settings/application-updates',{action:'cancel'});
 });
 it('says what is still running after a stop attempt, with its reason', async () => {
  api.get.mockResolvedValue({...state,workers:[{...ready,install_state:'blocked',wait_reason:'still_busy_after_stop',blocked_by:[svc],
   stop_result:{request_id:'r',accepted:false,reason:'Busy with running work: Fix login',completed_at:1_790_000_000}}]});
  render(DesktopUpdateButton);
  const button = await screen.findByRole('button',{name:'Ready; waiting for running work'});
  expect(button.title).toContain('The stop attempt finished, but work is still running. Restart was not queued. Service npm run preview -- --port 5173 still running');
  // Named once, by the blocker line; the busy refusal does not repeat it.
  expect(button.title.match(/npm run preview/g)).toHaveLength(1);
 });
 it('reports a not-sent request with the worker named once', async () => {
  api.get.mockResolvedValue({...state,workers:[{...ready,install_state:'blocked',wait_reason:'running_work',blocked_by:[svc]}]});
  api.post.mockResolvedValue({scope:'current',sent:null,action:'install',mode:'when_idle',request_state:'not_sent',results:[
   {device_id:'local',device_name:'Current worker',accepted:false,request_state:'not_sent',mode:'when_idle',reason:'Waiting on workers to finish the previous update'}]});
  render(DesktopUpdateButton);
  await fireEvent.click(await screen.findByRole('button',{name:'Ready; waiting for running work'}));
  await fireEvent.click(await screen.findByRole('button',{name:'Restart when work finishes'}));
  await waitFor(()=>expect(notice.warning).toHaveBeenCalledWith('Not sent to Current worker: Waiting on workers to finish the previous update.',expect.objectContaining({title:'Restart was not sent'})));
  await waitFor(()=>expect(screen.getByRole('button').title).toContain('Last request: Not sent to Current worker: Waiting on workers to finish the previous update.'));
  expect(screen.getByRole('button').title).not.toContain('Refused');
 });
 it('names a retained download behind a newer offer without the offer’s release name', async () => {
  api.get.mockResolvedValue({...state,workers:[{...worker,phase:'available',version:'b'.repeat(40),staged_version:'a'.repeat(40),
   install_state:'unavailable',wait_reason:'newer_offer',release:'2.0.0',notes:'- New things',role:'available',label:'Update available; not downloaded'}]});
  render(DesktopUpdateButton);
  const button = await screen.findByRole('button',{name:'Download update'});
  await fireEvent.focus(button);
  const tip = screen.getByRole('tooltip');
  expect(tip.textContent).toContain('Downloaded aaaaaaaaaaaa is retained. A newer update is available; download it to continue.');
  expect(tip.textContent).not.toMatch(/aaaaaaaaaaaa[^.]*2\.0\.0/);
 });
 it('never wears another offer’s notes on a staged build', async () => {
  api.get.mockResolvedValue({...state,workers:[{...ready,version:'b'.repeat(40),staged_version:'a'.repeat(40),install_state:'ready',release:'2.0.0',notes:'- Other offer'}]});
  render(DesktopUpdateButton);
  await fireEvent.focus(await screen.findByRole('button',{name:'Update and restart'}));
  expect(screen.getByRole('tooltip').querySelector('.release-notes')).toBeNull();
 });
});

describe('review fixes (#355)', () => {
 const svc = {session_id:'s1',title:'Fix login',kind:'service',task_id:'t1',label:'npm run preview -- --port 5173',port:5173,live:true,running_turn:false};
 const blockedSnap = {...state,workers:[{...worker,phase:'ready',staged_version:'abc123',install_state:'blocked',wait_reason:'running_work',blocked_by:[svc]}]};
 const stopped = {results:[{device_id:'local',device_name:'Current worker',accepted:true,reason:'Stopping running work, then restarting',install_state:'stopping'}]};
 it('sends one stop while the snapshot read after it has not landed', async () => {
  api.get.mockResolvedValue(blockedSnap);
  render(DesktopUpdateButton);
  const button = await screen.findByRole('button',{name:'Ready; waiting for running work'});
  // Every read from here on is held: the last snapshot still says "blocked".
  let release!: (value: unknown) => void;
  api.get.mockReturnValue(new Promise(resolve => { release = resolve; }));
  api.post.mockResolvedValue(stopped);
  await fireEvent.click(button);
  await fireEvent.click(screen.getByRole('button',{name:'Stop running work and restart'}));
  await waitFor(()=>expect(api.post).toHaveBeenCalledOnce());
  const locked = await screen.findByRole('button',{name:'Stopping running work…'});
  await fireEvent.click(locked);
  const stop = screen.getByRole('button',{name:'Stop running work and restart'}) as HTMLButtonElement;
  expect(stop.disabled).toBe(true);
  await fireEvent.click(stop);
  expect(api.post).toHaveBeenCalledOnce();
  // The held read was sent before the stop: it does not settle the lock.
  release(blockedSnap);
  await new Promise(r => setTimeout(r, 0));
  expect(screen.getByRole('button',{name:'Stopping running work…'})).toBeTruthy();
  // A read sent after it does: here the stop finished and the work still runs, so a retry is allowed.
  api.get.mockResolvedValue({...blockedSnap,workers:[{...blockedSnap.workers[0],wait_reason:'still_busy_after_stop',stop_result:{accepted:false,reason:'Busy with running work'}}]});
  window.dispatchEvent(new Event('application-update-policy'));
  const again = await screen.findByRole('button',{name:'Ready; waiting for running work'});
  expect(again.title).toContain('The stop attempt finished, but work is still running.');
 });
 it('the bell action cannot send a second stop before the next snapshot', async () => {
  api.get.mockResolvedValue({...state,workers:[{...worker,phase:'ready'}]});
  api.post.mockResolvedValue(busyReceipt);
  render(DesktopUpdateButton);
  await fireEvent.click(await screen.findByRole('button',{name:'Update and restart'}));
  await waitFor(()=>expect(notice.warning).toHaveBeenCalled());
  await new Promise(r => setTimeout(r, 0));
  api.get.mockReturnValue(new Promise(() => {}));
  api.post.mockResolvedValue({results:[{device_name:'Current worker',accepted:true,reason:'Stopping running work, then restarting'}]});
  const stop = notice.warning.mock.calls.at(-1)![1].secondaryAction.onClick;
  stop();
  await waitFor(()=>expect(api.post).toHaveBeenCalledTimes(2));
  await new Promise(r => setTimeout(r, 0));
  stop();
  await new Promise(r => setTimeout(r, 0));
  expect(api.post).toHaveBeenCalledTimes(2);
 });
 const idleRetained = {...worker,phase:'idle',version:null,staged_version:'abc123',install_state:'unavailable',wait_reason:'updater_idle'};
 it('checks for updates from a retained download on an idle updater, and says what it found', async () => {
  // The server includes it with role available and its own label.
  api.get.mockResolvedValue({...state,workers:[{...idleRetained,role:'available',label:'Check for updates before restarting'}]});
  render(DesktopUpdateButton);
  const button = await screen.findByRole('button',{name:'Check for updates'});
  expect(button.title).toContain('Current worker: Check for updates before restarting');
  expect(button.title.match(/the updater is idle/g)).toHaveLength(1);
  await fireEvent.click(button);
  await waitFor(()=>expect(api.post).toHaveBeenCalledExactlyOnceWith('/settings/application-updates',{action:'check'}));
  expect(notice.info).toHaveBeenCalledWith(expect.any(String),expect.objectContaining({title:'Checking for updates'}));
 });
 it('shows no button for a retained download an older server still excludes', async () => {
  api.get.mockResolvedValue({...state,workers:[{...idleRetained,role:'excluded',label:'No update offered'}]});
  render(DesktopUpdateButton);
  await waitFor(()=>expect(api.get).toHaveBeenCalledOnce());
  expect(screen.queryByRole('button')).toBeNull();
 });
 it('does not hide a newer offer behind the waiting panel', async () => {
  api.get.mockResolvedValue({...state,workers:[{...worker,phase:'available',version:'b'.repeat(40),install_state:'waiting_for_idle',wait_reason:'running_work',blocked_by:[svc],role:'available',label:'Update available; not downloaded'}]});
  render(DesktopUpdateButton);
  await fireEvent.click(await screen.findByRole('button',{name:'Download update'}));
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(api.post).toHaveBeenCalledExactlyOnceWith('/settings/application-updates',{action:'download'});
 });
 it('never marks a worker with unknown blockers as verified', async () => {
  api.get.mockResolvedValue({...state,workers:[{...worker,phase:'ready',install_state:'blocked',wait_reason:'running_work',blocked_by:null}]});
  render(DesktopUpdateButton);
  await fireEvent.focus(await screen.findByRole('button',{name:'Ready; waiting for running work'}));
  expect(screen.getByRole('tooltip').querySelector('.verified')).toBeNull();
  expect(screen.getByRole('tooltip').textContent).toContain('Running work details are unavailable.');
 });
 it('formats this worker’s server epochs against its own clock', async () => {
  // The server is 1 h ahead of this browser: its 1 h-from-its-now expiry is "now" here.
  const serverNow = Date.now() / 1000 + 3600;
  api.get.mockResolvedValue({...state,server_time:serverNow,workers:[{...worker,is_self:true,phase:'ready',install_state:'waiting_for_idle',wait_reason:'running_work',
   blocked_by:[svc],restart_expires_at:serverNow + 60}]});
  render(DesktopUpdateButton);
  const button = await screen.findByRole('button',{name:'Restarts when work finishes'});
  const local = new Date(Date.now() + 60_000).toLocaleTimeString(undefined,{hour:'numeric',minute:'2-digit'});
  expect(button.title).toContain(`Waits until ${local} at most`);
 });
});

describe('check-first fleet (#361)', () => {
 const staged = {...worker,is_self:true,phase:'ready',staged_version:'abc123',install_state:'ready',role:'staged',label:'Staged, awaiting restart'};
 const idle = {...worker,device_id:'desk',device_name:'Desktop',is_self:false,phase:'idle',version:null,staged_version:'abc123',
  install_state:'unavailable',wait_reason:'updater_idle',role:'available',label:'Check for updates before restarting'};
 const mixed = {...state,update_all_workers:true,workers:[staged,idle]};
 const plan = {action:'check',ready:false,available:false,staged:1,held:'Check for updates needed on Desktop; nothing was installed'};
 it.each([['without',undefined],['with',plan]])('one staged and one idle-updater worker %s a server plan: the click checks, never installs', async (_, p) => {
  api.get.mockResolvedValue({...mixed,...(p ? {plan:p} : {})});
  render(DesktopUpdateButton);
  const button = await screen.findByRole('button',{name:'Check for updates'});
  expect(screen.queryByRole('button',{name:'Update and restart'})).toBeNull();
  expect(button.title).toContain('All online workers: Restart waits for an update check on Desktop. Click to check for updates.');
  expect(button.title).toContain('Desktop: Check for updates before restarting');
  await fireEvent.click(button);
  await waitFor(()=>expect(api.post).toHaveBeenCalledExactlyOnceWith('/settings/application-updates',{action:'check'}));
  expect(notice.info).toHaveBeenCalledWith(expect.any(String),expect.objectContaining({title:'Checking for updates'}));
 });
 it('trusts the server plan when a peer is too old to say why it must check first', async () => {
  const quiet = {...idle,wait_reason:undefined,install_state:undefined};
  api.get.mockResolvedValue({...mixed,workers:[staged,{...quiet,role:'excluded',label:'No update offered'}],plan});
  render(DesktopUpdateButton);
  await fireEvent.click(await screen.findByRole('button',{name:'Check for updates'}));
  await waitFor(()=>expect(api.post).toHaveBeenCalledExactlyOnceWith('/settings/application-updates',{action:'check'}));
 });
 it('shows the server’s held text when it sends a check instead of an install', async () => {
  // This view still thinks every worker is staged; the server knows better.
  api.get.mockResolvedValue({...mixed,workers:[staged,{...staged,device_id:'desk',device_name:'Desktop',is_self:false}]});
  api.post.mockResolvedValue({scope:'all',sent:'check',held:'Check for updates needed on Desktop; nothing was installed',
   results:[{device_id:'desk',device_name:'Desktop',accepted:true,reason:'Check queued'}]});
  render(DesktopUpdateButton);
  await fireEvent.click(await screen.findByRole('button',{name:'Update and restart'}));
  await waitFor(()=>expect(notice.warning).toHaveBeenCalledWith('Check for updates needed on Desktop; nothing was installed. Checking for updates first.',
   expect.objectContaining({title:'Restart held'})));
  // Nothing restarts: never "waiting on startup".
  expect(screen.queryByRole('button',{name:'Waiting on all workers to finish startup'})).toBeNull();
  await waitFor(()=>expect(screen.getByRole('button').title).toContain('Last request: Check for updates needed on Desktop; nothing was installed'));
 });
 it('a restart waiting on running work offers the check, not stop or restart, while a worker must check first', async () => {
  const svc = {session_id:'s1',title:'Fix login',kind:'service',task_id:'t1',label:'npm run preview',port:5173,live:true,running_turn:false};
  api.get.mockResolvedValue({...mixed,workers:[{...staged,install_state:'blocked',wait_reason:'running_work',blocked_by:[svc]},idle],plan});
  render(DesktopUpdateButton);
  await fireEvent.click(await screen.findByRole('button',{name:'Ready; waiting for running work'}));
  const dialog = screen.getByRole('dialog');
  expect(dialog.textContent).toContain('Restart waits for an update check on Desktop.');
  expect(screen.queryByRole('button',{name:'Stop running work and restart'})).toBeNull();
  expect(screen.queryByRole('button',{name:'Restart when work finishes'})).toBeNull();
  await fireEvent.click(screen.getByRole('button',{name:'Check for updates'}));
  await waitFor(()=>expect(api.post).toHaveBeenCalledExactlyOnceWith('/settings/application-updates',{action:'check'}));
 });
});

describe('aggregate outcome', () => {
 const skipped = {device_id:'local',device_name:'Current worker',status:'skipped',message:'Busy with running work: Fix login'};
 it('refused: names the worker and reason, never startup failure', async () => {
  api.get.mockResolvedValue({...state,workers:[{...worker,phase:'ready'}],operation:{id:'op',phase:'error',outcome:'refused',
   refused_workers:[{device_id:'local',device_name:'Current worker',reason:'Busy with running work: Fix login'}],workers:[skipped]}});
  render(DesktopUpdateButton);
  const button = await screen.findByRole('button',{name:'Update and restart'});
  expect(button.title).toContain('Restart was not started: Current worker: Busy with running work: Fix login.');
  expect(button.title).not.toContain('startup could not be confirmed');
  expect(screen.getByRole('alert').textContent).toContain('Restart was not started');
 });
 it('a legacy all-skipped error reads as a refusal too', async () => {
  api.get.mockResolvedValue({...state,workers:[{...worker,phase:'ready'}],operation:{id:'op',phase:'error',workers:[skipped]}});
  render(DesktopUpdateButton);
  const button = await screen.findByRole('button',{name:'Update and restart'});
  expect(button.title).toContain('Restart was not started: Current worker: Busy with running work: Fix login.');
 });
 it('partial: names who was left behind and never celebrates', async () => {
  vi.useFakeTimers();
  try {
   api.get.mockResolvedValue({...state,update_all_workers:true,workers:[{...worker,phase:'idle',version:null}],server_time:1_000_030,
    operation:{id:'op',phase:'complete',outcome:'partial',completed_at:1_000_000,
     refused_workers:[{device_id:'local',device_name:'Current worker',reason:'Busy with running work: Fix login'}],
     workers:[{device_id:'peer',device_name:'Peer',status:'verified',message:'Updated and verified'},skipped]}});
   render(DesktopUpdateButton);await vi.advanceTimersByTimeAsync(0);
   expect(document.querySelector('.complete')).toBeNull();
   expect(notice.warning).toHaveBeenCalledWith('Update finished on some workers. Not restarted: Current worker: Busy with running work: Fix login.',expect.objectContaining({title:'Update finished on some workers'}));
   const button = screen.getByRole('button',{name:'Update finished on some workers'});
   expect(button.title).toContain('Update finished on some workers. Not restarted: Current worker');
   await vi.advanceTimersByTimeAsync(2500);
   expect(screen.queryByRole('button',{name:'Update complete'})).toBeNull();
   await fireEvent.click(button);
   const panel = screen.getByRole('dialog',{name:'Update finished on some workers'});
   expect(panel.textContent).toContain('Not restarted:');
   await fireEvent.click(screen.getByRole('button',{name:'Close'}));
   await vi.advanceTimersByTimeAsync(0);
   expect(screen.queryByRole('button')).toBeNull();
  } finally { cleanup();vi.useRealTimers(); }
 });
 it('failed: shows the worker’s own message', async () => {
  api.get.mockResolvedValue({...state,workers:[{...worker,phase:'ready'}],operation:{id:'op',phase:'error',outcome:'failed',refused_workers:[],
   workers:[{device_id:'local',device_name:'Current worker',status:'error',message:'Startup not confirmed. Check this worker before retrying.'}]}});
  render(DesktopUpdateButton);
  const button = await screen.findByRole('button',{name:'Update and restart'});
  expect(button.title).toContain('Current worker: Startup not confirmed. Check this worker before retrying.');
 });
 it('waiting: keeps refused workers named while the rest start', async () => {
  api.get.mockResolvedValue({...state,update_all_workers:true,operation:{id:'op',phase:'waiting',outcome:'waiting',
   refused_workers:[{device_id:'local',device_name:'Current worker',reason:'Offline; not queued'}],
   workers:[{device_id:'peer',device_name:'Peer',status:'waiting',message:'Verifying updated version…'},{...skipped,message:'Offline; not queued'}]}});
  render(DesktopUpdateButton);
  const button = await screen.findByRole('button',{name:'Waiting on all workers to finish startup'});
  expect(button.title).toContain('Not restarted: Current worker: Offline; not queued.');
 });
});
