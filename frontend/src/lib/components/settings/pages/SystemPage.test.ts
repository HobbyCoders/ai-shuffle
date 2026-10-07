import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const transport = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn(), patch: vi.fn() }));
vi.mock('$lib/api/client', () => ({
	api: transport,
	getErrorMessage: (e: { detail?: string; message?: string }, fallback: string) => e?.detail ?? e?.message ?? fallback,
	isApiError: (e: unknown) => typeof e === 'object' && e !== null && 'status' in e
}));
vi.mock('$lib/stores/app-data.svelte', () => ({ appData: { loadProjects: vi.fn() } }));

import SystemPage from './SystemPage.svelte';
import { resourceSync } from '$lib/services/resource-sync';
import { assertCategoryCoverage } from '$lib/settings/testing/coverage';
import { supportCaps, supportReportDialog } from '$lib/stores/support-report.svelte';

const lease = {
	mode: 'licensed',
	state: 'active',
	allowed: true,
	reason: null,
	banner: null,
	plan: 'monthly',
	seats: 3,
	seats_used: 2,
	account_id: 'acct',
	license_id: 'lic',
	key_hint: '7QK2',
	install_id: 'inst',
	issued_at: '2026-09-20T00:00:00Z',
	expires_at: '2026-10-04T00:00:00Z',
	grace: false,
	grace_until: null,
	last_refresh_at: '2026-09-20T00:00:00Z',
	next_refresh_at: null,
	last_error: null,
	server_status: null,
	server_configured: true
};

let license: Record<string, unknown>;
let policy: { allow_updates: boolean; update_all_workers: boolean };
let workspace: string;
let workers: Record<string, unknown>[];
let memoryHealth: unknown;

beforeEach(() => {
	vi.resetAllMocks();
	license = { ...lease };
	policy = { allow_updates: true, update_all_workers: false };
	workspace = '/data/workspace';
	workers = [{ device_id: 'self', device_name: 'Current worker', online: true, supported: true, phase: 'idle' }];
	memoryHealth = { state: 'healthy', checked_at: 1, signals: [], repairs: [] };
	transport.get.mockImplementation(async (path: string) => {
		if (path === '/license') return { ...license };
		if (path === '/memory/health') {
			if (memoryHealth instanceof Error) throw memoryHealth;
			return memoryHealth;
		}
		if (path === '/settings/application-updates') return { ...policy };
		if (path === '/settings/application-updates/workers') return { ...policy, workers };
		if (path === '/workspace/config') return { workspace_path: workspace, is_configured: true, is_local_mode: false, default_path: '', exists: true };
		return {};
	});
	transport.put.mockImplementation(async (path: string, body: Record<string, boolean>) => {
		if (path === '/settings/application-updates') {
			policy = { ...policy, ...body };
			return { ...policy };
		}
		return {};
	});
});
afterEach(() => {
	cleanup();
	resourceSync.clear();
	supportCaps.reset();
	supportReportDialog.close();
	vi.useRealTimers();
});

const sw = (name: string) => screen.getByRole('switch', { name }) as HTMLButtonElement;

const mainRevision = '02e97673a';
const publicationRefusal = `2 behind; check for updates; publish edge first if none is offered. Edge refused ${mainRevision}: dispatch CI and Build worker source on main.`;
const refusalEvidence = { revision: mainRevision, reason: 'dispatch CI and Build worker source on main.' };
const unpublishedExplanation = /The newest main revision is not published as an edge worker update yet/;

async function openDeployReport(worker: Record<string, unknown>, originMain: string | null = mainRevision) {
	const get = transport.get.getMockImplementation()!;
	transport.get.mockImplementation(async (path: string) => path === '/settings/application-updates/deploy-status'
		? { origin_main: originMain, workers: [{ label: 'Unraid', is_self: true, short: '83e64013c', ...worker }] }
		: get(path));
	render(SystemPage);
	const row = document.querySelector('[data-setting-anchor="system.deployStatus"]')!;
	await fireEvent.click(within(row as HTMLElement).getByRole('button', { name: /^Running revisions/ }));
	await waitFor(() => expect(screen.getByText('Unraid (this worker)')).toBeTruthy());
}

it('shows unpublished main only for a current-main edge publication refusal and renders the full reason', async () => {
	await openDeployReport({ online: true, channel: 'edge', behind: 2, action: publicationRefusal, edge_publication_refusal: refusalEvidence });
	expect(screen.getByText(unpublishedExplanation)).toBeTruthy();
	expect(document.querySelector('.st-update-results .st-status')?.textContent).toContain(publicationRefusal);
	expect(document.querySelector('.st-update-results .st-status')?.textContent).toContain('Build worker source on main.');
});

it('uses structured evidence independently of readable action wording', async () => {
	await openDeployReport({ online: true, channel: 'edge', behind: 2, action: 'Readable next step without the old marker.', edge_publication_refusal: refusalEvidence });
	expect(screen.getByText(unpublishedExplanation)).toBeTruthy();
	expect(document.querySelector('.st-update-results .st-status')?.textContent).toContain('Readable next step without the old marker.');
});

it.each([
	['current', { online: true, channel: 'edge', behind: 0, action: 'up to date', edge_publication_refusal: refusalEvidence }],
	['behind without publication evidence', { online: true, channel: 'edge', behind: 2, action: '2 behind; check for updates; publish edge first if none is offered' }],
	['stable', { online: true, channel: 'stable', behind: 2, action: publicationRefusal, edge_publication_refusal: refusalEvidence }],
	['older backend with prose only', { online: true, channel: 'edge', behind: 2, action: publicationRefusal }],
	['offline', { online: false, channel: 'edge', behind: 2, action: publicationRefusal, edge_publication_refusal: { revision: mainRevision, reason: 'refused' } }],
	['unknown comparison', { online: true, channel: 'edge', behind: null, action: publicationRefusal, edge_publication_refusal: refusalEvidence }],
	['no refusal', { online: true, channel: 'edge', behind: 2, action: publicationRefusal, edge_publication_refusal: null }],
	['refusal for an older main revision', { online: true, channel: 'edge', behind: 2, action: publicationRefusal, edge_publication_refusal: { ...refusalEvidence, revision: 'old123456' } }]
])('does not infer unpublished main from a %s worker', async (_name, worker) => {
	await openDeployReport(worker);
	expect(screen.queryByText(unpublishedExplanation)).toBeNull();
	expect(screen.getByText(/Running revisions compares workers with main/)).toBeTruthy();
});

it('does not infer unpublished main when the main revision is unknown', async () => {
	await openDeployReport({ online: true, channel: 'edge', behind: 2, action: publicationRefusal, edge_publication_refusal: refusalEvidence }, null);
	expect(screen.queryByText(unpublishedExplanation)).toBeNull();
});

describe('coverage', () => {
	it('renders a row for every System entry with an active license', async () => {
		await assertCategoryCoverage('system', () => render(SystemPage).container, {
			settle: () => waitFor(() => expect(screen.getByTestId('license-state').textContent).toContain('Active'))
		});
	});
});

describe('workspace', () => {
	it('saves the folder when Check path finds it valid', async () => {
		transport.post.mockImplementation(async (path: string, body: { workspace_path: string }) =>
			path === '/workspace/validate'
				? { valid: true, error: null, path: body.workspace_path, exists: true, writable: true }
				: { workspace_path: body.workspace_path }
		);
		render(SystemPage);
		const input = screen.getByLabelText('Workspace folder') as HTMLInputElement;
		await waitFor(() => expect(input.value).toBe('/data/workspace'));
		await fireEvent.input(input, { target: { value: '/data/other' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Check path' }));
		expect(await screen.findByText('Valid, saved')).toBeTruthy();
		expect(transport.post).toHaveBeenCalledWith('/workspace/config', { workspace_path: '/data/other' });
	});

	it('reports an invalid folder without saving', async () => {
		transport.post.mockResolvedValue({ valid: false, error: 'Not writable', path: '/x', exists: true, writable: false });
		render(SystemPage);
		const input = screen.getByLabelText('Workspace folder') as HTMLInputElement;
		await waitFor(() => expect(input.value).toBe('/data/workspace'));
		await fireEvent.input(input, { target: { value: '/x' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Check path' }));
		expect(await screen.findByText('Not writable')).toBeTruthy();
		expect(transport.post).not.toHaveBeenCalledWith('/workspace/config', expect.anything());
	});

	it('keeps an edited path when the folder changes elsewhere and offers the newer one', async () => {
		render(SystemPage);
		const input = screen.getByLabelText('Workspace folder') as HTMLInputElement;
		await waitFor(() => expect(input.value).toBe('/data/workspace'));
		await fireEvent.input(input, { target: { value: '/data/mine' } });
		workspace = '/data/theirs';
		resourceSync.invalidate(['settings']);
		expect(await screen.findByText(/changed elsewhere, your path kept/)).toBeTruthy();
		expect(input.value).toBe('/data/mine');
		await fireEvent.click(screen.getByRole('button', { name: 'Load latest' }));
		expect(input.value).toBe('/data/theirs');
	});
});

describe('updates', () => {
	it('shows the enabled default and saves turning it off without checking', async () => {
		render(SystemPage);
		await waitFor(() => expect(sw('Allow updates').disabled).toBe(false));
		expect(sw('Allow updates').getAttribute('aria-checked')).toBe('true');
		const changed = vi.fn();
		window.addEventListener('application-update-policy', changed);
		await fireEvent.click(sw('Allow updates'));
		await waitFor(() => expect(transport.put).toHaveBeenCalledWith('/settings/application-updates', { allow_updates: false }));
		expect(await screen.findByText('Paused, this version keeps running')).toBeTruthy();
		expect(changed).toHaveBeenCalledOnce();
		window.removeEventListener('application-update-policy', changed);
		expect(transport.post).not.toHaveBeenCalled();
		expect((screen.getByRole('button', { name: 'Check now' }) as HTMLButtonElement).disabled).toBe(true);
	});

	it('keeps the saved policy and reports a failed save', async () => {
		transport.put.mockRejectedValue(new Error('Not allowed'));
		render(SystemPage);
		await waitFor(() => expect(sw('Allow updates').disabled).toBe(false));
		await fireEvent.click(sw('Allow updates'));
		expect((await screen.findByRole('alert')).textContent).toContain('Not allowed');
		await waitFor(() => expect(sw('Allow updates').getAttribute('aria-checked')).toBe('true'));
	});

	it('keeps the saved scope but disables it while updates are off', async () => {
		policy = { allow_updates: false, update_all_workers: true };
		render(SystemPage);
		await screen.findByText('Paused, this version keeps running');
		expect(sw('Update every worker').disabled).toBe(true);
		expect(sw('Update every worker').getAttribute('aria-checked')).toBe('true');
	});

	it('checks for updates and lists each worker until the check settles', async () => {
		policy = { allow_updates: true, update_all_workers: true };
		transport.post.mockResolvedValue({
			scope: 'all',
			results: [
				{ device_id: 'peer', device_name: 'build-box', accepted: false, reason: 'Offline; not queued' },
				{ device_id: 'self', device_name: 'Current worker', accepted: true, reason: 'Request queued' }
			]
		});
		let reads = 0;
		transport.get.mockImplementation(async (path: string) => {
			if (path === '/settings/application-updates') return { ...policy };
			if (path === '/settings/application-updates/workers') {
				reads++;
				return {
					...policy,
					workers: [
						{ device_id: 'self', device_name: 'Current worker', online: true, supported: true, phase: reads === 1 ? 'checking' : 'available', version: '0.8.23' },
						{ device_id: 'peer', device_name: 'build-box', online: false, supported: false, phase: 'unsupported' }
					]
				};
			}
			return { ...license };
		});
		vi.useFakeTimers({ shouldAdvanceTime: true });
		render(SystemPage);
		await waitFor(() => expect((screen.getByRole('button', { name: 'Check now' }) as HTMLButtonElement).disabled).toBe(false));
		expect(screen.getByText('Checks every online worker')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Check now' }));
		expect(transport.post).toHaveBeenCalledWith('/settings/application-updates', { action: 'check' });
		expect(await screen.findByText('Checking')).toBeTruthy();
		expect(screen.getByText('Offline; not queued')).toBeTruthy();
		await vi.advanceTimersByTimeAsync(2_100);
		expect(await screen.findByText('Version 0.8.23 available')).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Check now' })).toBeTruthy();
	});
});

describe('license', () => {
	it('shows Beta and nothing to enter in beta builds', async () => {
		license = { ...lease, mode: 'beta', state: 'beta', plan: 'beta' };
		render(SystemPage);
		await waitFor(() => expect(screen.getByTestId('license-state').textContent).toContain('Beta'));
		expect(screen.getByText('No key needed in beta')).toBeTruthy();
		expect(screen.queryByText('License key')).toBeNull();
		expect(screen.queryByText('Seats')).toBeNull();
	});

	it('shows status, plan, seats and the key for a licensed worker', async () => {
		render(SystemPage);
		await waitFor(() => expect(screen.getByTestId('license-state').textContent).toContain('Active'));
		expect(screen.getByTestId('license-seats').textContent).toContain('2 of 3');
		expect(screen.getByText(/^Monthly, until/)).toBeTruthy();
		expect(screen.getByRole('button', { name: /ASH-…7QK2/ })).toBeTruthy();
		expect(screen.getByText('Deactivate on this worker')).toBeTruthy();
	});

	it('activates with a key and tells the banner to re-read', async () => {
		license = { ...lease, state: 'unlicensed', allowed: false, reason: 'No license is active on this worker.', plan: null, key_hint: null };
		transport.post.mockResolvedValue({ activated: true, ...lease });
		const changed = vi.fn();
		window.addEventListener('license-changed', changed);
		render(SystemPage);
		await waitFor(() => expect(screen.getByTestId('license-state').textContent).toContain('Not activated'));
		expect(screen.getByText('No license is active on this worker.')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: /^License key/ }));
		await fireEvent.input(screen.getByRole('textbox', { name: 'License key' }), { target: { value: ' ASH-AAAAA-BBBBB-CCCCC-DDDDD ' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Activate' }));
		await waitFor(() => expect(transport.post).toHaveBeenCalledWith('/license/activate', { key: 'ASH-AAAAA-BBBBB-CCCCC-DDDDD' }));
		expect((await screen.findByRole('status')).textContent).toContain('licensed');
		expect(screen.getByTestId('license-state').textContent).toContain('Active');
		expect(changed).toHaveBeenCalled();
		window.removeEventListener('license-changed', changed);
	});

	it('when every seat is taken, lists the workers and frees one with the key', async () => {
		license = { ...lease, state: 'unlicensed', allowed: false, plan: null };
		transport.post.mockImplementation(async (path: string) =>
			path === '/license/activate'
				? {
						activated: false,
						code: 'seats_full',
						detail: 'All 3 workers on this license are in use.',
						seats: 3,
						installs: [{ install_id: 'old-box-1', name: 'Old box', app_version: '0.8.0', last_seen: '2026-08-01T00:00:00Z' }]
					}
				: { ok: true, seats_used: 2 }
		);
		render(SystemPage);
		await waitFor(() => expect(screen.getByTestId('license-state').textContent).toContain('Not activated'));
		await fireEvent.click(screen.getByRole('button', { name: /^License key/ }));
		await fireEvent.input(screen.getByRole('textbox', { name: 'License key' }), { target: { value: 'ASH-AAAAA-BBBBB-CCCCC-DDDDD' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Activate' }));
		expect((await screen.findByRole('alert')).textContent).toContain('All 3 workers');
		const seats = screen.getByRole('group', { name: 'Workers using this license' });
		expect(within(seats).getByText('Old box')).toBeTruthy();
		await fireEvent.click(within(seats).getByRole('button', { name: 'Deactivate' }));
		await fireEvent.click(within(seats).getByRole('button', { name: 'Free seat' }));
		await waitFor(() =>
			expect(transport.post).toHaveBeenCalledWith('/license/release', { key: 'ASH-AAAAA-BBBBB-CCCCC-DDDDD', install_id: 'old-box-1' })
		);
		expect((await screen.findByRole('status')).textContent).toContain('Freed the seat of Old box');
	});

	it('shows the grace banner and deactivates only after confirming', async () => {
		license = { ...lease, state: 'grace', grace: true, grace_until: '2026-10-01T00:00:00Z', banner: { severity: 'warning', message: 'Your last payment failed.' } };
		transport.post.mockResolvedValue({ ...lease, state: 'unlicensed', allowed: false, seat_released: true });
		render(SystemPage);
		await waitFor(() => expect(screen.getByTestId('license-state').textContent).toContain('Payment grace period'));
		expect(screen.getByText('Your last payment failed.')).toBeTruthy();
		expect(screen.getByText(/^Grace until/)).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Deactivate' }));
		expect(screen.getByText('Pause new runs here?')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
		expect(transport.post).not.toHaveBeenCalled();
		await fireEvent.click(screen.getByRole('button', { name: 'Deactivate' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Deactivate' }));
		await waitFor(() => expect(transport.post).toHaveBeenCalledWith('/license/deactivate'));
		expect((await screen.findByRole('status')).textContent).toContain('seat freed');
	});

	it('checks the license on request', async () => {
		transport.post.mockResolvedValue({ ...lease, last_refresh_at: new Date().toISOString() });
		render(SystemPage);
		await waitFor(() => expect(screen.getByTestId('license-state').textContent).toContain('Active'));
		await fireEvent.click(screen.getByRole('button', { name: /^License key/ }));
		await fireEvent.click(screen.getByRole('button', { name: 'Check the license now' }));
		await waitFor(() => expect(transport.post).toHaveBeenCalledWith('/license/refresh'));
		expect((await screen.findByRole('status')).textContent).toContain('Checked with the license server');
	});

	it('reports activation errors', async () => {
		license = { ...lease, state: 'unlicensed', allowed: false, plan: null };
		transport.post.mockRejectedValue({ detail: 'License key not recognised', status: 404 });
		render(SystemPage);
		await waitFor(() => expect(screen.getByTestId('license-state').textContent).toContain('Not activated'));
		await fireEvent.click(screen.getByRole('button', { name: /^License key/ }));
		await fireEvent.input(screen.getByRole('textbox', { name: 'License key' }), { target: { value: 'ASH-X' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Activate' }));
		expect((await screen.findByRole('alert')).textContent).toBe('License key not recognised');
	});
});

describe('memory health', () => {
	it('shows a healthy store with one line when opened', async () => {
		render(SystemPage);
		const row = await screen.findByTestId('memory-health');
		await waitFor(() => expect(row.textContent).toContain('Healthy'));
		await fireEvent.click(within(row).getByRole('button'));
		expect(await screen.findByText('Nothing needs attention')).toBeTruthy();
	});

	it('names what needs attention and what was repaired', async () => {
		const at = Date.now() / 1000;
		memoryHealth = {
			state: 'attention',
			checked_at: at,
			signals: [
				{ id: 'learner', label: 'Learning', state: 'attention', reason: 'Learning has not run despite recent sessions.' },
				{ id: 'saves', label: 'Saving', state: 'healthy', reason: 'Saves are writing normally.' }
			],
			repairs: [{ kind: 'duplicates', count: 1, at, summary: 'Archived 1 exact duplicate.' }]
		};
		render(SystemPage);
		const row = await screen.findByTestId('memory-health');
		await waitFor(() => expect(row.textContent).toContain('Needs attention'));
		await fireEvent.click(within(row).getByRole('button'));
		expect(await screen.findByText('Learning has not run despite recent sessions.')).toBeTruthy();
		expect(screen.getByText(/Archived 1 exact duplicate\./)).toBeTruthy();
		expect(screen.queryByText('Saves are writing normally.')).toBeNull();
	});

	it('is owner only', async () => {
		memoryHealth = Object.assign(new Error('Owner access required'), { status: 403 });
		render(SystemPage);
		const row = await screen.findByTestId('memory-health');
		await waitFor(() => expect(row.textContent).toContain('Available after approval'));
		expect(within(row).queryByRole('button')).toBeNull();
	});
});

describe('help: report a problem', () => {
	const openForm = async () => {
		await fireEvent.click(await screen.findByRole('button', { name: /Describe it/ }));
		return (await screen.findByLabelText('What went wrong?')) as HTMLTextAreaElement;
	};

	it('opens the support report dialog with the description as a frontend error', async () => {
		render(SystemPage);
		expect(screen.getByText('Report a problem')).toBeTruthy();
		const field = await openForm();
		await waitFor(() => expect(document.activeElement).toBe(field));
		await fireEvent.input(field, { target: { value: '  The voice call dropped after a minute  ' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
		expect(supportReportDialog.request?.source).toEqual({
			error: { message: 'The voice call dropped after a minute', source: 'frontend' }
		});
		expect(transport.get).toHaveBeenCalledWith('/support/capabilities');
	});

	it('continues on Enter and keeps Shift+Enter for a new line', async () => {
		render(SystemPage);
		const field = await openForm();
		await fireEvent.input(field, { target: { value: 'Sidebar is empty' } });
		await fireEvent.keyDown(field, { key: 'Enter', shiftKey: true });
		expect(supportReportDialog.request).toBeNull();
		await fireEvent.keyDown(field, { key: 'Enter' });
		expect(supportReportDialog.request?.source).toEqual({ error: { message: 'Sidebar is empty', source: 'frontend' } });
	});

	it('requires a description before opening anything', async () => {
		render(SystemPage);
		const field = await openForm();
		await fireEvent.input(field, { target: { value: '   ' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
		expect((await screen.findByRole('alert')).textContent).toContain('Describe the problem first.');
		expect(field.getAttribute('aria-invalid')).toBe('true');
		expect(supportReportDialog.request).toBeNull();
		await fireEvent.input(field, { target: { value: 'x' } });
		expect(screen.queryByText('Describe the problem first.')).toBeNull();
	});

	it('limits the description and closes on Cancel', async () => {
		render(SystemPage);
		const field = await openForm();
		expect(field.maxLength).toBe(1000);
		await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
		await waitFor(() => expect(screen.queryByLabelText('What went wrong?')).toBeNull());
		expect(supportReportDialog.request).toBeNull();
	});

	it('is hidden when this login cannot send support reports', async () => {
		transport.get.mockImplementation(async (path: string) => {
			if (path === '/support/capabilities') throw Object.assign(new Error('Owner access required'), { status: 403 });
			if (path === '/license') return { ...license };
			return {};
		});
		render(SystemPage);
		await waitFor(() => expect(supportCaps.status).toBe('forbidden'));
		await waitFor(() => expect(screen.queryByText('Report a problem')).toBeNull());
		expect(screen.queryByRole('button', { name: /Describe it/ })).toBeNull();
	});
});
