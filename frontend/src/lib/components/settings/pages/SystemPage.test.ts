import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const transport = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn(), patch: vi.fn() }));
const fetchAndDownload = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/client', () => ({
	api: transport,
	fetchAndDownload,
	getErrorMessage: (e: { detail?: string; message?: string }, fallback: string) => e?.detail ?? e?.message ?? fallback,
	isApiError: (e: unknown) => typeof e === 'object' && e !== null && 'status' in e
}));
vi.mock('$lib/stores/app-data.svelte', () => ({ appData: { loadProjects: vi.fn() } }));

import SystemPage from './SystemPage.svelte';
import { resourceSync } from '$lib/services/resource-sync';
import { assertCategoryCoverage } from '$lib/settings/testing/coverage';
import { supportCaps, supportReportDialog } from '$lib/stores/support-report.svelte';

let policy: { allow_updates: boolean; update_all_workers: boolean };
let workspace: string;
let workers: Record<string, unknown>[];
let memoryHealth: unknown;

beforeEach(() => {
	vi.resetAllMocks();
	policy = { allow_updates: true, update_all_workers: false };
	workspace = '/data/workspace';
	workers = [{ device_id: 'self', device_name: 'Current worker', online: true, supported: true, phase: 'idle' }];
	memoryHealth = { state: 'healthy', checked_at: 1, signals: [], repairs: [] };
	transport.get.mockImplementation(async (path: string) => {
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
const refusalEvidence = { revision: mainRevision, reason: 'Edge publication waits for a local full-suite pass on main. Run on a clean checkout of main: python scripts/local_ci.py full --attest' };
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

describe('held main in check results', () => {
	it('shows the SHA, gate reason and next step without expanding Running revisions, and refreshes each check', async () => {
		const get = transport.get.getMockImplementation()!;
		let refusal: typeof refusalEvidence | null = refusalEvidence;
		transport.get.mockImplementation(async (path: string) => path === '/settings/application-updates/deploy-status'
			? { origin_main: mainRevision, workers: [{ online: true, channel: 'edge', behind: 2, edge_publication_refusal: refusal }] }
			: get(path));
		transport.post.mockResolvedValue({ workers: [] });
		// The updater found the same published revision it already runs.
		Object.assign(workers[0], { phase: 'ready', version: '83e64013c', running_version: '83e64013c' });
		render(SystemPage);
		const check = screen.getByRole('button', { name: 'Check now' });
		await waitFor(() => expect((check as HTMLButtonElement).disabled).toBe(false));
		await fireEvent.click(check);
		const notice = await screen.findByText(/Main is at .*not published to edge yet/);
		expect(notice.textContent).toContain(mainRevision);
		expect(notice.textContent).toContain(refusalEvidence.reason);
		expect(screen.getByText('Up to date')).toBeTruthy();
		expect(screen.queryByText(/Running revisions compares/)).toBeNull();
		refusal = null;
		await waitFor(() => expect((check as HTMLButtonElement).disabled).toBe(false));
		await fireEvent.click(check);
		await waitFor(() => expect(screen.queryByText(/Main is at .*not published to edge yet/)).toBeNull());
	});

	it.each(['stale', 'stable', 'offline', 'unavailable'])('does not claim a hold for %s evidence', async (scenario) => {
		const get = transport.get.getMockImplementation()!;
		transport.get.mockImplementation(async (path: string) => {
			if (path !== '/settings/application-updates/deploy-status') return get(path);
			if (scenario === 'unavailable') throw new Error('GitHub unavailable');
			return { origin_main: mainRevision, workers: [{
				online: scenario !== 'offline', channel: scenario === 'stable' ? 'stable' : 'edge', behind: 2,
				edge_publication_refusal: { ...refusalEvidence, revision: scenario === 'stale' ? 'old123456' : mainRevision }
			}] };
		});
		transport.post.mockResolvedValue({ workers: [] });
		render(SystemPage);
		const check = screen.getByRole('button', { name: 'Check now' });
		await waitFor(() => expect((check as HTMLButtonElement).disabled).toBe(false));
		await fireEvent.click(check);
		await waitFor(() => expect(transport.get).toHaveBeenCalledWith('/settings/application-updates/deploy-status'));
		expect(screen.queryByText(/Main is at .*not published to edge yet/)).toBeNull();
	});
});

describe('coverage', () => {
	it('renders a row for every System entry', async () => {
		await assertCategoryCoverage('system', () => render(SystemPage).container, {
			settle: () => waitFor(() => expect(screen.getByTestId('memory-health').textContent).toContain('Healthy'))
		});
	});

	it('no longer has License rows; they moved to Cloud', async () => {
		render(SystemPage);
		await waitFor(() => expect(screen.getByTestId('memory-health').textContent).not.toBe(''));
		expect(screen.queryByRole('heading', { name: 'License' })).toBeNull();
		expect(transport.get).not.toHaveBeenCalledWith('/license');
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

describe('export all data', () => {
	it('downloads one ZIP of everything and says to keep it private', async () => {
		let finish!: () => void;
		fetchAndDownload.mockImplementation(() => new Promise<void>((resolve) => (finish = resolve)));
		render(SystemPage);
		await fireEvent.click(screen.getByRole('button', { name: 'Export' }));
		expect(fetchAndDownload).toHaveBeenCalledExactlyOnceWith('/data-export/all', 'ai-shuffle-export-private.zip');
		const busy = screen.getByRole('button', { name: 'Exporting…' }) as HTMLButtonElement;
		expect(busy.disabled).toBe(true);
		expect(screen.getByText(/A long history takes a few minutes/)).toBeTruthy();
		finish();
		expect(await screen.findByText(/Downloaded\. Chats are raw backups, so keep the file private\./)).toBeTruthy();
		expect((screen.getByRole('button', { name: 'Export' }) as HTMLButtonElement).disabled).toBe(false);
	});

	it('reports a refused export', async () => {
		fetchAndDownload.mockRejectedValue({ detail: 'Owner access required', status: 403 });
		render(SystemPage);
		await fireEvent.click(screen.getByRole('button', { name: 'Export' }));
		expect(await screen.findByText('Owner access required')).toBeTruthy();
		expect(screen.queryByText(/A long history takes a few minutes/)).toBeNull();
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
			return {};
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
			return {};
		});
		render(SystemPage);
		await waitFor(() => expect(supportCaps.status).toBe('forbidden'));
		await waitFor(() => expect(screen.queryByText('Report a problem')).toBeNull());
		expect(screen.queryByRole('button', { name: /Describe it/ })).toBeNull();
	});
});
