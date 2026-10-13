import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '@fontsource-variable/jetbrains-mono';
import '../src/app.css';

/**
 * Browser fixture for the context meter on a provider model (#2274).
 *
 *   ?bound=unknown  an xAI model with no advertised window, and the session's
 *                   context report has none either (maxTokens null)
 *   ?bound=server   the same model; the server resolves 131,072 for it
 *   ?bound=catalog  the curated model entry carries 256,000; no server bound
 *
 * Every request is answered here; nothing reaches a backend.
 */
const bound = new URLSearchParams(location.search).get('bound') ?? 'unknown';
const SESSION = 'fixture-grok';
const MODEL = 'x-ai/grok-unlisted';
const USED = 84_200;
const serverMax = bound === 'server' ? 131_072 : null;
const registry = {
	models: [{ value: 'sonnet', label: 'Sonnet', family: 'sonnet', context_window: 1_000_000 }],
	default: 'sonnet',
	legacy_map: {},
	providers: [{
		id: 'xai', name: 'xAI', provider_type: 'openai_compatible', default_model: MODEL,
		models: [{ id: MODEL, label: 'Grok (unlisted)', ...(bound === 'catalog' ? { context_window: 256_000 } : {}) }]
	}]
};
const usage = {
	available: true,
	source: 'persisted',
	captured_at: new Date().toISOString(),
	usage: {
		categories: [
			{ name: 'System prompt', tokens: 6_100, color: '#5b8def' },
			{ name: 'Tools', tokens: 11_900, color: '#3bb27a' },
			{ name: 'Messages', tokens: 66_200, color: '#e0a13c' },
			...(serverMax ? [{ name: 'Free space', tokens: serverMax - USED, color: '#444' }] : [])
		],
		totalTokens: USED,
		maxTokens: serverMax,
		rawMaxTokens: serverMax,
		percentage: serverMax ? (USED / serverMax) * 100 : null,
		model: MODEL,
		isAutoCompactEnabled: false
	}
};
window.fetch = async (input) => {
	const path = new URL(input instanceof Request ? input.url : String(input), location.href).pathname;
	let body: unknown = [];
	if (path.endsWith('/models')) body = registry;
	else if (path.endsWith(`/sessions/${SESSION}/context-usage`)) body = usage;
	else if (path.includes('/auth/status')) body = { authenticated: true, has_any_credential: true, onboarding_complete: true };
	else if (path.includes('computer-use')) body = { available: false, enabled: false, active: false, reason: 'no-display' };
	else if (path.includes('commands')) body = { commands: [] };
	else if (path.includes('usage')) body = { available: false, buckets: {}, provider_buckets: {} };
	return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
};
class MockSocket { static OPEN = 1; static CLOSED = 3; readyState = 1; send() {} close() {} addEventListener() {} removeEventListener() {} }
window.WebSocket = MockSocket as unknown as typeof WebSocket;
window.EventSource = MockSocket as unknown as typeof EventSource;
localStorage.clear();

const { theme } = await import('../src/lib/stores/theme.svelte');
theme.setTheme('dark');
const { hydrateModelRegistry, encodeProviderModel } = await import('../src/lib/constants/models');
await hydrateModelRegistry();
const { auth } = await import('../src/lib/stores/auth.svelte');
await auth.checkAuth();
const { activeSession } = await import('../src/lib/stores/active-session');
activeSession.setModelOverride(encodeProviderModel('xai', MODEL));
Object.assign(activeSession.current, {
	sessionId: SESSION, mode: 'code', isStreaming: false, draft: '', liveProvider: 'xai',
	contextUsed: USED, totalTokensIn: 6_100, totalCacheReadTokens: 72_000, totalCacheCreationTokens: 6_100, totalTokensOut: 3_500
});
activeSession.setDraft('');
const { default: Fixture } = await import('./ContextMeterFixture.svelte');
const label = { unknown: 'xAI model · no advertised context length', server: 'xAI model · server-resolved bound 131,072', catalog: 'xAI model · curated entry 256,000' }[bound] ?? bound;
mount(Fixture, { target: document.getElementById('app')!, props: { label } });
