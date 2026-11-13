import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
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
  expect(view.container.querySelector('.st-dwrap.docs')).toBeTruthy();
  expect(view.container.querySelector('.docs-content.framed')).toBeTruthy();
  expect(view.getByRole('button', { name: 'Back to Docs' })).toBeTruthy();
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
  expect(view.container.querySelector('.st-empty-detail')).toBeTruthy();
  await fireEvent.click(view.getByRole('button', { name: /^Clear$/ }));
  expect(document.activeElement).toBe(search);
  expect(view.queryByRole('status')).toBeNull();
  await fireEvent.input(search,{target:{value:'qzxqzxmissingword'}});
  await fireEvent.keyDown(search,{key:'Escape'});
  expect(view.queryByRole('status')).toBeNull();
 });
});

// A browser history for shallow routing: pushState adds an entry, Back and
// Forward move through them and set `page.state` as SvelteKit does.
const nav = vi.hoisted(() => ({ stack: [{}] as Record<string, unknown>[], at: 0 }));
vi.mock('$app/navigation', async (original) => {
 const { page } = await import('$app/state');
 return {
  ...(await original<object>()),
  pushState: (_url: string, state: Record<string, unknown>) => {
   nav.stack.splice(nav.at + 1, Infinity, state);
   nav.at += 1;
   page.state = state;
  }
 };
});

describe('Docs history and headings (#1476, #1477)', () => {
 let page: { state: Record<string, unknown> };
 const width = window.innerWidth;
 function go(step: number) {
  nav.at = Math.max(0, Math.min(nav.stack.length - 1, nav.at + step));
  page.state = nav.stack[nav.at];
 }
 const title = (view: { container: HTMLElement }) => view.container.querySelector('.docs-content h1')?.textContent;
 beforeEach(async () => {
  await preloadAllDocs();
  page = (await import('$app/state')).page as unknown as typeof page;
  nav.stack = [{}];
  nav.at = 0;
  page.state = {};
  sessionStorage.clear();
  vi.spyOn(history, 'back').mockImplementation(() => go(-1));
 });
 afterEach(() => {
  vi.restoreAllMocks();
  Object.defineProperty(window, 'innerWidth', { value: width, configurable: true });
  sessionStorage.clear();
 });

 it('on a phone keeps "On this page" on reopen, and Back returns to the document a link left', async () => {
  Object.defineProperty(window, 'innerWidth', { value: 390, configurable: true });
  const view = render(DocsCard, { mobile: true });
  await fireEvent.click(view.getByRole('button', { name: 'Changelog' }));
  await waitFor(() => expect(nav.stack).toHaveLength(2));
  expect(page.state.docsDoc).toBe('getting-started/changelog');
  expect(view.getByRole('button', { name: 'On this page' })).toBeTruthy();

  await fireEvent.click(view.getByRole('button', { name: 'Back to Docs' }));
  await waitFor(() => expect(view.queryByRole('button', { name: 'Back to Docs' })).toBeNull());
  await fireEvent.click(view.getByRole('button', { name: 'Changelog' }));
  await waitFor(() => expect(view.getByRole('button', { name: 'On this page' })).toBeTruthy());

  // A link in the document opens another one as its own Back step.
  await view.rerender({ mobile: true, initialItemId: 'getting-started/updates' });
  await waitFor(() => expect(title(view)).toBe('Updates'));
  expect(page.state.docsDoc).toBe('getting-started/updates');
  await fireEvent.click(view.getByRole('button', { name: 'Back to Docs' }));
  await waitFor(() => expect(title(view)).toBe('Changelog'));
  go(1);
  await waitFor(() => expect(title(view)).toBe('Updates'));
 });

 it('on a wide screen makes each document a Back step and reopens the last one after a reload', async () => {
  Object.defineProperty(window, 'innerWidth', { value: 1440, configurable: true });
  const view = render(DocsCard, {});
  await waitFor(() => expect(view.container.querySelector('.docs-content h1')).not.toBeNull());
  const first = title(view);
  await fireEvent.click(view.getByRole('button', { name: 'FAQ' }));
  await waitFor(() => expect(title(view)).toBe('Frequently asked questions'));
  expect(nav.stack).toEqual([{}, { docsDoc: 'getting-started/faq' }]);

  go(-1);
  await waitFor(() => expect(title(view)).toBe(first));
  go(1);
  await waitFor(() => expect(title(view)).toBe('Frequently asked questions'));

  cleanup();
  const reloaded = render(DocsCard, {});
  await waitFor(() => expect(title(reloaded)).toBe('Frequently asked questions'));
 });
});
