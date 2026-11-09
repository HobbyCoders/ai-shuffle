import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
const api = vi.hoisted(() => ({ summary:vi.fn(), providers:vi.fn(), export:vi.fn(), costs:vi.fn(async():Promise<unknown[]>=>[]), trends:vi.fn(async():Promise<unknown[]>=>[]), top:vi.fn(async():Promise<unknown[]>=>[]) }));
vi.mock('$lib/api/client', () => ({
 getAnalyticsSummary:api.summary, getCostBreakdown:api.costs, getUsageTrends:api.trends, getTopSessions:api.top, getProviderBreakdown:api.providers, exportAnalyticsCSV:api.export
}));
/** `n` daily points ending today (the card's end date is today's UTC date). */
function trendDays(n: number) {
 const end = Date.parse(new Date().toISOString().slice(0, 10));
 return Array.from({ length: n }, (_, i) => ({ date: new Date(end - (n - 1 - i) * 864e5).toISOString().slice(0, 10), input_tokens: 1000, output_tokens: 100, cost: i === n - 1 ? 2.9 : 1 + (i % 3), estimated: i === 1 }));
}
const SUMMARY = {total_cost:1.25,total_input_tokens:100,total_output_tokens:25,session_count:3,average_cost_per_session:1.25/3,estimated_query_count:1};
import StatsCard from './StatsCard.svelte';
import { platform } from '$lib/stores/platform.svelte';
import { page } from '$app/state';
import { phonePanelActions } from '$lib/components/layout/view/ViewPanel.svelte';
beforeEach(() => { vi.stubGlobal('matchMedia', () => ({ matches: window.innerWidth <= 600, addEventListener() {}, removeEventListener() {} })); vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} }); platform.set('desktop'); page.state = {}; Object.defineProperty(window, 'innerWidth', { value: 1440, configurable: true }); });
afterEach(() => { cleanup(); vi.clearAllMocks(); api.costs.mockResolvedValue([]); api.trends.mockResolvedValue([]); api.top.mockResolvedValue([]); platform.set('desktop'); vi.unstubAllGlobals(); });
describe('Stats shared view', () => {
 it('keeps range and export actions in the panel and uses the supplied totals unchanged', async () => {
  api.summary.mockResolvedValue({total_cost:1.25,total_input_tokens:100,total_output_tokens:25,session_count:3,average_cost_per_session:1.25/3,estimated_query_count:1});
  api.providers.mockResolvedValue([]);
  const view=render(StatsCard);
  await screen.findByText('$1.25');
  expect(view.container.querySelector('[data-view-shell="Stats"]')).toBeTruthy();
  expect(view.container.querySelector('.metrics.st-card')).toBeTruthy();
  // No usage at all: one page-level empty state under the KPIs, not four empty cards.
  expect(view.container.querySelectorAll('.stats-section .stats-content.st-card')).toHaveLength(0);
  expect(screen.getByText('No usage in the last 30 days')).toBeTruthy();
  expect(screen.getByText('125')).toBeTruthy();
  expect(screen.getAllByText('est.').length).toBeGreaterThan(0);
  expect(screen.getByRole('button',{name:'7 days'}).closest('.view-panel')).toBeTruthy();
  await fireEvent.click(screen.getByRole('button',{name:'7 days'}));
  await waitFor(()=>expect(api.summary).toHaveBeenCalledTimes(2));
  await fireEvent.click(screen.getByRole('button',{name:'Export usage by provider as CSV'}));
  expect(api.export).toHaveBeenCalledOnce();
 });
 it.each(['narrow browser', 'phone app'])('shows KPIs immediately and keeps range and export reachable on %s', async mode => {
  if (mode === 'phone app') platform.set('mobile');
  else Object.defineProperty(window, 'innerWidth', { value: 390, configurable: true });
  api.summary.mockResolvedValue({total_cost:1.25,total_input_tokens:100,total_output_tokens:25,session_count:3,average_cost_per_session:1.25/3,estimated_query_count:1});
  api.providers.mockResolvedValue([]);
  const view = render(StatsCard);
  await screen.findByText('$1.25');
  expect(view.container.querySelector('[data-pane="detail"]')).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Back to Stats' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Usage by provider' })).toBeNull();
  expect(screen.getByRole('heading', { name: 'Last 30 days' })).toBeTruthy();
  expect(view.container.querySelectorAll('.metric')).toHaveLength(4);
  await fireEvent.click(screen.getByRole('button', { name: '7 days' }));
  await waitFor(() => expect(api.summary).toHaveBeenCalledTimes(2));
  {
   // Both phone shells render this action snippet in their top bar.
   // The real mobile shell renders this action snippet in its native bar;
   // the browser journey covers the actual export button there.
   expect(phonePanelActions.label).toBe('Stats');
   expect(phonePanelActions.content).toBeTruthy();
   expect(screen.queryByRole('button', { name: 'Export usage by provider as CSV' })).toBeNull();
  }
 });
 it('marks where a Jump to landed when nothing can scroll (#1220)', async () => {
  api.summary.mockResolvedValue(null); api.providers.mockRejectedValue(new Error('offline'));
  Element.prototype.scrollIntoView = vi.fn();
  const view = render(StatsCard);
  await screen.findByText('Provider breakdown unavailable');
  const row = screen.getAllByText('Usage by provider')[0].closest('[role="option"], button') as HTMLElement;
  await fireEvent.click(row);
  await waitFor(() => expect(view.container.querySelector('.stats-jumped')).toBeTruthy());
 });
 it('shows unavailable provider data separately from zero usage', async () => {
  api.summary.mockResolvedValue(null); api.providers.mockRejectedValue(new Error('offline'));
  render(StatsCard);
  expect(await screen.findByText('Provider breakdown unavailable')).toBeTruthy();
  expect(screen.getByText('No chat costs in this range')).toBeTruthy();
  expect(screen.getAllByText('$0.00')).toHaveLength(2);
 });
 it('offers 90 days from the empty state and drops the offer once it is the range', async () => {
  api.summary.mockResolvedValue(SUMMARY); api.providers.mockResolvedValue([]);
  render(StatsCard);
  await fireEvent.click(await screen.findByRole('button', { name: 'Show 90 days' }));
  expect(await screen.findByText('No usage in the last 90 days')).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Show 90 days' })).toBeNull();
 });
 it('collapses only the empty sections to a one-line row when some data exists', async () => {
  api.summary.mockResolvedValue(SUMMARY); api.providers.mockResolvedValue([]); api.trends.mockResolvedValue(trendDays(31));
  const view = render(StatsCard);
  await screen.findByRole('group', { name: /^Daily spend/ });
  expect(screen.queryByText(/^No usage in the last/)).toBeNull();
  const rows = [...view.container.querySelectorAll('.stats-empty-row')].map(r => r.textContent);
  expect(rows).toEqual(['Usage by providerNo usage in this range', 'Cost by profileNo profile costs in this range', 'Top chats by costNo chat costs in this range']);
 });
 it('draws one bar per day of the range, with zero days in the gaps and today last', async () => {
  // The API returns only days with usage: three of the 31, and not today.
  const sparse = trendDays(31).filter((_, i) => i === 0 || i === 10 || i === 20);
  api.summary.mockResolvedValue(SUMMARY); api.providers.mockResolvedValue([]); api.trends.mockResolvedValue(sparse);
  const view = render(StatsCard);
  const group = await screen.findByRole('group', { name: /^Daily spend/ });
  const cols = [...group.querySelectorAll<HTMLButtonElement>('.chart-col')];
  expect(cols).toHaveLength(31);
  const heights = cols.map(c => (c.querySelector('.chart-bar') as HTMLElement).style.height);
  expect(heights.filter(h => h !== '0%')).toHaveLength(3);
  expect(cols.at(-1)?.classList.contains('today')).toBe(true);
  expect(cols.at(-1)?.getAttribute('aria-label')).toMatch(/^Today, .+: \$0\.00$/);
  expect([...view.container.querySelectorAll('.chart-x span')].map(e => e.textContent).at(-1)).toBe('Today');
 });
 it('folds a range with tokens but no spend to the empty spend row, not an empty grid', async () => {
  api.summary.mockResolvedValue(SUMMARY); api.providers.mockResolvedValue([]);
  api.trends.mockResolvedValue(trendDays(4).map(d => ({ ...d, cost: 0 })));
  const view = render(StatsCard);
  await screen.findByText('No spend in this range');
  expect(screen.queryByRole('group', { name: /^Daily spend/ })).toBeNull();
  expect(view.container.querySelector('.chart-grid')).toBeNull();
  expect(screen.queryByText(/^No usage in the last/)).toBeNull();
 });
 it('labels the spend chart with a currency scale, a few dates and Today, and lets the keyboard pick a day', async () => {
  api.summary.mockResolvedValue(SUMMARY); api.providers.mockResolvedValue([]); api.trends.mockResolvedValue(trendDays(31));
  const view = render(StatsCard);
  const group = await screen.findByRole('group', { name: /^Daily spend/ });
  expect([...view.container.querySelectorAll('.chart-y span:not(.chart-y-sizer)')].map(e => e.textContent)).toEqual(['$0', '$1', '$2', '$3']);
  expect(view.container.querySelectorAll('.chart-grid i')).toHaveLength(3);
  const dates = [...view.container.querySelectorAll('.chart-x span')].map(e => e.textContent);
  expect(dates.length).toBeGreaterThanOrEqual(3);
  expect(dates.length).toBeLessThanOrEqual(5);
  expect(dates.at(-1)).toBe('Today');
  const cols = group.querySelectorAll<HTMLButtonElement>('.chart-col');
  expect(cols).toHaveLength(31);
  // One tab stop, resting on today, which is the full-strength bar.
  const stops = [...cols].filter(c => c.tabIndex === 0);
  expect(stops).toHaveLength(1);
  expect(stops[0].classList.contains('today')).toBe(true);
  expect(stops[0].getAttribute('aria-label')).toMatch(/^Today, .+: \$2\.90$/);
  expect(view.container.querySelector('.chart-readout')?.textContent).toContain('$2.90');
  stops[0].focus();
  await fireEvent.keyDown(stops[0], { key: 'Home' });
  expect(document.activeElement).toBe(cols[0]);
  await fireEvent.keyDown(cols[0], { key: 'ArrowRight' });
  expect(document.activeElement).toBe(cols[1]);
  expect(cols[1].getAttribute('aria-label')).toMatch(/includes estimated token counts$/);
  expect(view.container.querySelector('.chart-readout')?.textContent).toContain('Estimated');
  // Hover and tap pick a day too.
  await fireEvent.click(cols[5]);
  expect(cols[5].classList.contains('picked')).toBe(true);
 });
});
