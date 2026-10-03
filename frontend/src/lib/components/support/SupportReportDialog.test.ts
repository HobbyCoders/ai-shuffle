/**
 * The support report dialog's main states, rendered from a real
 * SupportReportFlow with the HTTP client faked.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api }));
const nav = vi.hoisted(() => ({ openSettings: vi.fn() }));
vi.mock('$lib/settings/navigation.svelte', () => nav);

import SupportReportDialog from './SupportReportDialog.svelte';
import { SupportReportFlow } from '$lib/stores/support-report.svelte';
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
		body: 'AI Shuffle support report (basic)\nToken: ***',
		redactions: 1,
		user_note: null,
		expires_at: '2026-10-03T11:05:00+00:00',
		...over
	};
}

const issue = (over = {}) => ({ status: 'new', fixed_in: null, note: null, reported: false, update_available: false, ...over });

function routePosts(routes: Record<string, Array<(body: any) => unknown>>) {
	api.post.mockImplementation((path: string, body: unknown) => {
		const key = Object.keys(routes).find((k) => path.endsWith(k));
		if (!key) return Promise.reject({ status: 500, detail: `unrouted ${path}` });
		const queue = routes[key];
		const responder = queue.length > 1 ? queue.shift()! : queue[0];
		return Promise.resolve().then(() => responder(body));
	});
}

async function open(routes: Record<string, Array<(body: any) => unknown>>, opts = {}) {
	routePosts(routes);
	const flow = new SupportReportFlow(ERROR, { pollMs: 20, ...opts });
	const onClose = vi.fn();
	render(SupportReportDialog, { flow, onClose });
	await flow.start();
	await tick();
	return { flow, onClose };
}

const sendButton = () => screen.getByRole('button', { name: /^Send/ }) as HTMLButtonElement;

beforeEach(() => {
	vi.clearAllMocks();
});

afterEach(() => {
	cleanup();
	vi.useRealTimers();
});

describe('SupportReportDialog', () => {
	it('shows exactly what will be sent and sends nothing until Send', async () => {
		await open({ '/support/reports': [() => preview()], '/support/check': [() => ({ state: 'ok', known_issue: issue() })] });
		expect(screen.getByText('Send report to HobbyCoders support')).toBeTruthy();
		expect(screen.getByLabelText('Report text').textContent).toBe('AI Shuffle support report (basic)\nToken: ***');
		expect(screen.getByText(/1 secret hidden as \*\*\*/)).toBeTruthy();
		expect(sendButton().disabled).toBe(false);
		expect(api.post.mock.calls.some(([p]) => String(p).endsWith('/send'))).toBe(false);
	});

	it('says when an issue is fixed and points to the existing update screen', async () => {
		await open({
			'/support/reports': [() => preview()],
			'/support/check': [() => ({ state: 'ok', known_issue: issue({ status: 'fixed', fixed_in: '0.9.2', reported: true, update_available: true }) })]
		});
		expect(screen.getByText('Fixed in version 0.9.2')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Open updates' }));
		expect(nav.openSettings).toHaveBeenCalledWith({ category: 'system', anchor: 'system.checkUpdates' });
	});

	it('explains not being signed in and blocks Send', async () => {
		await open({
			'/support/reports': [() => preview()],
			'/support/check': [() => ({ state: 'not_signed_in', message: 'Sign this worker in to a Shuffle account to contact HobbyCoders support.', known_issue: null })]
		});
		expect(screen.getByText('Not signed in')).toBeTruthy();
		expect(screen.getByText(/Sign this worker in to a Shuffle account/)).toBeTruthy();
		expect(sendButton().disabled).toBe(true);
		await fireEvent.click(screen.getByRole('button', { name: 'Open Shuffle account' }));
		expect(nav.openSettings).toHaveBeenCalledWith({ category: 'devices', item: 'account' });
	});

	it('requires the AI usage acknowledgement, then shows progress and the diagnosis', async () => {
		vi.useFakeTimers({ shouldAdvanceTime: true });
		const ai = preview({
			report_id: 'ai-1',
			kind: 'ai',
			body: 'AI Shuffle support report (ai)',
			redactions: 0,
			diagnosis: { model: 'OpenAI / gpt-6.1', text: 'The session list was empty.', workaround: '- Reload the page.' }
		});
		api.get.mockImplementation((path: string) =>
			path.startsWith('/support/diagnoses/')
				? Promise.resolve({ run_id: 'run-1', status: 'done', report_id: 'ai-1', report: ai, model: 'OpenAI / gpt-6.1' })
				: Promise.reject({ status: 404 })
		);
		const { flow } = await open(
			{
				'/support/reports': [() => preview()],
				'/support/check': [() => ({ state: 'ok', known_issue: issue() })],
				'/support/diagnoses': [() => ({ run_id: 'run-1', status: 'running', base_report_id: 'basic-1', report_id: null, error: null, model: 'OpenAI / gpt-6.1', started_at: '', finished_at: null })]
			},
			{ defaultModel: () => 'sonnet', pollMs: 5000 }
		);

		await fireEvent.click(screen.getByRole('button', { name: 'Add AI diagnosis' }));
		expect(screen.getByText(/your own model/)).toBeTruthy();
		expect(screen.getByText(/counts against your AI usage or subscription/)).toBeTruthy();
		const start = screen.getByRole('button', { name: 'Start diagnosis' }) as HTMLButtonElement;
		expect(start.disabled).toBe(true);
		await fireEvent.click(screen.getByLabelText('I understand this uses my AI usage'));
		expect(start.disabled).toBe(false);
		await fireEvent.click(start);
		await tick();
		await tick();
		expect(flow.aiStep).toBe('running');
		expect(screen.getByText(/Diagnosing with OpenAI \/ gpt-6.1/)).toBeTruthy();
		expect(sendButton().disabled).toBe(true);

		await vi.advanceTimersByTimeAsync(5100);
		await tick();
		expect(screen.getByText('The session list was empty.')).toBeTruthy();
		expect(screen.getByText('What you can try')).toBeTruthy();
		expect(screen.getByLabelText('Report text').textContent).toBe('AI Shuffle support report (ai)');
		expect(sendButton().textContent).toContain('Send with diagnosis');
		await fireEvent.click(screen.getByRole('radio', { name: 'Basic only' }));
		expect(screen.getByLabelText('Report text').textContent).toContain('(basic)');
		flow.dispose();
	});

	it('confirms a delivered report in plain words', async () => {
		await open({
			'/support/reports': [() => preview()],
			'/support/check': [() => ({ state: 'ok', known_issue: issue() })],
			'/send': [() => ({ state: 'ok', report_id: 'basic-1', hub_report_id: '0e7c1234abcd9999', delivered: { email: 'sent', github: 'created' }, known_issue: issue({ reported: true }) })]
		});
		await fireEvent.click(sendButton());
		await tick();
		await tick();
		expect(screen.getByText('Sent to HobbyCoders support')).toBeTruthy();
		expect(screen.getByText('0e7c1234abcd')).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Done' })).toBeTruthy();
	});

	it('does not claim anyone received a report that was only stored', async () => {
		await open({
			'/support/reports': [() => preview()],
			'/support/check': [() => ({ state: 'ok', known_issue: issue() })],
			'/send': [() => ({ state: 'ok', report_id: 'basic-1', hub_report_id: 'x', delivered: { email: 'not_configured', github: 'deferred' }, known_issue: null })]
		});
		await fireEvent.click(sendButton());
		await tick();
		await tick();
		expect(screen.getByText('Saved by HobbyCoders')).toBeTruthy();
		expect(screen.queryByText('Sent to HobbyCoders support')).toBeNull();
	});

	it('shows a rate limit with when to retry, keeping the draft', async () => {
		await open({
			'/support/reports': [() => preview()],
			'/support/check': [() => ({ state: 'ok', known_issue: issue() })],
			'/send': [() => ({ state: 'rate_limited', report_id: 'basic-1', message: 'Too many reports from this worker or account. Try again later.', retry_after: 900 })]
		});
		await fireEvent.click(sendButton());
		await tick();
		await tick();
		expect(screen.getByText('Too many reports')).toBeTruthy();
		expect(screen.getByText(/Try again in 15 minutes\./)).toBeTruthy();
		expect(screen.getByLabelText('Report text')).toBeTruthy();
	});

	it('shows a failed diagnosis and leaves the basic report sendable', async () => {
		api.get.mockResolvedValue({ run_id: 'run-1', status: 'failed', error: 'The run took longer than 180 seconds.' });
		const { flow } = await open({
			'/support/reports': [() => preview()],
			'/support/check': [() => ({ state: 'ok', known_issue: issue() })],
			'/support/diagnoses': [() => ({ run_id: 'run-1', status: 'running', base_report_id: 'basic-1', report_id: null, error: null, model: 'Claude / sonnet', started_at: '', finished_at: null })]
		}, { pollMs: 1 });
		flow.openAi();
		flow.aiAcknowledged = true;
		await flow.startAi();
		await new Promise((r) => setTimeout(r, 20));
		await tick();
		expect(screen.getByText('Diagnosis failed')).toBeTruthy();
		expect(screen.getByText(/took longer than 180 seconds\. You can still send the basic report\./)).toBeTruthy();
		expect(sendButton().disabled).toBe(false);
	});
});
