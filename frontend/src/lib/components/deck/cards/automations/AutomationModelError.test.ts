import { afterEach, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/svelte';
import AutomationModelError from './AutomationModelError.svelte';

afterEach(cleanup);
it('keeps a backend model failure reason and recovery while hiding serialized IDs', () => {
 const providerId = 'removed-provider-identity';
 const modelId = 'prov:removed-provider-identity:eyJtb2RlbCI6InNvbm5ldCJ9';
 const error = `This automation's model is no longer available: Model '${modelId}' is not available on provider '${providerId}'. Pick another model in the automation's settings.`;
 const { container } = render(AutomationModelError, { error, providerId, modelId });
 expect(container.textContent).toContain('is no longer available');
 expect(container.textContent).toContain('Pick another model');
 expect(container.textContent).not.toContain('prov:');
 expect(container.textContent).not.toContain(providerId);
 expect(screen.getByTitle(error)).toBeTruthy();
});
it('leaves a plain-language backend reason unchanged', () => {
 const error = 'The provider is offline. Reconnect it or choose another model.';
 const { container } = render(AutomationModelError, { error });
 expect(container.textContent).toBe(error);
});
