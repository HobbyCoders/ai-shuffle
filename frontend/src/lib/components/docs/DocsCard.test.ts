import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor, within } from '@testing-library/svelte';
import { preloadAllDocs } from '$lib/docs/index';
import DocsCard from './DocsCard.svelte';
afterEach(() => { cleanup(); sessionStorage.clear(); });
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

import { shellHistory } from '$lib/services/app-history.svelte';
import { goto } from '$app/navigation';
vi.mock('$app/navigation', () => ({ goto: vi.fn() }));

describe('Docs headings, search marks and reload (#1476, #1478)', () => {
 const width = window.innerWidth;
 const title = (view: { container: HTMLElement }) => view.container.querySelector('.docs-content h1')?.textContent;
 beforeEach(async () => {
  await preloadAllDocs();
  sessionStorage.clear();
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
 });
 afterEach(() => {
  shellHistory.stop();
  vi.unstubAllGlobals();
  Object.defineProperty(window, 'innerWidth', { value: width, configurable: true });
  sessionStorage.clear();
 });

 it('on a phone keeps "On this page" on reopen, and Back to Docs shows the list', async () => {
  Object.defineProperty(window, 'innerWidth', { value: 390, configurable: true });
  const view = render(DocsCard, { mobile: true });
  await fireEvent.click(view.getByRole('button', { name: 'Changelog' }));
  await waitFor(() => expect(title(view)).toBe('Changelog'));
  expect(view.getByRole('button', { name: 'On this page' })).toBeTruthy();

  await fireEvent.click(view.getByRole('button', { name: 'Back to Docs' }));
  await waitFor(() => expect(view.container.querySelector('[data-view-shell="Docs"]')?.getAttribute('data-pane')).toBe('panel'));
  await fireEvent.click(view.getByRole('button', { name: 'Changelog' }));
  await waitFor(() => expect(view.getByRole('button', { name: 'On this page' })).toBeTruthy());

  // A link in the document opens the other one; Back to Docs still shows the list.
  await view.rerender({ mobile: true, initialItemId: 'getting-started/updates' });
  await waitFor(() => expect(title(view)).toBe('Updates'));
  await fireEvent.click(view.getByRole('button', { name: 'Back to Docs' }));
  await waitFor(() => expect(view.container.querySelector('[data-view-shell="Docs"]')?.getAttribute('data-pane')).toBe('panel'));
 });

 it('reopens the entry’s article on desktop and phone', async () => {
  Object.defineProperty(window, 'innerWidth', { value: 1440, configurable: true });
  shellHistory.start({}, { type: 'docs' });
  const view = render(DocsCard, {});
  await waitFor(() => expect(title(view)).toBe('AI Shuffle documentation'));
  await fireEvent.click(view.getByRole('button', { name: 'FAQ' }));
  await waitFor(() => expect(title(view)).toBe('Frequently asked questions'));

  cleanup();
  const reloaded = render(DocsCard, {});
  await waitFor(() => expect(title(reloaded)).toBe('Frequently asked questions'));

  cleanup();
  Object.defineProperty(window, 'innerWidth', { value: 390, configurable: true });
  const phone = render(DocsCard, { mobile: true });
  await waitFor(() => expect(title(phone)).toBe('Frequently asked questions'));
 });

 it('choosing the shown article again retries a rejected step, and a committed one adds none (S15)', async () => {
  Object.defineProperty(window, 'innerWidth', { value: 1440, configurable: true });
  shellHistory.start({}, { type: 'docs' });
  const view = render(DocsCard, {});
  await waitFor(() => expect(title(view)).toBe('AI Shuffle documentation'));
  const docs = () => vi.mocked(goto).mock.calls.map(([, options]) => (options as { replace?: boolean; state: { shell: { place: { doc?: string } } } }))
   .filter((options) => !options.replace).map((options) => options.state.shell.place.doc);
  vi.mocked(goto).mockClear();
  vi.mocked(goto).mockRejectedValueOnce(new Error('navigation aborted'));
  await fireEvent.click(view.getByRole('button', { name: 'FAQ' }));
  await waitFor(() => expect(docs()).toHaveLength(1));
  await new Promise((resolve) => setTimeout(resolve));
  // FAQ shows but its push failed: choosing it again writes it again.
  await fireEvent.click(view.getByRole('button', { name: 'FAQ' }));
  await waitFor(() => expect(docs()).toHaveLength(2));
  expect(docs()[0]).toBe(docs()[1]);
  await new Promise((resolve) => setTimeout(resolve));
  // Now committed: a third pick adds nothing.
  await fireEvent.click(view.getByRole('button', { name: 'FAQ' }));
  await new Promise((resolve) => setTimeout(resolve, 20));
  expect(docs()).toHaveLength(2);
 });

 it('marks every search result it opens, also after a document chosen from the list', async () => {
  Object.defineProperty(window, 'innerWidth', { value: 1440, configurable: true });
  const view = render(DocsCard, {});
  await waitFor(() => expect(view.container.querySelector('.docs-content h1')).not.toBeNull());
  const search = view.getByRole('searchbox', { name: 'Search docs' });
  async function openResult(query: string) {
   await fireEvent.input(search, { target: { value: query } });
   const results = view.container.querySelector('[aria-label="Doc search results"]') as HTMLElement;
   await waitFor(() => expect(within(results).getAllByRole('button').length).toBeGreaterThan(0));
   await fireEvent.click(within(results).getAllByRole('button')[0]);
  }
  const hit = () => [...view.container.querySelectorAll('mark.doc-find-hit')].map((m) => m.textContent).join('');
  await openResult('remote access');
  await waitFor(() => expect(hit().toLowerCase()).toBe('remote access'));
  await fireEvent.click(view.getByRole('button', { name: 'FAQ' }));
  await waitFor(() => expect(title(view)).toBe('Frequently asked questions'));
  expect(hit()).toBe('');
  await openResult('remote access');
  await waitFor(() => expect(hit().toLowerCase()).toBe('remote access'));
 });
});
