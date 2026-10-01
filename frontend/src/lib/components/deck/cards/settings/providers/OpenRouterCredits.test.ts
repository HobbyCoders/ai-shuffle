import { beforeEach, afterEach, it, expect, vi } from 'vitest';
import { render, screen, cleanup } from '@testing-library/svelte';
import OpenRouterCredits from './OpenRouterCredits.svelte';
import type { ModelProvider } from '$lib/api/model-providers';
import * as billing from '$lib/api/openrouter-billing';
vi.mock('$lib/api/openrouter-billing', () => ({ getOpenRouterUsage: vi.fn(), setOpenRouterUsage: vi.fn(), disconnectOpenRouterUsage: vi.fn() }));
const provider = { id: 'p1', name: 'OpenRouter', enabled: true, provider_type: 'openrouter' } as ModelProvider;
beforeEach(() => vi.clearAllMocks());
afterEach(cleanup);
it.each([false, true])('uses the saved key without setup and preserves legacy credentials (legacy=%s)', async configured => {
 vi.mocked(billing.getOpenRouterUsage).mockResolvedValue({ configured, credits: { total_credits: 25, total_usage: 4.25, balance_remaining: 20.75 }, fetched_at: 1000, last_error_kind: null, source: 'OpenRouter credits API', scope: 'Account' });
 const { container } = render(OpenRouterCredits, { provider });
 await screen.findByText(/20.75 left/);
 expect(screen.getByText(/No separate key is needed/)).toBeTruthy();
 expect(container.querySelector('input, form')).toBeNull();
 expect(billing.getOpenRouterUsage).toHaveBeenCalledTimes(1);
 expect(billing.setOpenRouterUsage).not.toHaveBeenCalled();
 expect(billing.disconnectOpenRouterUsage).not.toHaveBeenCalled();
});
