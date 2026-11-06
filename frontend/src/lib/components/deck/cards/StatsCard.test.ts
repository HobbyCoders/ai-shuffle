import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
const api = vi.hoisted(() => ({ summary:vi.fn(), providers:vi.fn(), export:vi.fn() }));
vi.mock('$lib/api/client', () => ({
 getAnalyticsSummary:api.summary, getCostBreakdown:async()=>[], getUsageTrends:async()=>[], getTopSessions:async()=>[], getProviderBreakdown:api.providers, exportAnalyticsCSV:api.export
}));
import StatsCard from './StatsCard.svelte';
import { platform } from '$lib/stores/platform.svelte';
import { page } from '$app/state';
import { phonePanelActions } from '$lib/components/layout/view/ViewPanel.svelte';
beforeEach(() => { vi.stubGlobal('matchMedia', () => ({ matches: window.innerWidth <= 600, addEventListener() {}, removeEventListener() {} })); vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} }); platform.set('desktop'); page.state = {}; Object.defineProperty(window, 'innerWidth', { value: 1440, configurable: true }); });
afterEach(() => { cleanup(); vi.clearAllMocks(); platform.set('desktop'); vi.unstubAllGlobals(); });
describe('Stats shared view', () => {
 it('keeps range and export actions in the panel and uses the supplied totals unchanged', async () => {
  api.summary.mockResolvedValue({total_cost:1.25,total_input_tokens:100,total_output_tokens:25,session_count:3,average_cost_per_session:1.25/3,estimated_query_count:1});
  api.providers.mockResolvedValue([]);
  const view=render(StatsCard);
  await screen.findByText('$1.25');
  expect(view.container.querySelector('[data-view-shell="Stats"]')).toBeTruthy();
  expect(view.container.querySelector('.metrics.st-card')).toBeTruthy();
  expect(view.container.querySelectorAll('.stats-section .stats-content.st-card')).toHaveLength(4);
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
 it('shows unavailable provider data separately from zero usage', async () => {
  api.summary.mockResolvedValue(null); api.providers.mockRejectedValue(new Error('offline'));
  render(StatsCard);
  expect(await screen.findByText('Provider breakdown unavailable')).toBeTruthy();
  expect(screen.getByText('No chats')).toBeTruthy();
  expect(screen.getAllByText('$0.00')).toHaveLength(2);
 });
});
