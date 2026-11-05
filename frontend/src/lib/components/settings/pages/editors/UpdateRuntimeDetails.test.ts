import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
const transport = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: transport, getErrorMessage: (_e: unknown, fallback: string) => fallback }));
import UpdateRuntimeDetails from './UpdateRuntimeDetails.svelte';
import { UpdatesState, type UpdateRuntimeStatus } from '../updates-state.svelte';
import { SETTINGS_ACCESS_CONTEXT } from '$lib/settings/access.svelte';

const app = 'a'.repeat(40);
const resident = 'b'.repeat(40);
const status = (extra: Partial<UpdateRuntimeStatus> = {}): UpdateRuntimeStatus => ({
  allow_updates: true, supported: true, phase: 'idle', running_version: app,
  updater: { revision: resident, capabilities: ['refresh-updater-v1'], refresh: 'available' }, ...extra
});
let updates: UpdatesState;
beforeEach(() => { vi.resetAllMocks(); updates = new UpdatesState(); });
afterEach(() => { cleanup(); updates.destroy(); });
async function show(extra: Partial<UpdateRuntimeStatus> = {}) {
  updates.runtime = status(extra);
  updates.policy = { allow_updates: extra.allow_updates ?? true, update_all_workers: true };
  updates.status = 'ok';
  render(UpdateRuntimeDetails, { updates });
  await fireEvent.click(screen.getByRole('button', { name: 'Update details' }));
}

it.each([['connected', 'not-a-revision'], ['peer', 'not-a-revision'], ['connected', null], ['peer', null]] as const)('defensively renders controlled %s identity %s as unknown without stages or actions', async (entry, revision) => {
  const malformed = {
    allow_updates: true, update_all_workers: true, supported: true, phase: 'idle', running_version: app,
    updater: { revision, capabilities: ['refresh-updater-v1'], refresh: 'available' },
    diagnostics: { revision: 'bogus', total_elapsed_ms: -50, stages: 'malformed' }
  } as unknown as UpdateRuntimeStatus;
  if (entry === 'connected') {
    updates.runtime = malformed; updates.policy = { allow_updates: true, update_all_workers: true }; updates.status = 'ok';
    render(UpdateRuntimeDetails, { updates });
  } else render(UpdateRuntimeDetails, { status: malformed, workerName: 'Other worker' });
  await fireEvent.click(screen.getByRole('button', { name: entry === 'connected' ? 'Update details' : 'Update details for Other worker' }));
  expect(screen.queryByText(/A newer updater can be loaded/)).toBeNull();
  expect(screen.getByText('Unknown')).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Refresh updater…' })).toBeNull();
  expect(screen.queryByRole('list', { name: 'Preparation stages' })).toBeNull();
  expect(screen.getByText('No update timings reported yet.')).toBeTruthy();
});

it.each(['connected', 'peer'] as const)('keeps a valid legacy available %s report read-only with no shortcut or confirmation', async (entry) => {
  if (entry === 'connected') await show();
  else {
    render(UpdateRuntimeDetails, { status: status(), workerName: 'Other worker' });
    await fireEvent.click(screen.getByRole('button', { name: 'Update details for Other worker' }));
  }
  expect(screen.getByText('aaaaaaaaaaaa')).toBeTruthy();
  expect(screen.getByText('bbbbbbbbbbbb')).toBeTruthy();
  expect(screen.getByText(/separately operator-approved container restart/)).toBeTruthy();
  expect(screen.getByRole('region', { name: entry === 'connected' ? 'Connected worker update details' : 'Other worker update details' }).querySelector('button')).toBeNull();
  expect(screen.queryByText('Restart the connected worker to refresh its updater?')).toBeNull();
  expect(transport.post).not.toHaveBeenCalled();
});

it('separates app and updater identity and never infers current from the app', async () => {
  await show({ updater: null });
  expect(screen.getByText('aaaaaaaaaaaa')).toBeTruthy();
  expect(screen.getByText('Unknown')).toBeTruthy();
  expect(screen.getByText(/App version alone cannot confirm/)).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Refresh updater…' })).toBeNull();
  expect(screen.getByText('No update timings reported yet.')).toBeTruthy();
});

it.each([
  ['current', /matches the active app revision/],
  ['container_restart_required', /separately operator-approved container restart/],
  ['image_update_required', /does not mean the app or image is out of date/],
  ['disabled', /Updater changes are disabled/],
  ['unknown', /does not report its running updater revision/]
] as const)('explains %s without offering a refresh action', async (refresh, text) => {
  await show({ updater: { revision: resident, capabilities: [], refresh } });
  expect(screen.getByText(text)).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Refresh updater…' })).toBeNull();
});

it('shows nested wall time, outcomes and fallback without raw producer text', async () => {
  await show({ diagnostics: { revision: app, total_elapsed_ms: 1500, stages: [
    { stage: 'assembly', elapsed_ms: 1400, outcome: 'completed' },
    { stage: 'tools_cache', elapsed_ms: 20, outcome: 'hit' },
    { stage: 'python_cache', elapsed_ms: 10, outcome: 'miss' },
    { stage: 'prebuilt_ui', elapsed_ms: 30, outcome: 'fallback' },
    { stage: 'frontend_cache', elapsed_ms: 0, outcome: 'disabled' },
    { stage: 'frontend_build', elapsed_ms: 60_000, outcome: 'failed' }
  ] } });
  expect(screen.getByText('1.5 s total')).toBeTruthy();
  expect(screen.getByText(/Stages can overlap/)).toBeTruthy();
  for (const text of ['Reused verified cache', 'No reusable verified entry', 'Fallback to source build', 'Not offered or reuse disabled', 'Failed', '1 min 0 s']) expect(screen.getByText(text)).toBeTruthy();
  expect(document.body.textContent).not.toContain('/private');
});

it.each([{ allow_updates: false }, { request_pending: true }, { install_pending: true }, { phase: 'downloading' }, { phase: 'error' }])('keeps paused, pending and failed reports read-only for %j', async (extra) => {
  await show(extra);
  expect(screen.queryByRole('button', { name: /Refresh updater|Refresh and restart/ })).toBeNull();
  expect(screen.getByRole('region', { name: 'Connected worker update details' }).querySelector('[role="group"]')).toBeNull();
  if (extra.allow_updates === false) expect(screen.getByText('In-app updates are paused.')).toBeTruthy();
  if (extra.request_pending || extra.install_pending) expect(screen.getByText('An update action is pending.')).toBeTruthy();
  expect(transport.post).not.toHaveBeenCalled();
});

it('shows read-only peer metadata without a local refresh action', async () => {
  render(UpdateRuntimeDetails, { status: status(), workerName: 'Other worker' });
  await fireEvent.click(screen.getByRole('button', { name: 'Update details for Other worker' }));
  expect(screen.getByRole('region', { name: 'Other worker update details' })).toBeTruthy();
  expect(screen.getByText('bbbbbbbbbbbb')).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Refresh updater…' })).toBeNull();
});

it('keeps legacy available metadata read-only under Settings approval', async () => {
  updates.runtime = status(); updates.policy = { allow_updates: true, update_all_workers: false }; updates.status = 'ok';
  render(UpdateRuntimeDetails, { props: { updates }, context: new Map([[SETTINGS_ACCESS_CONTEXT, { readOnly: true }]]) });
  await fireEvent.click(screen.getByRole('button', { name: 'Update details' }));
  expect(screen.getByText('bbbbbbbbbbbb')).toBeTruthy();
  expect(screen.queryByRole('button', { name: /Refresh updater|Refresh and restart/ })).toBeNull();
  expect(transport.post).not.toHaveBeenCalled();
});

it('keeps the last report visible and read-only while status is stale', async () => {
  await show();
  updates.runtimeStale = true;
  await waitFor(() => expect(screen.getByText('Worker status is temporarily unavailable. Showing the last report.')).toBeTruthy());
  expect(screen.getByText('bbbbbbbbbbbb')).toBeTruthy();
  expect(screen.getByRole('region', { name: 'Connected worker update details' }).querySelector('button')).toBeNull();
  expect(transport.post).not.toHaveBeenCalled();
});

it('renders loading, failure and recovery states', async () => {
  render(UpdateRuntimeDetails, { updates });
  await fireEvent.click(screen.getByRole('button', { name: 'Update details' }));
  expect(screen.getByText('Loading update details…')).toBeTruthy();
  updates.status = 'error';
  await waitFor(() => expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy());
  transport.get.mockResolvedValueOnce(status());
  await fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
  await waitFor(() => expect(screen.getByText('bbbbbbbbbbbb')).toBeTruthy());
});
