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

// A browser history for shallow routing: pushState adds an entry, replaceState
// rewrites the current one, Back and Forward move through them and set
// `page.state` as SvelteKit does. Each entry keeps its own number.
const nav = vi.hoisted(() => ({ stack: [{}] as Record<string, unknown>[], ids: [1], at: 0, next: 2 }));
vi.mock('$app/navigation', async (original) => {
 const { page } = await import('$app/state');
 return {
  ...(await original<object>()),
  pushState: (_url: string, state: Record<string, unknown>) => {
   nav.stack.splice(nav.at + 1, Infinity, state);
   nav.ids.splice(nav.at + 1, Infinity, nav.next++);
   nav.at += 1;
   page.state = state;
  },
  replaceState: (_url: string, state: Record<string, unknown>) => {
   nav.stack[nav.at] = state;
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
 /** A reload: SvelteKit empties the current entry's state but keeps its number. */
 function reload() {
  cleanup();
  nav.stack[nav.at] = {};
  page.state = {};
 }
 const title = (view: { container: HTMLElement }) => view.container.querySelector('.docs-content h1')?.textContent;
 const pane = (view: { container: HTMLElement }, label: string) => view.container.querySelector(`[data-view-shell="${label}"]`)?.getAttribute('data-pane');
 beforeEach(async () => {
  await preloadAllDocs();
  page = (await import('$app/state')).page as unknown as typeof page;
  nav.stack = [{}];
  nav.ids = [1];
  nav.at = 0;
  page.state = {};
  sessionStorage.clear();
  vi.spyOn(history, 'back').mockImplementation(() => go(-1));
  Object.defineProperty(history, 'state', { configurable: true, get: () => ({ 'sveltekit:history': nav.ids[nav.at] }) });
 });
 afterEach(() => {
  vi.restoreAllMocks();
  delete (history as { state?: unknown }).state;
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
  // The entry Docs opened on says what it showed; the chosen document is a new one.
  expect(nav.stack).toEqual([{ docsDoc: 'getting-started/_index', docsStep: false }, { docsDoc: 'getting-started/faq', docsStep: true }]);

  go(-1);
  await waitFor(() => expect(title(view)).toBe(first));
  go(1);
  await waitFor(() => expect(title(view)).toBe('Frequently asked questions'));

  cleanup();
  const reloaded = render(DocsCard, {});
  await waitFor(() => expect(title(reloaded)).toBe('Frequently asked questions'));
 });

 it('choosing the open document again keeps Back to the document before it', async () => {
  Object.defineProperty(window, 'innerWidth', { value: 1440, configurable: true });
  const view = render(DocsCard, {});
  await waitFor(() => expect(view.container.querySelector('.docs-content h1')).not.toBeNull());
  const first = title(view);
  await fireEvent.click(view.getByRole('button', { name: 'FAQ' }));
  await waitFor(() => expect(title(view)).toBe('Frequently asked questions'));
  await fireEvent.click(view.getByRole('button', { name: 'FAQ' }));
  expect(nav.stack).toHaveLength(2);
  go(-1);
  await waitFor(() => expect(title(view)).toBe(first));
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

 it('a reload keeps the entry it was on and the whole Back and Forward chain', async () => {
  Object.defineProperty(window, 'innerWidth', { value: 1440, configurable: true });
  const view = render(DocsCard, {});
  await waitFor(() => expect(title(view)).toBe('AI Shuffle documentation'));
  for (const [name, heading] of [['Updates', 'Updates'], ['FAQ', 'Frequently asked questions'], ['Changelog', 'Changelog']]) {
   await fireEvent.click(view.getByRole('button', { name }));
   await waitFor(() => expect(title(view)).toBe(heading));
  }
  go(-2);
  await waitFor(() => expect(title(view)).toBe('Updates'));

  reload();
  const reloaded = render(DocsCard, {});
  await waitFor(() => expect(title(reloaded)).toBe('Updates'));
  expect(nav.stack).toHaveLength(4);
  expect(page.state).toEqual({ docsDoc: 'getting-started/updates', docsStep: true });
  go(-1);
  await waitFor(() => expect(title(reloaded)).toBe('AI Shuffle documentation'));
  go(2);
  await waitFor(() => expect(title(reloaded)).toBe('Frequently asked questions'));
  go(1);
  await waitFor(() => expect(title(reloaded)).toBe('Changelog'));
 });

 describe('beside another destination', () => {
  let platform: typeof import('$lib/stores/platform.svelte').platform;
  let layout: typeof import('$lib/stores/layout.svelte').layout;
  let RetainedDocs: typeof import('./test-fixtures/RetainedDocs.svelte').default;
  beforeEach(async () => {
   ({ platform } = await import('$lib/stores/platform.svelte'));
   ({ layout } = await import('$lib/stores/layout.svelte'));
   RetainedDocs = (await import('./test-fixtures/RetainedDocs.svelte')).default;
   platform.set('mobile');
   layout.openFeaturePage('docs');
  });
  afterEach(() => {
   platform.set('desktop');
   layout.openChat(null);
  });
  const shown = () => layout.current.activeView?.type;

  it('keeps its steps when another destination opens: every Back shows something else, and a Docs step reopens Docs', async () => {
   const view = render(RetainedDocs, {});
   await fireEvent.click(view.getByRole('button', { name: 'Changelog' }));
   await waitFor(() => expect(nav.stack).toHaveLength(2));
   await view.rerender({ initialItemId: 'getting-started/updates' });
   await waitFor(() => expect(title(view)).toBe('Updates'));
   expect(nav.stack).toHaveLength(3);

   layout.openFeaturePage('profile');
   await fireEvent.click(view.getByRole('button', { name: 'Open detail' }));
   await waitFor(() => expect(page.state.viewPanelDetail).toBeTruthy());
   expect(nav.stack).toHaveLength(4);

   // Back closes the other destination's detail, in one step.
   history.back();
   await waitFor(() => expect(pane(view, 'Probe')).toBe('panel'));
   expect(shown()).toBe('profile');
   // The next Back lands on a Docs step: Docs opens on the document that entry showed.
   history.back();
   await waitFor(() => expect(shown()).toBe('docs'));
   await waitFor(() => expect(title(view)).toBe('Changelog'));
   history.back();
   await waitFor(() => expect(pane(view, 'Docs')).toBe('panel'));
   expect(nav.at).toBe(0);
  });

  it('opens on an entry another destination wrote, also after stepping through it', async () => {
   const { pushState } = await import('$app/navigation');
   const view = render(RetainedDocs, {});
   await waitFor(() => expect(page.state.docsDoc).toBe(''));
   // Settings' own step names no document: Back from it stays in Settings.
   layout.openFeaturePage('settings');
   pushState('', { settingsView: 'page' } as App.PageState);
   go(-1);
   await waitFor(() => expect(page.state.docsDoc).toBe(''));
   expect(shown()).toBe('settings');
   go(1);
   layout.openFeaturePage('docs');
   await waitFor(() => expect(page.state).toEqual({ settingsView: 'page', docsDoc: '', docsStep: false }));
   expect(pane(view, 'Docs')).toBe('panel');
   await fireEvent.click(view.getByRole('button', { name: 'FAQ' }));
   await waitFor(() => expect(title(view)).toBe('Frequently asked questions'));
   expect(nav.stack).toHaveLength(3);
  });
 });
});
