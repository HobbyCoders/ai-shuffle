import {afterEach, expect, it, vi} from 'vitest';
import {act, cleanup, fireEvent, render, screen, waitFor} from '@testing-library/svelte';
import CommandAutocomplete from './CommandAutocomplete.svelte';
afterEach(()=>{cleanup();vi.unstubAllGlobals()});
const item=(name:string,type='sdk_builtin')=>({name,display:`/${name}`,description:`${name} command`,type});
const response=(commands:any[])=>({ok:true,json:async()=>({commands})});
it('requests selected model and discards a late menu from the previous selection',async()=>{
 let first!:(value:any)=>void;
 const fetch=vi.fn().mockImplementationOnce(()=>new Promise(resolve=>first=resolve)).mockResolvedValueOnce(response([item('custom-task','custom')]));
 vi.stubGlobal('fetch',fetch);
 const {rerender}=render(CommandAutocomplete,{inputValue:'/',visible:true,projectId:'project',sessionId:'session',profileId:'agent',model:'sonnet',mode:'code',onSelect:vi.fn(),onClose:vi.fn()});
 await waitFor(()=>expect(fetch).toHaveBeenCalledTimes(1));
 await rerender({model:'gpt-test'});
 await waitFor(()=>expect(screen.getByText('/custom-task')).toBeTruthy());
 expect(String(fetch.mock.calls[1][0])).toContain('model=gpt-test');
 expect(String(fetch.mock.calls[1][0])).toContain('profile_id=agent');
 expect(fetch.mock.calls[0][1].signal.aborted).toBe(true);
 first(response([item('compact')]));
 await Promise.resolve(); await Promise.resolve();
 expect(screen.queryByText('/compact')).toBeNull();
 expect(screen.getByText('/custom-task')).toBeTruthy();
});
it('clears the old commands when the replacement request is forbidden',async()=>{
 vi.stubGlobal('fetch',vi.fn().mockResolvedValueOnce(response([item('compact')])).mockResolvedValueOnce({ok:false,status:403}));
 const {rerender}=render(CommandAutocomplete,{inputValue:'/',visible:true,projectId:'mine',model:'sonnet',onSelect:vi.fn(),onClose:vi.fn()});
 await waitFor(()=>expect(screen.getByText('/compact')).toBeTruthy());
 await rerender({projectId:'denied'});
 await waitFor(()=>expect(screen.queryByText('/compact')).toBeNull());
});
it('keeps the catalog and selected row when the same context updates or refreshes', async () => {
 let refresh!:(value:any)=>void;
 const commands=[item('alpha','skill'),item('beta','custom')];
 const fetch=vi.fn().mockResolvedValueOnce(response(commands))
  .mockImplementationOnce(()=>new Promise(resolve=>refresh=resolve));
 vi.stubGlobal('fetch',fetch);
 const {rerender}=render(CommandAutocomplete,{inputValue:'/',visible:true,sessionId:'session',profileId:'agent',model:'sonnet',onSelect:vi.fn(),onClose:vi.fn()});
 await waitFor(()=>expect(screen.getByText('/alpha')).toBeTruthy());
 const row=screen.getByText('/alpha').closest('[role="option"]')!;
 const list=screen.getByRole('listbox');
 list.scrollTop=72;
 await fireEvent.mouseEnter(row.querySelector('button')!);
 await rerender({sessionId:'session',profileId:'agent',model:'sonnet',onClose:vi.fn()});
 expect(fetch).toHaveBeenCalledTimes(1);
 expect(row.getAttribute('aria-selected')).toBe('true');
 expect(screen.getByRole('listbox')).toBe(list);
 expect(list.scrollTop).toBe(72);
 await rerender({visible:false});
 await rerender({visible:true});
 await waitFor(()=>expect(fetch).toHaveBeenCalledTimes(2));
 const reopened=screen.getByText('/alpha').closest('[role="option"]')!;
 expect(reopened.getAttribute('aria-selected')).toBe('true');
 await act(async () => { refresh(response(commands)); });
 await waitFor(()=>expect(screen.getByText('/alpha').closest('[role="option"]')).toBe(reopened));
 expect(reopened.getAttribute('aria-selected')).toBe('true');
});
it('filters locally without another request or losing surviving row identity', async () => {
 const fetch=vi.fn().mockResolvedValue(response([item('alpha','skill'),item('beta','custom')]));
 vi.stubGlobal('fetch',fetch);
 const {rerender}=render(CommandAutocomplete,{inputValue:'/',visible:true,onSelect:vi.fn(),onClose:vi.fn()});
 await waitFor(()=>expect(screen.getByText('/alpha')).toBeTruthy());
 const row=screen.getByText('/alpha').closest('[role="option"]');
 await rerender({inputValue:'/alp'});
 expect(screen.getByRole('option')).toBe(row);
 expect(fetch).toHaveBeenCalledTimes(1);
});
it('removes cached commands if a same-context refresh loses access', async () => {
 const fetch=vi.fn().mockResolvedValueOnce(response([item('alpha','skill')])).mockResolvedValueOnce({ok:false,status:403});
 vi.stubGlobal('fetch',fetch);
 const {rerender}=render(CommandAutocomplete,{inputValue:'/',visible:true,onSelect:vi.fn(),onClose:vi.fn()});
 await waitFor(()=>expect(screen.getByText('/alpha')).toBeTruthy());
 await rerender({visible:false});
 await rerender({visible:true});
 await waitFor(()=>expect(screen.queryByText('/alpha')).toBeNull());
});
it('reloads when the selected agent settings change without leaking the old catalog', async () => {
 let refresh!:(value:any)=>void;
 const fetch=vi.fn().mockResolvedValueOnce(response([item('user-skill','skill')]))
  .mockImplementationOnce(()=>new Promise(resolve=>refresh=resolve));
 vi.stubGlobal('fetch',fetch);
 const {rerender}=render(CommandAutocomplete,{inputValue:'/',visible:true,profileId:'agent',settingsKey:'all',onSelect:vi.fn(),onClose:vi.fn()});
 await waitFor(()=>expect(screen.getByText('/user-skill')).toBeTruthy());
 await rerender({settingsKey:'project-only'});
 expect(screen.queryByText('/user-skill')).toBeNull();
 await waitFor(()=>expect(fetch).toHaveBeenCalledTimes(2));
 expect(String(fetch.mock.calls[1][0])).not.toContain('project-only');
 refresh(response([item('project-skill','skill')]));
 await waitFor(()=>expect(screen.getByText('/project-skill')).toBeTruthy());
});
it('does not restart an in-flight request on unrelated parent updates', async () => {
 let complete!:(value:any)=>void;
 const fetch=vi.fn().mockImplementation(()=>new Promise(resolve=>complete=resolve));
 vi.stubGlobal('fetch',fetch);
 const {rerender}=render(CommandAutocomplete,{inputValue:'/',visible:true,sessionId:'session',onSelect:vi.fn(),onClose:vi.fn()});
 await waitFor(()=>expect(fetch).toHaveBeenCalledTimes(1));
 for (let i=0;i<5;i++) await rerender({sessionId:'session',onClose:vi.fn()});
 expect(fetch).toHaveBeenCalledTimes(1);
 expect(fetch.mock.calls[0][1].signal.aborted).toBe(false);
 complete(response([item('alpha','skill')]));
 await waitFor(()=>expect(screen.getByText('/alpha')).toBeTruthy());
});
it('shows an honest empty state for a runtime without slash dispatch',async()=>{
 vi.stubGlobal('fetch',vi.fn().mockResolvedValue(response([])));
 render(CommandAutocomplete,{inputValue:'/',visible:true,model:'gpt-test',onSelect:vi.fn(),onClose:vi.fn()});
 await waitFor(()=>expect(screen.getByText('No slash commands available for this runtime')).toBeTruthy());
});
