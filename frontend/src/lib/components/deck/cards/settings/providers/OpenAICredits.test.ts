import { afterEach, it, expect, vi } from 'vitest';
import { render, screen, cleanup } from '@testing-library/svelte';
import OpenAICredits from './OpenAICredits.svelte';
import type { ModelProvider } from '$lib/api/model-providers';
import * as billing from '$lib/api/openai-billing';
vi.mock('$lib/api/openai-billing', () => ({ getOpenAIUsage: vi.fn(), setOpenAIUsage: vi.fn(), disconnectOpenAIUsage: vi.fn() }));
const provider = { id: 'p1', name: 'OpenAI', enabled: true, provider_type: 'openai' } as ModelProvider;
afterEach(cleanup);
it('explains the unsupported API balance without requesting another key or opening billing sessions', () => {
 const { container } = render(OpenAICredits, { provider });
 expect(screen.getByText(/does not expose a prepaid credit balance/)).toBeTruthy();
 expect(screen.getByText(/Admin API key reports organization spending, not credit left/)).toBeTruthy();
 expect(container.querySelector('input, form')).toBeNull();
 expect(screen.getByRole('link', { name: /View balance/ }).getAttribute('href')).toBe('https://platform.openai.com/settings/organization/billing/overview');
 expect(billing.getOpenAIUsage).not.toHaveBeenCalled();
 expect(billing.setOpenAIUsage).not.toHaveBeenCalled();
 expect(billing.disconnectOpenAIUsage).not.toHaveBeenCalled();
});
