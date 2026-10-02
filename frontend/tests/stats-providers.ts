import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '../src/app.css';
import StatsCard from '../src/lib/components/deck/cards/StatsCard.svelte';

// No request leaves this fixture: the analytics endpoints answer with the
// shapes app/api/analytics.py returns. `?providers=fail` makes the provider
// endpoint fail; `?providers=empty` returns no rows. Requests are recorded on
// window.__statsRequests for the verify script.
const mode = new URLSearchParams(location.search).get('providers');
const requests: string[] = [];
(window as unknown as { __statsRequests: string[] }).__statsRequests = requests;

const providerItems = [
	{
		key: 'claude', name: 'Claude', provider_type: 'claude', billing: 'subscription',
		total_tokens_in: 812000, total_tokens_out: 64000, cache_read_tokens: 9400000, cache_creation_tokens: 120000,
		total_cost_usd: 41.27, cost_status: 'estimated', query_count: 212, estimated_query_count: 3,
		unpriced_query_count: 0, subscription_query_count: 212, api_query_count: 0, inferred_query_count: 140
	},
	{
		key: 'p-oai', name: 'OpenAI (team key)', provider_type: 'openai', billing: 'api',
		total_tokens_in: 120000, total_tokens_out: 9000, cache_read_tokens: 0, cache_creation_tokens: 0,
		total_cost_usd: 3.9, cost_status: 'partial', query_count: 31, estimated_query_count: 0,
		unpriced_query_count: 2, subscription_query_count: 0, api_query_count: 31, inferred_query_count: 0
	},
	{
		key: 'p-codex', name: 'ChatGPT subscription with a long account label', provider_type: 'chatgpt', billing: 'subscription',
		total_tokens_in: 54000, total_tokens_out: 4100, cache_read_tokens: 0, cache_creation_tokens: 0,
		total_cost_usd: 0.84, cost_status: 'estimated', query_count: 12, estimated_query_count: 0,
		unpriced_query_count: 0, subscription_query_count: 12, api_query_count: 0, inferred_query_count: 0
	},
	{
		key: 'p-local', name: 'Local vLLM', provider_type: 'custom', billing: 'api',
		total_tokens_in: 33000, total_tokens_out: 2500, cache_read_tokens: 0, cache_creation_tokens: 0,
		total_cost_usd: 0, cost_status: 'unknown', query_count: 9, estimated_query_count: 0,
		unpriced_query_count: 9, subscription_query_count: 0, api_query_count: 9, inferred_query_count: 0
	},
	{
		key: 'unattributed', name: 'Unattributed', provider_type: null, billing: 'unknown',
		total_tokens_in: 1200, total_tokens_out: 300, cache_read_tokens: 0, cache_creation_tokens: 0,
		total_cost_usd: 0.002, cost_status: 'estimated', query_count: 4, estimated_query_count: 0,
		unpriced_query_count: 0, subscription_query_count: 0, api_query_count: 0, inferred_query_count: 0
	}
];

function body(path: string): [number, unknown] {
	if (path.endsWith('/analytics/summary')) {
		return [200, {
			start_date: '2026-09-01', end_date: '2026-10-01',
			usage_stats: { total_tokens_in: 1020200, total_tokens_out: 79900, total_cost_usd: 46.012, session_count: 38, query_count: 268, estimated_query_count: 3 },
			top_profiles: [], recent_trend: []
		}];
	}
	if (path.endsWith('/analytics/costs')) {
		return [200, { group_by: 'profile', start_date: '', end_date: '', total_cost_usd: 46.0, items: [
			{ key: 'default', name: 'Default', total_cost_usd: 40.1, total_tokens_in: 900000, total_tokens_out: 70000, query_count: 230 },
			{ key: 'reviewer', name: 'Reviewer', total_cost_usd: 5.9, total_tokens_in: 120000, total_tokens_out: 9900, query_count: 38 }
		] }];
	}
	if (path.endsWith('/analytics/trends')) {
		const points = Array.from({ length: 30 }, (_, i) => ({
			date: `2026-09-${String(i + 1).padStart(2, '0')}`, tokens_in: 1000, tokens_out: 100,
			cost_usd: 0.4 + ((i * 7) % 11) / 5, query_count: 8, estimated_query_count: i === 4 ? 1 : 0
		}));
		return [200, { interval: 'day', start_date: '', end_date: '', data_points: points }];
	}
	if (path.endsWith('/analytics/top-sessions')) {
		return [200, [
			{ session_id: 's1', title: 'Refactor the accounting path', profile_id: 'default', profile_name: 'Default', total_cost_usd: 9.2, total_tokens_in: 1, total_tokens_out: 1, created_at: '2026-09-20T10:00:00Z' }
		]];
	}
	if (path.endsWith('/analytics/providers')) {
		if (mode === 'fail') return [500, { detail: 'boom' }];
		return [200, { start_date: '', end_date: '', items: mode === 'empty' ? [] : providerItems }];
	}
	if (path.endsWith('/analytics/export')) {
		return [200, 'row_type,date\n'];
	}
	return [404, { detail: 'not in fixture' }];
}

window.fetch = async (input) => {
	const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, location.href);
	requests.push(url.pathname + url.search);
	const [status, payload] = body(url.pathname);
	const text = typeof payload === 'string' ? payload : JSON.stringify(payload);
	const type = typeof payload === 'string' ? 'text/csv' : 'application/json';
	return new Response(text, { status, headers: { 'Content-Type': type } });
};

mount(StatsCard, { target: document.getElementById('app')!, props: { onSessionClick: () => {} } });
