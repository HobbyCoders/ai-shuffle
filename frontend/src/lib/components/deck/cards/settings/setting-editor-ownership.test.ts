import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import type { AnyModel, ToolCategory } from '$lib/types/ai-tool-registry';

const mocks = vi.hoisted(() => ({ owner: 0, owners: new Set<(epoch: number) => void>(), get: vi.fn(), patch: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: { get: mocks.get, patch: mocks.patch }, getErrorMessage: () => 'Failed' }));
vi.mock('$lib/stores/session-owner', () => ({ sessionOwnerEpoch: () => mocks.owner,
 sessionOwnerBoundary: { subscribe(run: (epoch: number) => void) {
  run(mocks.owner); mocks.owners.add(run); return () => mocks.owners.delete(run);
 } }
}));
vi.mock('$lib/services/watch-resources', () => ({ watchResources: () => {} }));
vi.mock('svelte/transition', () => ({ slide: () => ({ duration: 0 }) }));
vi.mock('$lib/services/sttStream', () => ({ invalidateSttCapability: vi.fn() }));
vi.mock('$lib/stores/modelSchema', () => ({ modelSchemaStore: {
 fetch: async () => ({}),
 configurableFields: (schema: unknown) => schema ? [{ name: 'enabled', title: 'Enabled', type: 'boolean', default: false }] : []
} }));
import DynamicModelParams from './DynamicModelParams.svelte';
import ToolCategoryCard from './ToolCategoryCard.svelte';
import LazyPanel from '../../lazy/LazyPanel.svelte';
import { createLazyComponent } from '../../lazy/lazy-component';

beforeEach(() => { mocks.owner = 0; vi.clearAllMocks(); mocks.get.mockResolvedValue({ params: {} }); mocks.patch.mockResolvedValue({}); });
afterEach(() => { cleanup(); vi.useRealTimers(); });

const category: ToolCategory = { id: 'test', label: 'Test', icon: 'image', description: '', accent: 'blue', tools: [{
 id: 'test-tool', label: 'Test tool', description: '', modelSettingKey: 'test_model', modelEndpoint: '/test/model',
 settingsEndpoint: '/test/settings', defaultModel: 'first', catalogToolType: 'image', formatPrice: () => '',
 getModels: () => [{ id: 'first', displayName: 'First model', description: '' },
  { id: 'second', displayName: 'Second model', description: '' }] as AnyModel[],
 settings: [{ key: 'amount', settingKey: 'test_amount', label: 'Amount', type: 'slider',
  sliderConfig: { min: 1, max: 10, step: 1, format: String } }]
}] };

async function openTool() {
 const onchange = vi.fn();
 const view = render(ToolCategoryCard, { category, modelSelections: { test_model: 'first' }, settingValues: {}, onchange });
 await fireEvent.click(screen.getByRole('button', { name: /Test tool/ }));
 return { ...view, onchange };
}

describe('settings editor ownership', () => {
 for (const boundary of ['account', 'unmount'] as const) {
  it(`cancels model parameter saves after ${boundary}`, async () => {
   const view = render(DynamicModelParams, { endpointId: 'model', persistKey: 'image_model' });
   const input = await screen.findByRole('checkbox', { name: 'Enabled' });
   vi.useFakeTimers();
   await fireEvent.click(input);
   if (boundary === 'account') mocks.owner++; else view.unmount();
   await vi.advanceTimersByTimeAsync(400);
   expect(mocks.patch).not.toHaveBeenCalled();
  });
  it(`cancels tool slider saves after ${boundary}`, async () => {
   const view = await openTool();
   vi.useFakeTimers();
   await fireEvent.input(screen.getByRole('slider'), { target: { value: '7' } });
   if (boundary === 'account') mocks.owner++; else view.unmount();
   await vi.advanceTimersByTimeAsync(400);
   expect(mocks.patch).not.toHaveBeenCalled();
  });
 }

 it('still saves a current model parameter edit', async () => {
  render(DynamicModelParams, { endpointId: 'model', persistKey: 'image_model' });
  const input = await screen.findByRole('checkbox', { name: 'Enabled' });
  vi.useFakeTimers();
  await fireEvent.click(input);
  await vi.advanceTimersByTimeAsync(400);
  expect(mocks.patch).toHaveBeenCalledWith('/settings/integrations/model-params', { key: 'image_model', params: { enabled: true } });
 });

 it('reloads parameters when the setting key changes for the same model', async () => {
  const view = render(DynamicModelParams, { endpointId: 'model', persistKey: 'image_model' });
  await screen.findByRole('checkbox', { name: 'Enabled' });
  mocks.get.mockResolvedValue({ params: { enabled: true } });
  await view.rerender({ endpointId: 'model', persistKey: 'tts_model' });
  await waitFor(() => expect((screen.getByRole('checkbox') as HTMLInputElement).checked).toBe(true));
  expect(mocks.get).toHaveBeenLastCalledWith('/settings/integrations/model-params?key=tts_model');
 });

 it('hides old parameter controls while a different setting context is loading', async () => {
  const view = render(DynamicModelParams, { endpointId: 'model', persistKey: 'image_model' });
  await screen.findByRole('checkbox', { name: 'Enabled' });
  let resolve!: (value: unknown) => void;
  mocks.get.mockImplementationOnce(() => new Promise(r => { resolve = r; }));
  await view.rerender({ endpointId: 'model', persistKey: 'tts_model' });
  expect(screen.queryByRole('checkbox')).toBeNull();
  expect(screen.getByText('Loading model options…')).toBeTruthy();
  expect(mocks.patch).not.toHaveBeenCalled();
  resolve({ params: { enabled: true } });
  expect((await screen.findByRole('checkbox') as HTMLInputElement).checked).toBe(true);
 });

 it('remounts retained panels for a new account while preserving the same-account instance', async () => {
  const handle = createLazyComponent('params', async () => ({ default: DynamicModelParams }));
  render(LazyPanel, { props: { handle, props: { endpointId: 'model', persistKey: 'image_model' } } });
  const first = await screen.findByRole('checkbox');
  for (const notify of mocks.owners) notify(mocks.owner);
  await tick();
  expect(screen.getByRole('checkbox')).toBe(first);
  mocks.owner++;
  for (const notify of mocks.owners) notify(mocks.owner);
  await tick();
  const second = await screen.findByRole('checkbox');
  expect(second).not.toBe(first);
  vi.useFakeTimers();
  await fireEvent.click(second);
  await vi.advanceTimersByTimeAsync(400);
  expect(mocks.patch).toHaveBeenCalledWith('/settings/integrations/model-params', { key: 'image_model', params: { enabled: true } });
 });

 it('recovers when returning to the previous context while another load is pending', async () => {
  const view = render(DynamicModelParams, { endpointId: 'model', persistKey: 'image_model' });
  await screen.findByRole('checkbox');
  let releaseOther!: (value: unknown) => void;
  mocks.get.mockImplementationOnce(() => new Promise(r => { releaseOther = r; }));
  await view.rerender({ endpointId: 'model', persistKey: 'tts_model' });
  expect(screen.queryByRole('checkbox')).toBeNull();
  mocks.get.mockResolvedValue({ params: { enabled: true } });
  await view.rerender({ endpointId: 'model', persistKey: 'image_model' });
  expect((await screen.findByRole('checkbox') as HTMLInputElement).checked).toBe(true);
  releaseOther({ params: { enabled: false } });
  await tick();
  expect((screen.getByRole('checkbox') as HTMLInputElement).checked).toBe(true);
 });

 it('does not send model-reset followups after the account changes during a save', async () => {
  let resolve!: (value: unknown) => void;
  mocks.patch.mockImplementationOnce(() => new Promise(r => { resolve = r; }));
  const { onchange } = await openTool();
  await fireEvent.click(screen.getByRole('button', { name: 'First model' }));
  await fireEvent.click(screen.getByRole('button', { name: /Second model/ }));
  expect(mocks.patch).toHaveBeenCalledTimes(1);
  mocks.owner++;
  resolve({});
  await tick();
  await Promise.resolve();
  expect(mocks.patch).toHaveBeenCalledTimes(1);
  expect(onchange).not.toHaveBeenCalled();
 });
});
