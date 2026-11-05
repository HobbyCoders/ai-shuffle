import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
const api = vi.hoisted(() => ({ summary:vi.fn(), providers:vi.fn(), export:vi.fn() }));
vi.mock('$lib/api/client', () => ({
 getAnalyticsSummary:api.summary, getCostBreakdown:async()=>[], getUsageTrends:async()=>[], getTopSessions:async()=>[], getProviderBreakdown:api.providers, exportAnalyticsCSV:api.export
}));
import StatsCard from './StatsCard.svelte';
afterEach(() => { cleanup(); vi.clearAllMocks(); });
describe('Stats shared view', () => {
 it('keeps range and export actions in the panel and uses the supplied totals unchanged', async () => {
  api.summary.mockResolvedValue({total_cost:1.25,total_input_tokens:100,total_output_tokens:25,session_count:3,average_cost_per_session:1.25/3,estimated_query_count:1});
  api.providers.mockResolvedValue([]);
  const view=render(StatsCard);
  await screen.findByText('$1.25');
  expect(view.container.querySelector('[data-view-shell="Stats"]')).toBeTruthy();
  expect(screen.getByText('125')).toBeTruthy();
  expect(screen.getAllByText('est.').length).toBeGreaterThan(0);
  expect(screen.getByRole('button',{name:'7 days'}).closest('.view-panel')).toBeTruthy();
  await fireEvent.click(screen.getByRole('button',{name:'7 days'}));
  await waitFor(()=>expect(api.summary).toHaveBeenCalledTimes(2));
  await fireEvent.click(screen.getByRole('button',{name:'Export usage by provider as CSV'}));
  expect(api.export).toHaveBeenCalledOnce();
 });
 it('shows unavailable provider data separately from zero usage', async () => {
  api.summary.mockResolvedValue(null); api.providers.mockRejectedValue(new Error('offline'));
  render(StatsCard);
  expect(await screen.findByText('Provider breakdown unavailable')).toBeTruthy();
  expect(screen.getByText('No chats')).toBeTruthy();
  expect(screen.getAllByText('$0.00')).toHaveLength(2);
 });
});
