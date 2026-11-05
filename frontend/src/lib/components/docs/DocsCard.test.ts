import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, waitFor, within } from '@testing-library/svelte';
import { preloadAllDocs } from '$lib/docs/index';
import DocsCard from './DocsCard.svelte';
afterEach(cleanup);
describe('Docs shared navigation', () => {
 it('searches the bundled docs, selects with the keyboard and shows the matching article', async () => {
  await preloadAllDocs();
  const view = render(DocsCard, { mobile:true });
  const search = view.getByRole('searchbox', {name:'Search docs'});
  await fireEvent.input(search, {target:{value:'remote access'}});
  const results = view.container.querySelector('[aria-label="Doc search results"]')!;
  await waitFor(() => expect(within(results as HTMLElement).getAllByRole('button').length).toBeGreaterThan(0));
  await fireEvent.keyDown(search,{key:'ArrowDown'});
  await fireEvent.keyDown(search,{key:'Enter'});
  await waitFor(() => expect(view.container.querySelector('.docs-content h1')?.textContent).toContain('Remote'));
  expect((search as HTMLInputElement).value).toBe('');
  await fireEvent.input(search,{target:{value:'remote'}});
  await fireEvent.click(view.getByRole('button',{name:'Clear search'}));
  expect((search as HTMLInputElement).value).toBe('');
  expect(document.activeElement).toBe(search);
 });
 it('shows a helpful empty search and clears it with Escape',async()=>{
  await preloadAllDocs();
  const view=render(DocsCard,{mobile:true});
  const search=view.getByRole('searchbox',{name:'Search docs'});
  await fireEvent.input(search,{target:{value:'qzxqzxmissingword'}});
  expect(view.getByRole('status').textContent).toContain('No docs match');
  await fireEvent.keyDown(search,{key:'Escape'});
  expect(view.queryByRole('status')).toBeNull();
 });
});
