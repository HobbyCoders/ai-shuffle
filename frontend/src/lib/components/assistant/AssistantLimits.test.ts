import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import type { AssistantLimits as Limits } from '$lib/api/assistant';
const mocks = vi.hoisted(() => ({ getAssistantLimits: vi.fn(), updateAssistantLimits: vi.fn() }));
vi.mock('$lib/api/assistant', async original => ({ ...await original<typeof import('$lib/api/assistant')>(), ...mocks }));
import AssistantLimits from './AssistantLimits.svelte';

let server: Limits;
const base = (): Limits => ({
 spend_limit_enabled: true, daily_spend_limit_usd: 20, job_limit_enabled: true, max_job_minutes: 120,
 spent_today_usd: 4.2, day: '2026-10-03', timezone: 'UTC', resets_at: '2026-10-04T00:00:00+00:00',
 limit_reached: false, warning: false,
});
beforeEach(() => {
 vi.clearAllMocks(); server = base();
 mocks.getAssistantLimits.mockImplementation(async () => ({ ...server }));
 mocks.updateAssistantLimits.mockImplementation(async patch => {
  const { confirm_spend_limit_off: _confirm, ...changes } = patch;
  server = { ...server, ...changes }; return { ...server };
 });
});
afterEach(cleanup);

const spendSwitch = () => screen.getByRole('switch', { name: 'Daily spending limit' });
const jobSwitch = () => screen.getByRole('switch', { name: 'Longest background job' });

describe('assistant limits', () => {
 it('shows today\'s spend against the limit and both fields with short helpers', async () => {
  render(AssistantLimits);
  expect(await screen.findByText(/of \$20\.00 spent today/)).toBeTruthy();
  expect(screen.getByText('$4.20')).toBeTruthy();
  expect(screen.getByRole('progressbar', { name: 'Spent today' }).getAttribute('aria-valuenow')).toBe('21');
  const amount = screen.getByRole('spinbutton', { name: 'Daily spending limit' }) as HTMLInputElement;
  expect(amount.value).toBe('20');
  const job = screen.getByRole('combobox', { name: 'Longest background job' }) as HTMLSelectElement;
  expect(job.value).toBe('120');
  expect(job.selectedOptions[0].textContent).toBe('2 hours');
  for (const helper of ['All work, at API prices', 'Applies to each job']) {
   expect(screen.getByText(helper)).toBeTruthy();
   expect(helper.split(/\s+/).length).toBeLessThanOrEqual(5);
  }
  expect(spendSwitch().getAttribute('aria-checked')).toBe('true');
  expect(jobSwitch().getAttribute('aria-checked')).toBe('true');
 });

 it('saves a new daily amount and job time', async () => {
  render(AssistantLimits);
  const amount = await screen.findByRole('spinbutton', { name: 'Daily spending limit' });
  await fireEvent.input(amount, { target: { value: '35' } });
  await fireEvent.change(amount);
  await waitFor(() => expect(mocks.updateAssistantLimits).toHaveBeenCalledWith({ daily_spend_limit_usd: 35 }));
  expect(await screen.findByText(/of \$35\.00 spent today/)).toBeTruthy();
  await fireEvent.change(screen.getByRole('combobox', { name: 'Longest background job' }), { target: { value: '60' } });
  await waitFor(() => expect(mocks.updateAssistantLimits).toHaveBeenLastCalledWith({ max_job_minutes: 60 }));
  expect(screen.getByRole('status').textContent).toContain('Saved');
 });

 it('refuses an out-of-range amount without saving', async () => {
  render(AssistantLimits);
  const amount = await screen.findByRole('spinbutton', { name: 'Daily spending limit' });
  await fireEvent.input(amount, { target: { value: '0' } });
  await fireEvent.change(amount);
  expect((await screen.findByRole('alert')).textContent).toContain('$0.50 to $10,000');
  expect(mocks.updateAssistantLimits).not.toHaveBeenCalled();
 });

 it('asks before switching the spending limit off, and keeping it changes nothing', async () => {
  render(AssistantLimits);
  await fireEvent.click(await screen.findByRole('switch', { name: 'Daily spending limit' }));
  expect(screen.getByText('Turn off the daily spending limit?')).toBeTruthy();
  expect(mocks.updateAssistantLimits).not.toHaveBeenCalled();
  await fireEvent.click(screen.getByRole('button', { name: 'Keep it on' }));
  expect(screen.queryByText('Turn off the daily spending limit?')).toBeNull();
  expect(mocks.updateAssistantLimits).not.toHaveBeenCalled();
  expect(spendSwitch().getAttribute('aria-checked')).toBe('true');
 });

 it('switches the spending limit off only with the confirmation, then back on', async () => {
  render(AssistantLimits);
  await fireEvent.click(await screen.findByRole('switch', { name: 'Daily spending limit' }));
  await fireEvent.click(screen.getByRole('button', { name: 'Turn off limit' }));
  await waitFor(() => expect(mocks.updateAssistantLimits).toHaveBeenCalledWith({ spend_limit_enabled: false, confirm_spend_limit_off: true }));
  expect(await screen.findByText(/spent today · no daily limit/)).toBeTruthy();
  expect((screen.getByRole('spinbutton', { name: 'Daily spending limit' }) as HTMLInputElement).disabled).toBe(true);
  await fireEvent.click(spendSwitch());
  await waitFor(() => expect(mocks.updateAssistantLimits).toHaveBeenLastCalledWith({ spend_limit_enabled: true }));
  expect(screen.queryByText('Turn off the daily spending limit?')).toBeNull();
 });

 it('switches the job time limit off without a confirmation', async () => {
  render(AssistantLimits);
  await fireEvent.click(await screen.findByRole('switch', { name: 'Longest background job' }));
  await waitFor(() => expect(mocks.updateAssistantLimits).toHaveBeenCalledWith({ job_limit_enabled: false }));
  await waitFor(() => expect(jobSwitch().getAttribute('aria-checked')).toBe('false'));
  expect((screen.getByRole('combobox', { name: 'Longest background job' }) as HTMLSelectElement).disabled).toBe(true);
 });

 it('says plainly when the limit is reached', async () => {
  server = { ...base(), spent_today_usd: 21, limit_reached: true, warning: true };
  render(AssistantLimits);
  expect(await screen.findByText('Limit reached. New work waits until tomorrow.')).toBeTruthy();
 });

 it('keeps a job time set elsewhere selectable', async () => {
  server = { ...base(), max_job_minutes: 45 };
  render(AssistantLimits);
  const job = await screen.findByRole('combobox', { name: 'Longest background job' }) as HTMLSelectElement;
  expect(job.selectedOptions[0].textContent).toBe('45 minutes');
 });

 it('shows a save failure and restores the saved amount', async () => {
  mocks.updateAssistantLimits.mockRejectedValue({ detail: 'Daily spending limit must be between $0.5 and $10000' });
  render(AssistantLimits);
  const amount = await screen.findByRole('spinbutton', { name: 'Daily spending limit' }) as HTMLInputElement;
  await fireEvent.input(amount, { target: { value: '30' } });
  await fireEvent.change(amount);
  expect((await screen.findByRole('alert')).textContent).toContain('Daily spending limit must be');
  await waitFor(() => expect(amount.value).toBe('20'));
 });
});
