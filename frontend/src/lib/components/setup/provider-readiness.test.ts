import { describe, expect, it } from 'vitest';
import { choiceOf, readyCount, setupReadiness } from './provider-readiness';

describe('setup readiness for the Claude API key', () => {
	it('counts the native Claude API provider as connected under Claude, with no stored row', () => {
		// A fresh worker with only a saved Anthropic key: the gate names the
		// virtual provider, which the management API never lists as a row.
		const gate = { ready: true, reason: null, any_provider_ready: true, ready_providers: ['gw:claude-api'] };
		const readiness = setupReadiness(gate as never, [], []);
		expect(readiness).toMatchObject({ anyReady: true, ready: ['gw:claude-api'], needsModel: [] });
		expect(choiceOf('gw:claude-api')).toBe('claude');
		expect(readyCount('claude', readiness.ready)).toBe(1);
		expect(readyCount('gateway', readiness.ready)).toBe(0);
	});

	it('still files other gateways under "Your own gateway"', () => {
		expect(choiceOf('gw:my-proxy')).toBe('gateway');
	});
});
