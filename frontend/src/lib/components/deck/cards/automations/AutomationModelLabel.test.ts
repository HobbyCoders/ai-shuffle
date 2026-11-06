import { cleanup, render, screen } from '@testing-library/svelte';
import { afterEach, expect, it, vi } from 'vitest';
import AutomationModelLabel from './AutomationModelLabel.svelte';
import { hydrateModelRegistry } from '$lib/constants/models';
vi.mock('$lib/api/client', () => ({ api: { get: vi.fn(async () => ({ models: [], default: null, providers: [{ id: 'provider-a', name: 'Provider', provider_type: 'openai', models: [{ id: 'model-a', label: 'Clear model name' }] }] })) } }));
afterEach(cleanup);
it('resolves an already-composite agent model without printing provider IDs', async () => {
 await hydrateModelRegistry();
 render(AutomationModelLabel, { automation: { model: null, effective_model: 'prov:provider-a:model-a', effective_provider_id: 'provider-a', model_source: 'agent', model_error: null } });
 expect(screen.getByTestId('automation-model-badge').textContent).toBe('Clear model name · Saved on agent');
 expect(screen.getByTestId('automation-model-badge').textContent).not.toContain('prov:');
});
it('keeps unresolved values only in the tooltip', () => {
 render(AutomationModelLabel, { automation: { model: 'prov:missing:raw-id', model_error: null } });
 const badge = screen.getByTestId('automation-model-badge');
 expect(badge.textContent).toBe('Saved model');
 expect(badge.title).toBe('prov:missing:raw-id');
});
