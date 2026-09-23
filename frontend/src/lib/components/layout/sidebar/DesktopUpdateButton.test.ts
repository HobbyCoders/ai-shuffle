import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api, getErrorMessage: (error: Error) => error.message }));
const updateAuth = vi.hoisted(() => ({current: {status: {preview_access: false}}}));
vi.mock('$lib/stores/auth.svelte', () => ({auth: updateAuth}));
import DesktopUpdateButton from './DesktopUpdateButton.svelte';
const worker = { device_id:'local', device_name:'Current worker', online:true, supported:true, allow_updates:true, phase:'available', version:'abc123' };
const state = { allow_updates:true, update_all_workers:false, workers:[worker] };
beforeEach(() => { localStorage.clear(); updateAuth.current.status.preview_access = false; vi.resetAllMocks(); api.get.mockResolvedValue(state); api.post.mockResolvedValue({results:[{device_name:'Current worker',accepted:true,reason:'Request queued'}]}); });
afterEach(cleanup);
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
