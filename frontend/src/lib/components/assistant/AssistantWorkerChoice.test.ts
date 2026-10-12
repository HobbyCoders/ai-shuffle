import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import type { AssistantWorkerChoice as Choice } from '$lib/api/assistant';
const mocks = vi.hoisted(() => ({ getAssistantWorker: vi.fn(), updateAssistantWorker: vi.fn() }));
vi.mock('$lib/api/assistant', async original => ({ ...await original<typeof import('$lib/api/assistant')>(), ...mocks }));
import AssistantWorkerChoice from './AssistantWorkerChoice.svelte';

const UNRAID = { device_id: 'dev-serverworker', name: 'ServerWorker', online: true, remote_task: true };
const OLD = { device_id: 'dev-old', name: 'OldBox', online: true, remote_task: false };
const LAPTOP = { device_id: 'dev-laptop', name: 'Laptop', online: false, remote_task: true };
let server: Choice;
beforeEach(() => {
 vi.clearAllMocks();
 server = { device_id: null, workers: [UNRAID] };
 mocks.getAssistantWorker.mockImplementation(async () => ({ ...server }));
 mocks.updateAssistantWorker.mockImplementation(async (id: string | null) => { server = { ...server, device_id: id }; return { ...server }; });
});
afterEach(cleanup);

const picker = () => screen.findByRole('combobox', { name: 'Run work on' }) as Promise<HTMLSelectElement>;

describe('assistant work worker', () => {
 it('shows nothing when this is the only worker', async () => {
  server = { device_id: null, workers: [] };
  const { container } = render(AssistantWorkerChoice);
  await waitFor(() => expect(mocks.getAssistantWorker).toHaveBeenCalled());
  await Promise.resolve();
  expect(screen.queryByRole('combobox')).toBeNull();
  expect(container.textContent?.trim()).toBe('');
 });

 it('shows nothing when the read fails', async () => {
  mocks.getAssistantWorker.mockRejectedValue(new Error('down'));
  render(AssistantWorkerChoice);
  await waitFor(() => expect(mocks.getAssistantWorker).toHaveBeenCalled());
  await Promise.resolve();
  expect(screen.queryByRole('combobox')).toBeNull();
  expect(screen.queryByRole('alert')).toBeNull();
 });

 it('offers this worker and each other worker, with a short helper', async () => {
  server = { device_id: 'dev-serverworker', workers: [LAPTOP, OLD, UNRAID] };
  render(AssistantWorkerChoice);
  const select = await picker();
  expect(select.value).toBe('dev-serverworker');
  const options = [...select.options].map(o => [o.textContent, o.disabled]);
  expect(options).toEqual([['This worker', false], ['Laptop · Offline', true], ['OldBox · Needs update', true], ['ServerWorker', false]]);
  const helper = screen.getByText('Where background jobs run');
  expect(helper.textContent!.split(/\s+/).length).toBeLessThanOrEqual(5);
  // Not an agent picker: the home tests rely on none existing.
  expect(screen.queryByRole('combobox', { name: /agent/i })).toBeNull();
 });

 it('saves a pick and clears back to this worker', async () => {
  render(AssistantWorkerChoice);
  const select = await picker();
  expect(select.value).toBe('');
  await fireEvent.change(select, { target: { value: 'dev-serverworker' } });
  await waitFor(() => expect(mocks.updateAssistantWorker).toHaveBeenCalledWith('dev-serverworker'));
  expect((await screen.findByRole('status')).textContent).toContain('Saved');
  await fireEvent.change(select, { target: { value: '' } });
  await waitFor(() => expect(mocks.updateAssistantWorker).toHaveBeenLastCalledWith(null));
 });

 it('restores the saved choice when the server refuses', async () => {
  mocks.updateAssistantWorker.mockRejectedValue({ detail: 'ServerWorker is offline; it was not saved.' });
  render(AssistantWorkerChoice);
  const select = await picker();
  await fireEvent.change(select, { target: { value: 'dev-serverworker' } });
  expect((await screen.findByRole('alert')).textContent).toBe('ServerWorker is offline; it was not saved.');
  expect(select.value).toBe('');
  expect(select.disabled).toBe(false);
 });
});
