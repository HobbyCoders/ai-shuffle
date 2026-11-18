import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor, within } from '@testing-library/svelte';
import { tick } from 'svelte';
import { page } from '$app/state';
import { preloadAllDocs } from '$lib/docs/index';
import { FEATURE_PANEL_CONTEXT } from '$lib/components/layout/feature-panel-context';
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

// The router, as SvelteKit's: a shallow goto pushes an entry with its state
// (persistState keeps it across a reload, so page.state survives a remount);
// Back/Forward set page.state to the entry's. ViewShell's own pushState is never
// reached here (no layout, so never stacked): it throws if it were.
const router = vi.hoisted(() => ({ entries: [{}] as Record<string, unknown>[], index: 0 }));
vi.mock('$app/navigation', async () => {
 const { page } = await import('$app/state');
 return {
  pushState: () => { throw new Error('No ViewShell stop in this component test'); },
  goto: async (_url: string, opts: { shallow?: boolean; persistState?: boolean; state?: Record<string, unknown> }) => {
   if (!opts?.shallow || !opts.persistState) throw new Error('Docs writes persisted shallow entries only');
   router.entries.splice(router.index + 1, Infinity, opts.state ?? {});
   router.index++;
   page.state = opts.state ?? {};
  }
 };
});
async function go(delta: number) {
 router.index += delta;
 page.state = router.entries[router.index];
 await tick();
}

describe('Docs headings, search marks and reload (#1476, #1478)', () => {
 const width = window.innerWidth;
 const title = (view: { container: HTMLElement }) => view.container.querySelector('.docs-content h1')?.textContent;
 beforeEach(async () => {
  await preloadAllDocs();
  sessionStorage.clear();
  router.entries = [{}]; router.index = 0; page.state = {};
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
 });
 afterEach(() => {
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

 it('reopens the open document after a reload on a wide screen; a phone opens on the list', async () => {
  Object.defineProperty(window, 'innerWidth', { value: 1440, configurable: true });
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
  await waitFor(() => expect(phone.container.querySelector('[data-view-shell="Docs"]')?.getAttribute('data-pane')).toBe('panel'));
  expect(phone.container.querySelector('.docs-content h1')).toBeNull();
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

describe('Docs browser history in the web UI (#1477)', () => {
 const width = window.innerWidth;
 const START = 'getting-started/_index', FAQ = 'getting-started/faq', CHANGELOG = 'getting-started/changelog';
 const title = (view: { container: HTMLElement }) => view.container.querySelector('.docs-content h1')?.textContent ?? null;
 const articles = () => router.entries.map(e => e.docsArticle ?? null);
 const at = (w: number) => Object.defineProperty(window, 'innerWidth', { value: w, configurable: true });
 beforeEach(async () => {
  await preloadAllDocs();
  sessionStorage.clear();
  router.entries = [{}]; router.index = 0; page.state = {};
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
  vi.spyOn(window.history, 'back').mockImplementation(() => { void go(-1); });
 });
 afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  at(width);
  sessionStorage.clear();
 });

 it('each pick is an entry: Back shows the previous article, then what Docs showed under them; Forward again', async () => {
  at(1440);
  const view = render(DocsCard, {});
  await waitFor(() => expect(title(view)).toBe('AI Shuffle documentation'));
  expect(articles()).toEqual([null]);
  await fireEvent.click(view.getByRole('button', { name: 'FAQ' }));
  await fireEvent.click(view.getByRole('button', { name: 'Changelog' }));
  expect(articles()).toEqual([null, FAQ, CHANGELOG]);
  await go(-1);
  expect(title(view)).toBe('Frequently asked questions');
  await go(-1);
  expect(title(view)).toBe('AI Shuffle documentation');
  await go(1);
  expect(title(view)).toBe('Frequently asked questions');
 });

 it('in a narrow window Back from the first article shows the list, and the pane Back is the browser Back', async () => {
  at(500);
  const view = render(DocsCard, {});
  expect(title(view)).toBeNull();
  await fireEvent.click(view.getByRole('button', { name: 'FAQ' }));
  // A link in the article opens another: a step inside Docs.
  await view.rerender({ initialItemId: CHANGELOG });
  await waitFor(() => expect(title(view)).toBe('Changelog'));
  expect(articles()).toEqual([null, FAQ, CHANGELOG]);
  await fireEvent.click(view.getByRole('button', { name: 'Back to Docs' }));
  await waitFor(() => expect(title(view)).toBe('Frequently asked questions'));
  expect(router.index).toBe(1);
  await fireEvent.click(view.getByRole('button', { name: 'Back to Docs' }));
  await waitFor(() => expect(title(view)).toBeNull());
  expect(router.index).toBe(0);
  expect(view.getByRole('button', { name: 'FAQ' })).toBeTruthy();
  await go(1);
  expect(title(view)).toBe('Frequently asked questions');
 });

 it('copies the state under it, but never a panel’s Back-stop marker', async () => {
  at(1440);
  const under: Record<string, unknown> = { mobileDrawer: false, viewPanelDetail: 'other-panel' };
  router.entries = [under]; page.state = under;
  const view = render(DocsCard, {});
  await waitFor(() => expect(title(view)).toBe('AI Shuffle documentation'));
  await fireEvent.click(view.getByRole('button', { name: 'FAQ' }));
  expect(router.entries[1]).toEqual({ mobileDrawer: false, docsArticle: FAQ });
 });

 it('a reload on an article’s entry reopens that article, at any width', async () => {
  at(1440);
  const view = render(DocsCard, {});
  await waitFor(() => expect(title(view)).toBe('AI Shuffle documentation'));
  await fireEvent.click(view.getByRole('button', { name: 'FAQ' }));
  await fireEvent.click(view.getByRole('button', { name: 'Changelog' }));
  await go(-1);
  cleanup();
  at(500);
  const reloaded = render(DocsCard, {});
  await waitFor(() => expect(title(reloaded)).toBe('Frequently asked questions'));
  expect(articles()).toEqual([null, FAQ, CHANGELOG]);
  // …and Back from it still steps inside Docs, to the list (what showed under it is not known after a reload).
  await go(-1);
  expect(title(reloaded)).toBeNull();
 });

 it('choosing the article on screen is no step: the wide default, or the entry’s own article', async () => {
  at(1440);
  const view = render(DocsCard, {});
  await waitFor(() => expect(title(view)).toBe('AI Shuffle documentation'));
  await fireEvent.click(view.getByRole('button', { name: 'Start Here' }));
  expect(articles()).toEqual([null]);
  await fireEvent.click(view.getByRole('button', { name: 'FAQ' }));
  await fireEvent.click(view.getByRole('button', { name: 'FAQ' }));
  expect(articles()).toEqual([null, FAQ]);
 });

 it('widened from the list, picking the article the default would show is a step', async () => {
  at(500);
  const view = render(DocsCard, {});
  expect(title(view)).toBeNull();
  at(1000);
  window.dispatchEvent(new Event('resize'));
  await tick();
  expect(view.getByText('Choose a document')).toBeTruthy();
  await fireEvent.click(view.getByRole('button', { name: 'Start Here' }));
  expect(articles()).toEqual([null, START]);
  await go(-1);
  expect(title(view)).toBeNull();
 });

 it('off screen, Docs ignores Back/Forward to its entries', async () => {
  at(1440);
  const view = render(DocsCard, { props: {}, context: new Map([[FEATURE_PANEL_CONTEXT, { active: false }]]) });
  await waitFor(() => expect(title(view)).toBe('AI Shuffle documentation'));
  router.entries.push({ docsArticle: FAQ });
  await go(1);
  expect(title(view)).toBe('AI Shuffle documentation');
  await go(-1);
  expect(title(view)).toBe('AI Shuffle documentation');
 });

 it('a link arriving from elsewhere shows its article on the entry it is on; a link inside Docs is a step', async () => {
  at(1440);
  const view = render(DocsCard, { initialItemId: FAQ });
  await waitFor(() => expect(title(view)).toBe('Frequently asked questions'));
  expect(articles()).toEqual([null]);
  await view.rerender({ initialItemId: null });
  await view.rerender({ initialItemId: CHANGELOG });
  await waitFor(() => expect(title(view)).toBe('Changelog'));
  expect(articles()).toEqual([null, CHANGELOG]);
  // Back shows the linked article again: what Docs showed on the entry under the step.
  await go(-1);
  expect(title(view)).toBe('Frequently asked questions');
 });

 it('the phone app writes no Docs entries (ViewShell keeps its own detail stop, as on main)', async () => {
  at(390);
  const view = render(DocsCard, { mobile: true });
  await fireEvent.click(view.getByRole('button', { name: 'FAQ' }));
  await waitFor(() => expect(title(view)).toBe('Frequently asked questions'));
  expect(articles()).toEqual([null]);
  const linked: Record<string, unknown> = { docsArticle: CHANGELOG };
  page.state = linked;
  await tick();
  expect(title(view)).toBe('Frequently asked questions');
 });
});
