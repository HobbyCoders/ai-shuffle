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
// The app restarted into this install a moment ago, so a fresh mount may celebrate it.
const justSettled = {...settled,server_time:1_000_030,operation:{...settled.operation,completed_at:1_000_000}};

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
const peer = {...worker,device_id:'peer',device_name:'Unraid'};
it('waits for every included worker before offering restart and labels each phase', async()=>{
 api.get.mockResolvedValue({...state,update_all_workers:true,workers:[{...worker,phase:'ready'},{...peer,phase:'downloading',message:'Building frontend…'}]});
 render(DesktopUpdateButton);
 const button=await screen.findByRole('button',{name:'Downloading update'});
 expect(button.getAttribute('aria-disabled')).toBe('true');
 expect(button.title).toContain('1 of 2 staged');
 expect(button.title).toContain('Current worker: Staged, awaiting restart');
 expect(button.title).toContain('Unraid: Building frontend…');
 await fireEvent.click(button);expect(api.post).not.toHaveBeenCalled();
 api.get.mockResolvedValue({...state,update_all_workers:true,workers:[{...worker,phase:'ready'},{...peer,phase:'checking'}]});
 window.dispatchEvent(new Event('application-update-policy'));
 await waitFor(()=>expect(button.title).toContain('Unraid: Checking for updates…'));
 expect(screen.queryByRole('button',{name:'Update and restart'})).toBeNull();
 api.get.mockResolvedValue({...state,update_all_workers:true,workers:[{...worker,phase:'ready'},{...peer,phase:'ready',message:'Update ready. Choose Update and restart.'}]});
 window.dispatchEvent(new Event('application-update-policy'));
 const restart=await screen.findByRole('button',{name:'Update and restart'});
 expect(restart.title).toContain('Unraid: Staged, awaiting restart');
 expect(restart.title).not.toContain('running');
 await fireEvent.focus(restart);
 const rows=[...screen.getByRole('tooltip').querySelectorAll('.worker-row')].map(row=>row.textContent);
 expect(rows).toEqual(['Current workerStaged, awaiting restart','UnraidStaged, awaiting restart']);
});
it('lists excluded workers with a reason without counting them ready or letting them block', async()=>{
 api.get.mockResolvedValue({...state,update_all_workers:true,workers:[
  {...worker,phase:'ready'},
  {...peer,device_id:'off',device_name:'Laptop',online:false,supported:false,phase:'unsupported',reason:'Offline; not queued'},
  {...peer,device_id:'cur',device_name:'Desk',phase:'ready',version:'bbbbbbbbbbbb',running_version:'b'.repeat(40)},
  {...peer,device_id:'old',device_name:'Old',supported:false,phase:'unsupported',version:null},
  {...peer,device_id:'off2',device_name:'Locked',allow_updates:false,phase:'available'},
  {...peer,device_id:'idle',device_name:'Idle',phase:'idle',version:null},
 ]});
 render(DesktopUpdateButton);
 const button=await screen.findByRole('button',{name:'Update and restart'});
 for (const text of ['Laptop: Offline; not queued','Desk: Already up to date','Old: No application updater; not included',
  'Locked: Updates disabled on this worker; not included','Idle: No update offered']) expect(button.title).toContain(text);
});
it.each([
 ['an unreadable status',{phase:'unknown',supported:false,reason:'Worker unavailable or does not support application updates'},'Unraid (Status unavailable)'],
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
 expect(button.title).toContain('Unraid: Not in scope; current worker only');
});
it('never celebrates a recent completion while an update is staged but held for an unknown worker', async()=>{
 api.get.mockResolvedValue({...justSettled,update_all_workers:true,workers:[{...worker,phase:'ready',version:'cccccccccccc',running_version:'b'.repeat(40)},{...peer,phase:'unknown',supported:false}]});
 render(DesktopUpdateButton);
 const button=await screen.findByRole('button',{name:'Check for updates'});
 expect(button.title).toContain('Restart waits for Unraid');
 expect(document.querySelector('.complete')).toBeNull();
 expect(screen.queryByRole('button',{name:'Update complete'})).toBeNull();
});

// #1623 live test: a peer that refused the fanned-out request was left idle and
// the button offered restart from the initiating worker's state alone.
it('names a worker that refused the last request and holds restart for it', async()=>{
 api.get.mockResolvedValue({...state,update_all_workers:true,workers:[{...worker},{...peer,phase:'idle',version:null}]});
 api.post.mockResolvedValue({results:[
  {device_id:'peer',device_name:'Unraid',accepted:false,reason:'No application update available; current version left running'},
  {device_id:'local',device_name:'Current worker',accepted:true,reason:'Request queued'}]});
 render(DesktopUpdateButton);
 const download=await screen.findByRole('button',{name:'Download update'});
 api.get.mockResolvedValue({...state,update_all_workers:true,workers:[{...worker,phase:'ready'},{...peer,phase:'idle',version:null}]});
 await fireEvent.click(download);
 const button=await screen.findByRole('button',{name:'Check for updates'});
 expect(screen.queryByRole('button',{name:'Update and restart'})).toBeNull();
 expect(button.title).toContain('Restart waits for Unraid (Refused: No application update available');
 expect(screen.getByRole('alert').textContent).toContain('Refused: No application update available');
 await fireEvent.focus(button);
 const rows=[...screen.getByRole('tooltip').querySelectorAll('.worker-row')].map(row=>row.textContent);
 expect(rows).toContain('UnraidRefused: No application update available; current version left running');
 // A check that reaches the peer clears the refusal; the peer then reports its own phase.
 api.post.mockResolvedValue({results:[{device_id:'peer',device_name:'Unraid',accepted:true,reason:'Request queued'},
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
 expect(button.title).toContain('Unraid: Request queued; waiting for the updater');
 expect(screen.queryByRole('button',{name:'Update and restart'})).toBeNull();
});
it('ignores an idle message that claims an update is in progress', async()=>{
 api.get.mockResolvedValue({...state,update_all_workers:true,workers:[{...worker,phase:'ready'},
  {...peer,phase:'idle',version:null,message:'Application update is running.'},
  {...peer,device_id:'desk',device_name:'Desk',phase:'idle',version:null,message:'Updated version is running.'}]});
 render(DesktopUpdateButton);
 const button=await screen.findByRole('button',{name:'Update and restart'});
 expect(button.title).toContain('Unraid: No update offered');
 expect(button.title).not.toContain('Application update is running');
 expect(button.title).toContain('Desk: Updated version is running.');
});
