import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api, getErrorMessage: (error: Error) => error.message }));
import DesktopUpdateButton from './DesktopUpdateButton.svelte';
const worker = { device_id:'local', device_name:'Current worker', online:true, supported:true, allow_updates:true, phase:'available', version:'abc123' };
const state = { allow_updates:true, update_all_workers:false, workers:[worker] };
beforeEach(() => { vi.resetAllMocks(); api.get.mockResolvedValue(state); api.post.mockResolvedValue({results:[{device_name:'Current worker',accepted:true,reason:'Request queued'}]}); });
afterEach(cleanup);
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
 expect(api.post).toHaveBeenCalledOnce();expect(button.hasAttribute('disabled')).toBe(true);
 reject(new Error('Worker unavailable'));
 await waitFor(()=>expect(screen.getByRole('alert').textContent).toContain('Worker unavailable'));
 expect(button.hasAttribute('disabled')).toBe(false);
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
