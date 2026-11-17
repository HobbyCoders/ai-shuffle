import { expect, it, vi } from 'vitest';
import { activeSession } from './active-session';
import { composerPreferences } from './composer-preferences';
import { api } from '$lib/api/client';

it('opens a clean New Chat on the measured effort, not an invented Auto pick', async () => {
	const { hydrateModelRegistry } = await import('$lib/constants/models');
	const id = 'prov:codex:gpt-6.1-sol';
	const catalog = vi.spyOn(api, 'get').mockResolvedValue({ models: [], default: '', legacy_map: {}, providers: [{
		id: 'codex', name: 'Subscription', provider_type: 'chatgpt', models: [{ id: 'gpt-6.1-sol',
			label: 'Sol', effort_levels: ['low','high'], default_effort: 'low', initial_effort: 'high' }]
	}] });
	try {
		await hydrateModelRegistry();
		composerPreferences.set('model', id);
		activeSession.newChat();
		expect(activeSession.current.effortLevelOverride).toBe('high');
		composerPreferences.rememberEffort(id, null);
		activeSession.newChat();
		expect(activeSession.current.effortLevelOverride).toBeNull();
	} finally { catalog.mockRestore(); composerPreferences.set('model', null); }
});
