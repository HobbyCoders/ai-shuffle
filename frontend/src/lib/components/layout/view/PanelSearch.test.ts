import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import PanelSearch from './PanelSearch.svelte';
afterEach(cleanup);
it('uses the shared clear control by default and keeps focus and input callbacks', async () => {
 const oninput = vi.fn();
 render(PanelSearch,{value:'release',placeholder:'Search notes',oninput});
 const input = screen.getByRole('searchbox') as HTMLInputElement;
 await fireEvent.click(screen.getByRole('button',{name:'Clear search'}));
 expect(input.value).toBe('');
 expect(document.activeElement).toBe(input);
 expect(oninput).toHaveBeenCalledOnce();
 expect(screen.queryByRole('button',{name:'Clear search'})).toBeNull();
});
it('preserves the disabled and explicitly non-clearable paths', () => {
 const view=render(PanelSearch,{value:'release',disabled:true});
 expect((screen.getByRole('button',{name:'Clear search'}) as HTMLButtonElement).disabled).toBe(true);
 view.unmount();
 render(PanelSearch,{value:'release',clearable:false});
 expect(screen.queryByRole('button',{name:'Clear search'})).toBeNull();
});
