import { cleanup, fireEvent, render, screen, within } from '@testing-library/svelte';
import { afterEach, expect, it, vi } from 'vitest';
import { Folder } from 'lucide-svelte';
import Segmented from './Segmented.svelte';
afterEach(cleanup);
it('shares the Settings selected treatment with icon-bearing dialog radios', async () => {
 const onchange=vi.fn();
 const view=render(Segmented, { label:'Source', selection:'radio', stretch:true, value:'folder', options:[{value:'folder',label:'Folder on Local',icon:Folder},{value:'github',label:'Clone from GitHub'}], onchange });
 const group=screen.getByRole('radiogroup',{name:'Source'});
 const folder=within(group).getByRole('radio',{name:'Folder on Local'});
 expect(folder.getAttribute('aria-checked')).toBe('true');
 expect(folder.querySelector('svg')).toBeTruthy();
 expect(group.classList.contains('st-seg')).toBe(true);
 await fireEvent.click(within(group).getByRole('radio',{name:'Clone from GitHub'}));
 expect(onchange).toHaveBeenCalledWith('github');
 await view.rerender({disabled:true});
 expect((folder as HTMLButtonElement).disabled).toBe(true);
});
