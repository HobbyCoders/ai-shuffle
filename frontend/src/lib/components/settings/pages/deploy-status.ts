/**
 * System > Updates > Running revisions: which revision each worker runs and
 * how far it is behind origin/main (GET /settings/application-updates/deploy-status,
 * app/core/deploy_status.py). Answers "is my merged change live here?" without
 * checking each worker by hand.
 */
import { api, getErrorMessage } from '$lib/api/client';
import type { UpdateResultRow } from './updates-state.svelte';

/** Explicit current-main gate evidence; absent on older workers/backends. */
export interface EdgePublicationRefusal {
	/** Same short revision format as DeployReport.origin_main. */
	revision: string;
	reason: string;
}

export interface DeployWorker {
	label: string;
	is_self?: boolean;
	online: boolean;
	short?: string | null;
	app_version?: string | null;
	channel?: string | null;
	behind?: number | null;
	action?: string;
	reason?: string | null;
	edge_publication_refusal?: EdgePublicationRefusal | null;
}

export interface DeployReport {
	origin_main?: string | null;
	reason?: string | null;
	workers: DeployWorker[];
	summary: string;
}

/** The short chip text for one worker: "Up to date", "3 behind", "Offline". */
export function chipText(worker: DeployWorker): string {
	if (!worker.online) return 'Offline';
	if (worker.behind === 0) return 'Up to date';
	if (worker.behind != null) return `${worker.behind} behind`;
	return 'Unknown';
}

export function deployRows(report: DeployReport): UpdateResultRow[] {
	return (report.workers ?? []).map((worker, i) => {
		const tone = !worker.online ? 'off' : worker.behind === 0 ? 'ok' : worker.behind != null ? 'warn' : 'off';
		const detail = [
			worker.short,
			worker.channel,
			worker.behind ? worker.action : null,
			worker.behind == null && worker.online ? worker.reason : null
		].filter(Boolean);
		return {
			id: `${worker.label}-${i}`,
			name: worker.is_self ? `${worker.label} (this worker)` : worker.label,
			tone,
			text: detail.length ? `${chipText(worker)} · ${detail.join(' · ')}` : chipText(worker)
		};
	});
}

/** One-line value for the collapsed row. */
export function deploySummary(report: DeployReport | null): string {
	if (!report) return 'Loading';
	const online = (report.workers ?? []).filter((w) => w.online);
	if (online.length && online.every((w) => w.behind === 0)) return 'All up to date';
	const behind = online.filter((w) => (w.behind ?? 0) > 0);
	if (behind.length) return behind.map((w) => `${w.label} ${w.behind} behind`).join(', ');
	return 'Unknown';
}

export async function loadDeployStatus(): Promise<{ report: DeployReport | null; error: string }> {
	try {
		const report = await api.get<DeployReport>('/settings/application-updates/deploy-status');
		return { report: report && Array.isArray(report.workers) ? report : null, error: '' };
	} catch (err) {
		return { report: null, error: getErrorMessage(err) };
	}
}
