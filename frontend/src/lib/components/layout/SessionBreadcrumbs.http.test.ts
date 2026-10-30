// Exercise breadcrumb compatibility through the real API client's HTTP decoding.
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import { writable } from 'svelte/store';
import type { Session } from '$lib/api/sessions';

vi.mock('$lib/stores/app-data.svelte', () => ({ appData: { current: { sessions: [] } }, sessions: writable<Session[]>([]) }));
vi.mock('$lib/stores/active-session', () => ({ activeSession: Object.assign(writable({}), { switchSession: vi.fn() }) }));
vi.mock('$lib/stores/auth.svelte', () => ({ auth: writable({ status: { is_admin: false } }) }));
vi.mock('$lib/services/session-navigation', () => ({ openSessionLink: vi.fn(), sessionHref: (id: string) => `/?session=${encodeURIComponent(id)}` }));
import { api } from '$lib/api/client';
import { sessions } from '$lib/stores/app-data.svelte';
import SessionBreadcrumbs from './SessionBreadcrumbs.svelte';

beforeEach(() => {
	vi.stubEnv('VITE_PERF_SLIM_SESSION_READ', '1');
	(sessions as unknown as ReturnType<typeof writable<Session[]>>).set([]);
});
afterEach(() => {
	cleanup();
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
	vi.unstubAllEnvs();
});

it.each([
	{ name: 'canonical FastAPI route absence', status: 404, body: '{"detail":"Not Found"}', type: 'application/json', fallback: true },
	{ name: 'detail with an extra null field', status: 404, body: '{"detail":"Not Found","code":null}', type: 'application/json', fallback: false },
	{ name: 'modern missing session', status: 404, body: '{"detail":"Session not found: deleted"}', type: 'application/json', fallback: false },
	{ name: 'plain text Not Found', status: 404, body: 'Not Found', type: 'text/plain', fallback: false },
	{ name: 'JSON message Not Found', status: 404, body: '{"message":"Not Found"}', type: 'application/json', fallback: false },
	{ name: 'JSON error Not Found', status: 404, body: '{"error":"Not Found"}', type: 'application/json', fallback: false },
	{ name: 'JSON string Not Found', status: 404, body: '"Not Found"', type: 'application/json', fallback: false },
	{ name: 'unknown JSON 404', status: 404, body: '{}', type: 'application/json', fallback: false },
	{ name: 'wrong status with canonical text', status: 400, body: '{"detail":"Not Found"}', type: 'application/json', fallback: false },
	{ name: 'unsupported method', status: 405, body: 'Method Not Allowed', type: 'text/plain', fallback: true },
	{ name: 'unimplemented route', status: 501, body: '{"detail":"Not Implemented"}', type: 'application/json', fallback: true },
])('classifies $name through the real client without losing hidden-parent labels', async ({ name, status, body, type, fallback }) => {
	const slug = name.replace(/\W+/g, '-');
	const childId = `http-child-${slug}`;
	const parentId = `http-parent-${slug}`;
	const reads: URL[] = [];
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
		const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, 'http://localhost');
		reads.push(url);
		if (url.pathname === `/api/v1/sessions/${childId}/live`) return new Response(JSON.stringify({
			session_meta: { id: childId, title: 'HTTP worker', hidden: true, parent_session_id: parentId }
		}), { headers: { 'Content-Type': 'application/json' } });
		if (url.pathname === `/api/v1/sessions/${parentId}/live`) return new Response(body, {
			status, headers: { 'Content-Type': type }
		});
		if (url.pathname === `/api/v1/sessions/${parentId}`) return new Response(JSON.stringify({
			id: parentId, title: 'Legacy parent', hidden: false, parent_session_id: null
		}), { headers: { 'Content-Type': 'application/json' } });
		throw new Error(`Unexpected HTTP request: ${url.pathname}`);
	}));
	// Spy without replacing its implementation: response parsing and shared GETs
	// are production code, not fabricated ApiError objects.
	const shared = vi.spyOn(api, 'getShared');
	render(SessionBreadcrumbs, { sessionId: childId, title: 'HTTP worker' });
	await waitFor(() => expect(shared).toHaveBeenCalledTimes(2));
	await Promise.allSettled(shared.mock.results.filter(result => result.type === 'return').map(result => result.value));
	await tick();
	if (fallback) {
		expect(await screen.findByRole('link', { name: 'Open parent chat: Legacy parent' })).toBeTruthy();
	} else {
		expect(screen.queryByRole('link')).toBeNull();
		expect(screen.getByTitle('Parent chat unavailable')).toBeTruthy();
	}
	expect(screen.getByRole('button', { name: /HTTP worker/ })).toBeTruthy();
	expect(reads.filter(url => url.searchParams.get('message_limit') === '1').map(url => url.pathname + url.search))
		.toEqual(fallback ? [`/api/v1/sessions/${parentId}?message_limit=1`] : []);
});
