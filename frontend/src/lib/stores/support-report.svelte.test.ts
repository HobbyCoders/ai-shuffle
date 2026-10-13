/**
 * Support report service layer: capabilities, plain-language helpers and the
 * report flow (build → check → optional AI diagnosis → send), with the API
 * faked at the HTTP client.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api }));
const nav = vi.hoisted(() => ({ openSettings: vi.fn() }));
vi.mock('$lib/settings/navigation.svelte', () => nav);

import {
	SupportReportFlow,
	canFixWithAi,
	canSendSupportReport,
	hubNotice,
	knownIssueNotice,
	openSupportReport,
	aiContentPrefill,
	clipCodePoints,
	reportAiContent,
	reportKindOf,
	retryText,
	supportMailSubject,
	supportMailto,
	supportCaps,
	supportReportDialog,
	wasDelivered
} from './support-report.svelte';
import type { SupportReportPreview } from '$lib/api/support';

const ERROR = { error: { message: 'IndexError: list index out of range', source: 'ws' as const } };

function preview(over: Partial<SupportReportPreview> = {}): SupportReportPreview {
	return {
		report_id: 'basic-1',
		kind: 'basic',
		status: 'draft',
		fingerprint: 'f'.repeat(32),
		title: 'IndexError: list index out of range',
		diagnosis: null,
		body: 'AI Shuffle support report (basic)\nApp version: 0.9.1',
		redactions: 0,
		user_note: null,
		expires_at: '2026-10-03T11:05:00+00:00',
		...over
	};
}

const OK_NEW = { state: 'ok', fingerprint: 'f'.repeat(32), known_issue: { status: 'new', fixed_in: null, note: null, reported: false, update_available: false } };
const notFound = () => Promise.reject({ status: 404, detail: 'This report draft has expired. Build it again.' });

/** Route POSTs by path suffix; each entry is a queue of responders. */
function routePosts(routes: Record<string, Array<(body: any) => unknown>>) {
	api.post.mockImplementation((path: string, body: unknown) => {
		const key = Object.keys(routes).find((k) => path.endsWith(k));
		if (!key) return Promise.reject({ status: 500, detail: `unrouted ${path}` });
		const queue = routes[key];
		const responder = queue.length > 1 ? queue.shift()! : queue[0];
		return Promise.resolve().then(() => responder(body));
	});
}

beforeEach(() => {
	vi.clearAllMocks();
	supportCaps.reset();
	supportReportDialog.close();
});

afterEach(() => {
	vi.useRealTimers();
});

describe('capabilities', () => {
	it('offers the report and hides Fix with AI until capabilities say otherwise', async () => {
		expect(canSendSupportReport()).toBe(true);
		expect(canFixWithAi()).toBe(false);
		api.get.mockResolvedValueOnce({ basic_report: true, fix_with_ai: true, default_model: 'sonnet' });
		await supportCaps.load();
		expect(api.get).toHaveBeenCalledWith('/support/capabilities');
		expect(canFixWithAi()).toBe(true);
	});

	it('hides both for a login that is not the owner', async () => {
		api.get.mockRejectedValueOnce({ status: 403, detail: 'Owner only' });
		await supportCaps.load();
		expect(supportCaps.status).toBe('forbidden');
		expect(canSendSupportReport()).toBe(false);
		expect(canFixWithAi()).toBe(false);
	});

	it('loads once and retries after a failure', async () => {
		api.get.mockRejectedValueOnce({ status: 502 });
		await supportCaps.load();
		expect(supportCaps.status).toBe('failed');
		expect(canSendSupportReport()).toBe(true);
		api.get.mockResolvedValueOnce({ basic_report: true, fix_with_ai: false });
		await supportCaps.load();
		await supportCaps.load();
		expect(api.get).toHaveBeenCalledTimes(2);
	});

	it('openSupportReport opens a fresh request each time', () => {
		api.get.mockResolvedValue({ basic_report: true, fix_with_ai: false });
		openSupportReport(ERROR);
		const first = supportReportDialog.request?.key;
		openSupportReport(ERROR);
		expect(supportReportDialog.request?.key).toBe((first ?? 0) + 1);
		expect(supportReportDialog.request?.source).toEqual(ERROR);
	});
});

describe('AI content helpers', () => {
	it('builds a mailto link with the report subject and a bounded body', () => {
		const short = supportMailto('ai_content', 'Report kind: ai_content');
		expect(short).toBe('mailto:support@hobbycoders.com?subject=AI%20content%20report&body=Report%20kind%3A%20ai_content');
		const long = decodeURIComponent(supportMailto('problem', 'x'.repeat(5000)).split('body=')[1]);
		expect(long.length).toBeLessThan(1700);
		expect(long).toContain('Paste the full report');
		expect(supportMailSubject('problem')).toBe('AI Shuffle problem report');
	});

	it('never splits an emoji when clipping the mailto body or an excerpt', () => {
		const body = 'x'.repeat(1499) + '😀' + 'x'.repeat(20);
		const link = supportMailto('ai_content', body);
		const sent = decodeURIComponent(link.split('body=')[1]);
		expect(sent.startsWith('x'.repeat(1499) + '😀')).toBe(true);
		expect(clipCodePoints('ab😀cd', 3)).toBe('ab😀');
		expect(clipCodePoints('short', 10)).toBe('short');
		reportAiContent('x'.repeat(7999) + '😀😀', 's1');
		const prefill = aiContentPrefill.take()!;
		expect(Array.from(prefill.excerpt)).toHaveLength(8000);
		expect(prefill.excerpt.endsWith('😀')).toBe(true);
		expect(() => encodeURIComponent(prefill.excerpt)).not.toThrow();
	});

	it('tells the kind of a report source', () => {
		expect(reportKindOf({ kind: 'ai_content', excerpt: 'x' })).toBe('ai_content');
		expect(reportKindOf({ error_id: 'e1' })).toBe('problem');
	});
});

describe('plain-language helpers', () => {
	it('formats retry times', () => {
		expect(retryText(undefined)).toBeNull();
		expect(retryText(30)).toBe('Try again in 30 seconds.');
		expect(retryText(60)).toBe('Try again in 1 minute.');
		expect(retryText(600)).toBe('Try again in 10 minutes.');
		expect(retryText(7200)).toBe('Try again in 2 hours.');
	});

	it('maps every hub state to a notice', () => {
		expect(hubNotice({ state: 'ok' })).toBeNull();
		const signIn = hubNotice({ state: 'not_signed_in', message: 'Sign this worker in to a Shuffle account to contact HobbyCoders support.' });
		expect(signIn).toMatchObject({ title: 'Not signed in', action: 'account' });
		expect(signIn?.text).toContain('Sign this worker in');
		expect(hubNotice({ state: 'hub_unreachable' })).toMatchObject({ title: "Can't reach HobbyCoders", action: 'retry' });
		const limited = hubNotice({ state: 'rate_limited', message: 'Too many reports from this worker or account. Try again later.', retry_after: 1800 });
		expect(limited?.text).toBe('Too many reports from this worker or account. Try again in 30 minutes.');
		expect(hubNotice({ state: 'unavailable' })?.title).toBe('Support is unavailable');
		expect(hubNotice({ state: 'rejected' })).toMatchObject({ tone: 'error', title: 'Report refused', action: undefined });
	});

	it('describes known and fixed issues', () => {
		expect(knownIssueNotice(null)).toBeNull();
		expect(knownIssueNotice({ status: 'new', fixed_in: null, note: null, reported: false, update_available: false })).toBeNull();
		expect(knownIssueNotice({ status: 'new', fixed_in: null, note: null, reported: true, update_available: false })?.title).toBe('Reported before');
		expect(knownIssueNotice({ status: 'known', fixed_in: null, note: null, reported: true, update_available: false })?.title).toBe('Known issue');
		expect(knownIssueNotice({ status: 'fixed', fixed_in: '0.9.2', note: null, reported: true, update_available: true }))
			.toMatchObject({ title: 'Fixed in version 0.9.2', action: 'update' });
		expect(knownIssueNotice({ status: 'fixed', fixed_in: '0.9.2', note: null, reported: true, update_available: false })?.action).toBeUndefined();
	});

	it('only counts a real delivery as delivered', () => {
		expect(wasDelivered({ email: 'sent', github: 'not_configured' })).toBe(true);
		expect(wasDelivered({ email: 'failed', github: 'commented' })).toBe(true);
		expect(wasDelivered({ email: 'not_configured', github: 'deferred' })).toBe(false);
		expect(wasDelivered(undefined)).toBe(false);
	});
});

describe('SupportReportFlow', () => {
	it('builds the draft, then runs the known-issue check', async () => {
		routePosts({ '/support/reports': [() => preview()], '/support/check': [() => OK_NEW] });
		const flow = new SupportReportFlow(ERROR);
		await flow.start();
		expect(api.post).toHaveBeenNthCalledWith(1, '/support/reports', ERROR);
		expect(api.post).toHaveBeenNthCalledWith(2, '/support/check', { report_id: 'basic-1' });
		expect(flow.phase).toBe('ready');
		expect(flow.preview?.body).toContain('AI Shuffle support report');
		expect(flow.knownIssue?.status).toBe('new');
		expect(flow.canSend).toBe(true);
		flow.dispose();
	});

	it('reports a build failure without sending anything', async () => {
		api.post.mockRejectedValueOnce({ status: 404, detail: 'That error is no longer in the error log.' });
		const flow = new SupportReportFlow({ error_id: 'gone' });
		await flow.start();
		expect(flow.phase).toBe('build_failed');
		expect(flow.buildError).toBe('That error is no longer in the error log.');
		expect(api.post).toHaveBeenCalledTimes(1);
	});

	it('lets a signed-out worker save the report instead of blocking Send', async () => {
		routePosts({
			'/support/reports': [() => preview()],
			'/support/check': [() => ({ state: 'not_signed_in', message: 'Sign in.', known_issue: null }), () => OK_NEW]
		});
		const flow = new SupportReportFlow(ERROR);
		await flow.start();
		expect(flow.signedOut).toBe(true);
		expect(flow.canSend).toBe(true);
		await flow.recheck();
		expect(flow.signedOut).toBe(false);
		flow.dispose();
	});

	it('moves to saved_locally when a local-only worker keeps the report', async () => {
		routePosts({
			'/support/reports': [() => preview()],
			'/support/check': [() => ({ state: 'not_signed_in', known_issue: null })],
			'/send': [() => ({ state: 'not_signed_in', report_id: 'basic-1', local_record: { file: 'support-reports/basic-1.json', saved_at: '' } })]
		});
		const flow = new SupportReportFlow(ERROR);
		await flow.start();
		await flow.send();
		expect(flow.phase).toBe('saved_locally');
		expect(flow.savedLocally?.local_record?.file).toBe('support-reports/basic-1.json');
		flow.dispose();
	});

	it('saves the note into the draft so the preview shows it', async () => {
		vi.useFakeTimers();
		routePosts({
			'/support/reports': [() => preview()],
			'/support/check': [() => OK_NEW],
			'/note': [(b) => preview({ user_note: b.user_note, body: `report\nNote: ${b.user_note}` })]
		});
		const flow = new SupportReportFlow(ERROR, { noteDelayMs: 100 });
		await flow.start();
		flow.setNote('I opened the session list.');
		expect(flow.noteDirty).toBe(true);
		await vi.advanceTimersByTimeAsync(150);
		expect(api.post).toHaveBeenCalledWith('/support/reports/basic-1/note', { user_note: 'I opened the session list.' });
		expect(flow.preview?.body).toContain('Note: I opened the session list.');
		expect(flow.noteDirty).toBe(false);
		flow.dispose();
	});

	it('needs the acknowledgement before an AI diagnosis, then polls to the AI preview', async () => {
		vi.useFakeTimers();
		const ai = preview({
			report_id: 'ai-1',
			kind: 'ai',
			body: 'AI Shuffle support report (ai)',
			diagnosis: { model: 'OpenAI / gpt-6.1', text: 'The list was empty.', workaround: '- Reload the page.' }
		});
		routePosts({
			'/support/reports': [() => preview()],
			'/support/check': [() => OK_NEW],
			'/support/diagnoses': [() => ({ run_id: 'run-1', status: 'running', base_report_id: 'basic-1', report_id: null, error: null, model: 'OpenAI / gpt-6.1', started_at: '', finished_at: null })]
		});
		api.get
			.mockResolvedValueOnce({ run_id: 'run-1', status: 'running', model: 'OpenAI / gpt-6.1' })
			.mockResolvedValueOnce({ run_id: 'run-1', status: 'done', report_id: 'ai-1', report: ai, model: 'OpenAI / gpt-6.1' });
		const flow = new SupportReportFlow(ERROR, { pollMs: 50, defaultModel: () => 'prov:p1:gpt-6.1' });
		await flow.start();

		flow.openAi();
		expect(flow.aiStep).toBe('confirm');
		expect(flow.aiModel).toBe('prov:p1:gpt-6.1');
		await flow.startAi();
		expect(api.post).not.toHaveBeenCalledWith('/support/diagnoses', expect.anything());

		flow.aiModel = 'sonnet';
		flow.aiAcknowledged = true;
		await flow.startAi();
		expect(api.post).toHaveBeenCalledWith('/support/diagnoses', { report_id: 'basic-1', model: 'sonnet', acknowledge_ai_usage: true });
		expect(flow.aiStep).toBe('running');
		expect(flow.canSend).toBe(false);

		await vi.advanceTimersByTimeAsync(60);
		expect(flow.aiStep).toBe('running');
		await vi.advanceTimersByTimeAsync(60);
		expect(api.get).toHaveBeenCalledWith('/support/diagnoses/run-1');
		expect(flow.aiStep).toBe('done');
		expect(flow.kind).toBe('ai');
		expect(flow.preview?.report_id).toBe('ai-1');
		expect(flow.canSend).toBe(true);

		await flow.useKind('basic');
		expect(flow.preview?.report_id).toBe('basic-1');
		flow.dispose();
	});

	it('explains a busy or failed diagnosis and keeps the basic report sendable', async () => {
		vi.useFakeTimers();
		routePosts({
			'/support/reports': [() => preview()],
			'/support/check': [() => OK_NEW],
			'/support/diagnoses': [
				() => Promise.reject({ status: 409, detail: 'A diagnosis is already running.' }),
				() => ({ run_id: 'run-2', status: 'running', base_report_id: 'basic-1', report_id: null, error: null, model: 'Claude / sonnet', started_at: '', finished_at: null })
			]
		});
		api.get.mockResolvedValueOnce({ run_id: 'run-2', status: 'failed', error: 'The model is not signed in.' });
		const flow = new SupportReportFlow(ERROR, { pollMs: 10 });
		await flow.start();
		flow.openAi();
		flow.aiAcknowledged = true;
		await flow.startAi();
		expect(flow.aiStep).toBe('confirm');
		expect(flow.aiError).toBe('Another diagnosis is running. Try again in a minute.');
		await flow.startAi();
		await vi.advanceTimersByTimeAsync(20);
		expect(flow.aiStep).toBe('failed');
		expect(flow.aiError).toBe('The model is not signed in.');
		expect(flow.canSend).toBe(true);
		flow.dispose();
	});

	it('sends only on send(), and tells a delivery apart from storage', async () => {
		routePosts({
			'/support/reports': [() => preview()],
			'/support/check': [() => OK_NEW],
			'/send': [() => ({ state: 'ok', report_id: 'basic-1', hub_report_id: 'hub-9', delivered: { email: 'not_configured', github: 'deferred' }, known_issue: null })]
		});
		const flow = new SupportReportFlow(ERROR);
		await flow.start();
		expect(api.post.mock.calls.some(([p]) => String(p).endsWith('/send'))).toBe(false);
		await flow.send();
		expect(api.post).toHaveBeenCalledWith('/support/reports/basic-1/send', { confirm: true });
		expect(flow.phase).toBe('sent');
		expect(wasDelivered(flow.sent?.delivered)).toBe(false);
	});

	it('keeps the draft on a hub refusal, and treats 409 as already sent', async () => {
		routePosts({
			'/support/reports': [() => preview()],
			'/support/check': [() => OK_NEW],
			'/send': [
				() => ({ state: 'rate_limited', report_id: 'basic-1', message: 'Too many reports. Try again later.', retry_after: 120 }),
				() => Promise.reject({ status: 409, detail: 'This report was already sent.' })
			]
		});
		const flow = new SupportReportFlow(ERROR);
		await flow.start();
		await flow.send();
		expect(flow.phase).toBe('ready');
		expect(hubNotice(flow.sendOutcome!)?.text).toContain('Try again in 2 minutes.');
		await flow.send();
		expect(flow.phase).toBe('sent');
		expect(flow.alreadySent).toBe(true);
	});

	it('rebuilds an expired draft and never sends the rebuilt one unseen', async () => {
		routePosts({
			'/support/reports': [() => preview(), () => preview({ report_id: 'basic-2' })],
			'/support/check': [() => OK_NEW],
			'/send': [() => notFound(), () => ({ state: 'ok', report_id: 'basic-2', delivered: { email: 'sent', github: 'created' } })]
		});
		const flow = new SupportReportFlow(ERROR);
		await flow.start();
		await flow.send();
		expect(flow.phase).toBe('ready');
		expect(flow.preview?.report_id).toBe('basic-2');
		expect(flow.notice).toMatch(/expired/);
		expect(api.post.mock.calls.filter(([p]) => String(p).endsWith('/send'))).toHaveLength(1);
		await flow.send();
		expect(api.post).toHaveBeenLastCalledWith('/support/reports/basic-2/send', { confirm: true });
		expect(flow.phase).toBe('sent');
	});

	it('rebuilds when the check finds the draft gone', async () => {
		routePosts({
			'/support/reports': [() => preview(), () => preview({ report_id: 'basic-2' })],
			'/support/check': [() => notFound(), () => OK_NEW]
		});
		const flow = new SupportReportFlow(ERROR);
		await flow.start();
		expect(flow.preview?.report_id).toBe('basic-2');
		expect(flow.knownIssue?.status).toBe('new');
		flow.dispose();
	});
});
