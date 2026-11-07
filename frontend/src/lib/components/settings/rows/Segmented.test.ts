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
it('names an option in full in its tooltip when a page shortens its label', () => {
 render(Segmented, { label:'Show projects', size:'sm', value:'all', options:[{value:'all',label:'All workers'},{value:'here',label:'On My Very Long Workstation Name',title:'Show only projects on My Very Long Workstation Name'}] });
 const group=screen.getByRole('group',{name:'Show projects'});
 const here=within(group).getByRole('button',{name:'On My Very Long Workstation Name'});
 expect(here.getAttribute('title')).toBe('Show only projects on My Very Long Workstation Name');
 expect(here.querySelector('.st-seg-label')?.textContent).toBe('On My Very Long Workstation Name');
 expect(within(group).getByRole('button',{name:'All workers'}).hasAttribute('title')).toBe(false);
});
