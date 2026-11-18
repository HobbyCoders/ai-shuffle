import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor, within } from '@testing-library/svelte';
import { tick } from 'svelte';
import { page } from '$app/state';
import { preloadAllDocs } from '$lib/docs/index';
import { SvelteMap } from 'svelte/reactivity';
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

// The router, as SvelteKit's: a shallow goto first resolves its route (so the
// entry lands after the click has returned; `hold` keeps it from landing until
// released), then pushes the entry and sets page.state. SvelteKit numbers each
// entry by its position (`historyIndex`, kept across a reload); persistState
// keeps an entry's state across a reload, any other entry reloads empty.
// Back/Forward set page.state to the entry's. ViewShell's own pushState is never
// reached here (no layout, so never stacked): it throws if it were.
type Entry = { state: Record<string, unknown>; persist: boolean };
const router = vi.hoisted(() => {
 const r = {
  entries: [] as Entry[],
  index: 0,
  hold: null as null | (() => void)[],
  mark() { history.replaceState({ 'sveltekit:metadata': { historyIndex: r.index } }, ''); },
 };
 return r;
});
vi.mock('$app/navigation', async () => {
 const { page } = await import('$app/state');
 return {
  pushState: () => { throw new Error('No ViewShell stop in this component test'); },
  goto: async (_url: string, opts: { shallow?: boolean; persistState?: boolean; state?: Record<string, unknown> }) => {
   if (!opts?.shallow || !opts.persistState) throw new Error('Docs writes persisted shallow entries only');
   if (router.hold) await new Promise<void>(land => router.hold!.push(land));
   else await Promise.resolve();
   router.entries.splice(router.index + 1, Infinity, { state: opts.state ?? {}, persist: true });
   router.index++;
   router.mark();
   page.state = opts.state ?? {};
  }
 };
});
/** Start on these entries (persisted unless listed in `volatile`), standing on the last. */
function startOn(states: Record<string, unknown>[] = [{}], volatile: number[] = []) {
 router.entries = states.map((state, i) => ({ state, persist: !volatile.includes(i) }));
 router.index = states.length - 1;
 router.hold = null;
 router.mark();
 page.state = router.entries[router.index].state;
}
async function go(delta: number) {
 router.index += delta;
 router.mark();
 page.state = router.entries[router.index].state;
 await tick();
}
/** A document reload on the current entry: SvelteKit keeps only a persisted entry's state. */
function reload() {
 cleanup();
 const entry = router.entries[router.index];
 page.state = entry.persist ? entry.state : {};
}
/** Lets queued router writes land. */
const landed = () => new Promise(resolve => setTimeout(resolve, 0));

describe('Docs headings, search marks and reload (#1476, #1478)', () => {
 const width = window.innerWidth;
 const title = (view: { container: HTMLElement }) => view.container.querySelector('.docs-content h1')?.textContent;
 beforeEach(async () => {
  await preloadAllDocs();
  sessionStorage.clear();
  startOn();
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
 const REQUIREMENTS = 'getting-started/requirements';
 const articles = () => router.entries.map(e => e.state.docsArticle ?? null);
 const at = (w: number) => Object.defineProperty(window, 'innerWidth', { value: w, configurable: true });
 // FEATURE_PANEL_CONTEXT as AppPage gives it: whether Docs is the view on screen, reactive.
 function panel(active = true) {
  const on = new SvelteMap([['active', active]]);
  return { get active() { return on.get('active')!; }, set active(value: boolean) { on.set('active', value); } };
 }
 const inPanel = (feature: { active: boolean }, props: Record<string, unknown> = {}) =>
  render(DocsCard, { props, context: new Map([[FEATURE_PANEL_CONTEXT, feature]]) });
 beforeEach(async () => {
  await preloadAllDocs();
  sessionStorage.clear();
  startOn();
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
  await waitFor(() => expect(articles()).toEqual([null, FAQ, CHANGELOG]));
  // Its own entries landing were no step: the last pick stays on screen.
  expect(title(view)).toBe('Changelog');
  await go(-1);
  expect(title(view)).toBe('Frequently asked questions');
  await go(-1);
  expect(title(view)).toBe('AI Shuffle documentation');
  await go(1);
  expect(title(view)).toBe('Frequently asked questions');
 });

 it('two picks before the router writes either: the second stays on screen, and Back still reaches the first', async () => {
  at(1440);
  const view = render(DocsCard, {});
  await waitFor(() => expect(title(view)).toBe('AI Shuffle documentation'));
  router.hold = [];
  await fireEvent.click(view.getByRole('button', { name: 'FAQ' }));
  await fireEvent.click(view.getByRole('button', { name: 'Changelog' }));
  const queued = router.hold;
  router.hold = null;
  queued.shift()!();
  await landed();
  // The first entry landed; the pick after it is what shows.
  expect(articles()).toEqual([null, FAQ]);
  expect(title(view)).toBe('Changelog');
  queued.shift()!();
  await waitFor(() => expect(articles()).toEqual([null, FAQ, CHANGELOG]));
  expect(title(view)).toBe('Changelog');
  await go(-1);
  expect(title(view)).toBe('Frequently asked questions');
  await go(-1);
  expect(title(view)).toBe('AI Shuffle documentation');
 });

 it('in a narrow window Back from the first article shows the list, and the pane Back is the browser Back', async () => {
  at(500);
  const view = render(DocsCard, {});
  expect(title(view)).toBeNull();
  await fireEvent.click(view.getByRole('button', { name: 'Start Here' }));
  // A link in the article opens another: a pick inside Docs, so a step.
  await fireEvent.click(within(view.container.querySelector('.docs-content') as HTMLElement).getByRole('link', { name: 'Requirements' }));
  await waitFor(() => expect(title(view)).toBe('Requirements'));
  await waitFor(() => expect(articles()).toEqual([null, START, REQUIREMENTS]));
  await fireEvent.click(view.getByRole('button', { name: 'Back to Docs' }));
  await waitFor(() => expect(title(view)).toBe('AI Shuffle documentation'));
  expect(router.index).toBe(1);
  await fireEvent.click(view.getByRole('button', { name: 'Back to Docs' }));
  await waitFor(() => expect(title(view)).toBeNull());
  expect(router.index).toBe(0);
  expect(view.getByRole('button', { name: 'FAQ' })).toBeTruthy();
  await go(1);
  expect(title(view)).toBe('AI Shuffle documentation');
 });

 it('carries only Settings’ place from the entry under it: no chat marker, no overlay or panel marker', async () => {
  at(1440);
  const settings = { settingsView: 'page', settingsDepth: 1, settingsCategory: 'general' };
  startOn([{ chatSession: 'chat-b', mobileDrawer: false, ...settings }]);
  const view = render(DocsCard, {});
  await waitFor(() => expect(title(view)).toBe('AI Shuffle documentation'));
  await fireEvent.click(view.getByRole('button', { name: 'FAQ' }));
  await waitFor(() => expect(router.entries).toHaveLength(2));
  expect(router.entries[1].state).toEqual({ docsArticle: FAQ, docsBase: START, ...settings });
  await fireEvent.click(view.getByRole('button', { name: 'Changelog' }));
  await waitFor(() => expect(router.entries).toHaveLength(3));
  expect(router.entries[2].state).toEqual({ docsArticle: CHANGELOG, docsBase: START, ...settings });
 });

 it('while another panel’s Back stop is the entry (its pop pending), a pick shows in place: no entry for that pop to take', async () => {
  at(1440);
  // Notes in a narrow window left its detail open: leaving it pops that stop, later.
  startOn([{}, { viewPanelDetail: 'notes' }]);
  const view = render(DocsCard, {});
  await waitFor(() => expect(title(view)).toBe('AI Shuffle documentation'));
  await fireEvent.click(view.getByRole('button', { name: 'FAQ' }));
  await landed();
  expect(title(view)).toBe('Frequently asked questions');
  expect(articles()).toEqual([null, null]);
  // Notes' pop lands on the entry under its stop: still FAQ, and still no Docs entry.
  await go(-1);
  expect(title(view)).toBe('Frequently asked questions');
  expect(articles()).toEqual([null, null]);
  // A reload there shows what Docs showed on it.
  reload();
  const reloaded = render(DocsCard, {});
  await waitFor(() => expect(title(reloaded)).toBe('Frequently asked questions'));
 });

 it('a reload on an article’s entry reopens that article, at any width; Back then shows what the entry under them showed', async () => {
  at(1440);
  const view = render(DocsCard, {});
  await waitFor(() => expect(title(view)).toBe('AI Shuffle documentation'));
  await fireEvent.click(view.getByRole('button', { name: 'FAQ' }));
  await fireEvent.click(view.getByRole('button', { name: 'Changelog' }));
  await waitFor(() => expect(articles()).toEqual([null, FAQ, CHANGELOG]));
  await go(-1);
  // A reload empties the entry under Docs' steps (written by no one here): Docs' own entries keep what it showed.
  router.entries[0].persist = false;
  reload();
  at(500);
  const reloaded = render(DocsCard, {});
  await waitFor(() => expect(title(reloaded)).toBe('Frequently asked questions'));
  expect(articles()).toEqual([null, FAQ, CHANGELOG]);
  await go(-1);
  expect(title(reloaded)).toBe('AI Shuffle documentation');
  await go(1); await go(1);
  expect(title(reloaded)).toBe('Changelog');
 });

 it('a keyless entry’s article survives a reload on the step above it (Start Here → FAQ → reload → Back)', async () => {
  at(1440);
  const view = render(DocsCard, {});
  await waitFor(() => expect(title(view)).toBe('AI Shuffle documentation'));
  await fireEvent.click(view.getByRole('button', { name: 'FAQ' }));
  await waitFor(() => expect(articles()).toEqual([null, FAQ]));
  reload();
  const reloaded = render(DocsCard, {});
  await waitFor(() => expect(title(reloaded)).toBe('Frequently asked questions'));
  await go(-1);
  expect(title(reloaded)).toBe('AI Shuffle documentation');
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

 it('Back while Docs is hidden changes nothing there; shown again, Docs shows the entry it finds', async () => {
  at(1440);
  const feature = panel();
  const view = inPanel(feature);
  await waitFor(() => expect(title(view)).toBe('AI Shuffle documentation'));
  await fireEvent.click(view.getByRole('button', { name: 'FAQ' }));
  await fireEvent.click(view.getByRole('button', { name: 'Changelog' }));
  await waitFor(() => expect(articles()).toEqual([null, FAQ, CHANGELOG]));
  // Another view (Notes) on screen; Back to FAQ's entry there.
  feature.active = false;
  await tick();
  await go(-1);
  expect(title(view)).toBe('Changelog');
  feature.active = true;
  await tick();
  expect(title(view)).toBe('Frequently asked questions');
  // And Forward again while hidden, then shown: Changelog.
  feature.active = false;
  await tick();
  await go(1);
  feature.active = true;
  await tick();
  expect(title(view)).toBe('Changelog');
 });

 it('a reload while Docs is hidden restores nothing until it is shown, then shows the entry it is on', async () => {
  at(1440);
  // Reloaded on FAQ's entry (Docs showed Changelog before, in this tab), with another view on screen.
  sessionStorage.setItem('docs.open', CHANGELOG);
  startOn([{}, { docsArticle: FAQ, docsBase: '' }], [0]);
  const feature = panel(false);
  const view = inPanel(feature);
  await tick();
  expect(title(view)).toBeNull();
  expect(sessionStorage.getItem('docs.open')).toBe(CHANGELOG);
  // Back to the entry under it while hidden; then Docs is opened.
  await go(-1);
  feature.active = true;
  await tick();
  // That entry names nothing: main's wide reload, the article the tab last had open.
  await waitFor(() => expect(title(view)).toBe('Changelog'));
  expect(articles()).toEqual([null, FAQ]);
 });

 it('an article a link from elsewhere showed in place is what a reload on that entry shows', async () => {
  at(1440);
  const feature = panel();
  const view = inPanel(feature);
  await waitFor(() => expect(title(view)).toBe('AI Shuffle documentation'));
  await fireEvent.click(view.getByRole('button', { name: 'FAQ' }));
  await waitFor(() => expect(articles()).toEqual([null, FAQ]));
  // Wide Settings (no step), then its "How Cloud works" link opens Docs.
  feature.active = false;
  await tick();
  await view.rerender({ initialItemId: 'getting-started/cloud' });
  feature.active = true;
  await tick();
  await waitFor(() => expect(view.container.querySelector('.docs-content h1')?.textContent).not.toBe('Frequently asked questions'));
  const cloud = title(view);
  await landed();
  expect(articles()).toEqual([null, FAQ]);
  reload();
  const reloaded = render(DocsCard, {});
  await waitFor(() => expect(title(reloaded)).toBe(cloud));
 });

 it('a link from elsewhere is never a step, even with Docs on screen; a link inside an article is', async () => {
  at(1440);
  const view = render(DocsCard, { initialItemId: FAQ });
  await waitFor(() => expect(title(view)).toBe('Frequently asked questions'));
  await view.rerender({ initialItemId: null });
  // Global Spotlight, with Docs on screen.
  await view.rerender({ initialItemId: START });
  await waitFor(() => expect(title(view)).toBe('AI Shuffle documentation'));
  await landed();
  expect(articles()).toEqual([null]);
  await fireEvent.click(within(view.container.querySelector('.docs-content') as HTMLElement).getByRole('link', { name: 'Requirements' }));
  await waitFor(() => expect(articles()).toEqual([null, REQUIREMENTS]));
  expect(title(view)).toBe('Requirements');
  // Back shows the linked article again: what Docs showed on the entry under the step.
  await go(-1);
  expect(title(view)).toBe('AI Shuffle documentation');
 });

 it('the phone app writes no Docs entries (ViewShell keeps its own detail stop, as on main)', async () => {
  at(390);
  const view = render(DocsCard, { mobile: true });
  await fireEvent.click(view.getByRole('button', { name: 'FAQ' }));
  await waitFor(() => expect(title(view)).toBe('Frequently asked questions'));
  expect(articles()).toEqual([null]);
  // A link inside the article opens it as before, with no Docs entry either.
  await fireEvent.click(view.getByRole('button', { name: 'Back to Docs' }));
  await fireEvent.click(view.getByRole('button', { name: 'Start Here' }));
  await fireEvent.click(within(view.container.querySelector('.docs-content') as HTMLElement).getByRole('link', { name: 'Requirements' }));
  await waitFor(() => expect(title(view)).toBe('Requirements'));
  await landed();
  expect(articles()).toEqual([null]);
  const linked: Record<string, unknown> = { docsArticle: CHANGELOG };
  page.state = linked;
  await tick();
  expect(title(view)).toBe('Requirements');
 });
});
