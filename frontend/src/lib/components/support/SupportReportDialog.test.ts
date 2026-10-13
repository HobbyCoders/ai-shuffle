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

	it('explains not being signed in and offers to save the report instead', async () => {
		await open({
			'/support/reports': [() => preview()],
			'/support/check': [() => ({ state: 'not_signed_in', message: 'Sign this worker in to a Shuffle account to contact HobbyCoders support.', known_issue: null })]
		});
		expect(screen.getByText('Not signed in')).toBeTruthy();
		expect(screen.getByText(/Sign this worker in to a Shuffle account/)).toBeTruthy();
		expect(screen.getByText(/save the report here and email it/)).toBeTruthy();
		expect(screen.getByText('What will be saved')).toBeTruthy();
		const save = screen.getByRole('button', { name: 'Save report' }) as HTMLButtonElement;
		expect(save.disabled).toBe(false);
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

describe('SupportReportDialog: AI content and local-only workers', () => {
	const AI = { kind: 'ai_content' as const, excerpt: 'A hateful reply', session_id: 's1' };
	const aiPreview = () =>
		preview({
			report_id: 'content-1',
			report_kind: 'ai_content',
			title: 'AI content report',
			body: 'AI Shuffle AI content report\nReport kind: ai_content\n\n## Reported AI output\nA hateful reply',
			redactions: 0,
			excerpt: 'A hateful reply',
			user_note: 'Hate speech.'
		});

	it('builds an AI content report with the reason, without a known-issue check or diagnosis', async () => {
		routePosts({ '/support/reports': [() => aiPreview()] });
		const flow = new SupportReportFlow(AI, { note: 'Hate speech.' });
		render(SupportReportDialog, { flow, onClose: vi.fn() });
		await flow.start();
		await tick();
		expect(api.post).toHaveBeenCalledTimes(1);
		expect(api.post).toHaveBeenCalledWith('/support/reports', { ...AI, user_note: 'Hate speech.' });
		expect(screen.getByText('Report AI content')).toBeTruthy();
		expect(screen.getByText(/reviews reported AI content by hand/)).toBeTruthy();
		expect(screen.getByText('Why are you reporting it?')).toBeTruthy();
		expect(screen.queryByText('AI diagnosis')).toBeNull();
		expect(sendButton().disabled).toBe(false);
	});

	it('disables Send with an inline message while the reason is blank', async () => {
		routePosts({ '/support/reports': [() => aiPreview()] });
		const flow = new SupportReportFlow(AI, { note: 'Hate speech.', noteDelayMs: 10_000 });
		render(SupportReportDialog, { flow, onClose: vi.fn() });
		await flow.start();
		await tick();
		const reason = screen.getByLabelText('Why are you reporting it?') as HTMLTextAreaElement;
		await fireEvent.input(reason, { target: { value: '   ' } });
		expect(sendButton().disabled).toBe(true);
		expect(screen.getByRole('alert').textContent).toContain('Say why you are reporting it.');
		expect(reason.getAttribute('aria-invalid')).toBe('true');
		await fireEvent.blur(reason);
		expect(api.post.mock.calls.some(([p]) => String(p).endsWith('/note'))).toBe(false);
		await fireEvent.input(reason, { target: { value: 'Slur' } });
		expect(sendButton().disabled).toBe(false);
		expect(screen.queryByText('Say why you are reporting it.')).toBeNull();
		flow.dispose();
	});

	it('on a local-only worker, saves the report and offers it as text and an email', async () => {
		routePosts({
			'/support/reports': [() => aiPreview()],
			'/send': [
				() => ({
					state: 'not_signed_in',
					report_id: 'content-1',
					message: 'Saved on this worker.',
					local_record: { file: 'support-reports/content-1.json', saved_at: '' },
					support_email: 'support@hobbycoders.com'
				})
			]
		});
		const flow = new SupportReportFlow(AI, { note: 'Hate speech.' });
		render(SupportReportDialog, { flow, onClose: vi.fn() });
		await flow.start();
		await tick();
		await fireEvent.click(sendButton());
		await tick();
		expect(flow.phase).toBe('saved_locally');
		expect(screen.getByText('Saved on this worker')).toBeTruthy();
		expect(screen.getByText('support-reports/content-1.json')).toBeTruthy();
		const mail = screen.getByTestId('mailto') as HTMLAnchorElement;
		expect(mail.getAttribute('href')).toMatch(/^mailto:support@hobbycoders\.com\?subject=AI%20content%20report&body=/);
		expect(decodeURIComponent(mail.getAttribute('href')!.split('body=')[1])).toContain('A hateful reply');
		expect(screen.getByRole('button', { name: /Copy report/ })).toBeTruthy();
		expect(screen.getByLabelText('Report text').textContent).toContain('Report kind: ai_content');
	});
});
