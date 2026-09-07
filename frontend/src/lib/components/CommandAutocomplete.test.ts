import {afterEach, expect, it, vi} from 'vitest';
import {cleanup, render, screen, waitFor} from '@testing-library/svelte';
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
it('shows an honest empty state for a runtime without slash dispatch',async()=>{
 vi.stubGlobal('fetch',vi.fn().mockResolvedValue(response([])));
 render(CommandAutocomplete,{inputValue:'/',visible:true,model:'gpt-test',onSelect:vi.fn(),onClose:vi.fn()});
 await waitFor(()=>expect(screen.getByText('No slash commands available for this runtime')).toBeTruthy());
});
