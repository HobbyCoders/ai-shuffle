/**
 * A newer Claude Code or Antigravity the worker found when it started: one
 * small banner per CLI, with Update.
 *
 * The worker only looks. Nothing is installed until the owner presses Update
 * here or in Settings > Providers, where each provider also has the switch
 * that turns its check off. A banner leaves on its own after a while and
 * comes back on the next load. Closing it hides that version in this browser;
 * the next version shows again.
 */
import { getErrorMessage } from '$lib/api/client';
import { getAntigravityStatus, updateAntigravity } from '$lib/api/antigravity';
import { getCliUpdates, getClaudeCliInstallation, updateClaudeCli, type CliUpdate } from '$lib/api/claude-cli';
import { liveBanners, notify } from '$lib/stores/notifications';

type Provider = CliUpdate['provider'];

const DISMISSED_KEY = 'ai-shuffle-cli-update-dismissed';
/** The worker's check runs in the background after it starts: ask once more later. */
export const SETTLE_MS = 30_000;
/** How long an offer stays up. Left up, it covers controls on a phone. */
export const SHOW_MS = 15_000;
const POLL_MS = 2000;
const JOB_LIMIT_MS = 15 * 60_000;
const RESULT_MS = 8000;

/** The banner on screen for each CLI and the version it offers. */
const shown = new Map<Provider, { id: string; version: string }>();
/** Versions whose banner timed out: not put up again on this load. */
const rested = new Map<Provider, string>();
/** CLIs whose update the owner started from a banner. */
const working = new Set<Provider>();
let installed = false;
let asking = false;
let timer: ReturnType<typeof setTimeout> | null = null;
let stopWatching: (() => void) | null = null;

function dismissedVersions(): Record<string, string> {
	try {
		const raw = JSON.parse(localStorage.getItem(DISMISSED_KEY) ?? '{}');
		return raw && typeof raw === 'object' ? raw : {};
	} catch {
		return {};
	}
}

function rememberDismissed(provider: Provider, version: string): void {
	try {
		localStorage.setItem(DISMISSED_KEY, JSON.stringify({ ...dismissedVersions(), [provider]: version }));
	} catch { /* storage off: the banner shows again on the next load */ }
}

/** Take a banner down without counting it as closed by the owner. */
function drop(provider: Provider): void {
	const banner = shown.get(provider);
	if (!banner) return;
	shown.delete(provider);
	notify.dismissLive(banner.id);
}

function show(update: CliUpdate): void {
	const { provider, latest_version: version } = update;
	if (working.has(provider) || shown.get(provider)?.version === version) return;
	if (dismissedVersions()[provider] === version || rested.get(provider) === version) return;
	drop(provider);
	const id = notify.banner(`${update.name} ${version} is available. This worker has ${update.installed_version}.`, {
		severity: 'info',
		category: 'cli-update',
		duration_ms: 0,
		action: { label: 'Update', onClick: () => void runUpdate(update) },
		// Only the owner's Close hides a version. Newer banners pushing this one off do not.
		onDismiss: () => rememberDismissed(provider, version)
	});
	shown.set(provider, { id, version });
	// Timed here, not by the banner: the next ask has to know it timed out.
	setTimeout(() => {
		if (shown.get(provider)?.id !== id) return;
		rested.set(provider, version);
		drop(provider);
	}, SHOW_MS);
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

interface JobResult { state: string; message: string | null }

async function claudeJob(): Promise<JobResult> {
	// This worker only: the notice names this worker, whatever "Update all workers" says.
	let status = await updateClaudeCli(true);
	const until = Date.now() + JOB_LIMIT_MS;
	while (status.job?.state === 'running' && Date.now() < until) {
		await sleep(POLL_MS);
		status = await getClaudeCliInstallation();
	}
	return { state: status.job?.state ?? 'failed', message: status.job?.message ?? null };
}

async function antigravityJob(): Promise<JobResult> {
	let job = await updateAntigravity();
	const until = Date.now() + JOB_LIMIT_MS;
	while (job.state === 'running' && Date.now() < until) {
		await sleep(POLL_MS);
		job = (await getAntigravityStatus()).install;
	}
	return { state: job.state, message: job.message ?? job.error ?? null };
}

/** The owner pressed Update: run the worker's own update job and report how it ended. */
async function runUpdate(update: CliUpdate): Promise<void> {
	const { provider, name } = update;
	if (working.has(provider)) return;
	working.add(provider);
	const failed = `${name} could not be updated. Try again in Settings > Providers.`;
	const progress = notify.banner(`Updating ${name}`, { severity: 'info', category: 'cli-update', duration_ms: 0 });
	try {
		const job = await (provider === 'claude' ? claudeJob() : antigravityJob());
		if (job.state === 'succeeded') {
			notify.banner(job.message || `${name} is up to date.`, { severity: 'success', category: 'cli-update', duration_ms: RESULT_MS });
		} else if (job.state === 'running') {
			notify.banner(`${name} is still updating. Settings > Providers shows the result.`, { severity: 'info', category: 'cli-update', duration_ms: RESULT_MS });
		} else {
			notify.banner(job.message || failed, { severity: 'error', category: 'cli-update', duration_ms: 0 });
		}
	} catch (e) {
		notify.banner(getErrorMessage(e, failed), { severity: 'error', category: 'cli-update', duration_ms: 0 });
	} finally {
		notify.dismissLive(progress);
		working.delete(provider);
	}
}

/** Ask the worker what its start-time check found and show what is new. */
export async function checkCliUpdates(): Promise<void> {
	if (!installed || asking) return;
	asking = true;
	try {
		const { updates } = await getCliUpdates();
		if (!installed) return;
		const offered = new Set(updates.map((u) => u.provider));
		// Updated elsewhere (Settings, another device, by hand) since the banner went up.
		for (const provider of [...shown.keys()]) if (!offered.has(provider)) drop(provider);
		for (const update of updates) show(update);
	} catch {
		/* A worker that cannot answer has nothing to offer; the next ask tries again. */
	} finally {
		asking = false;
	}
}

/** The worker may have just started (first load, or live sync came back): ask now and once it has settled. */
export function recheckCliUpdates(): void {
	if (!installed) return;
	void checkCliUpdates();
	if (timer) clearTimeout(timer);
	timer = setTimeout(() => { timer = null; void checkCliUpdates(); }, SETTLE_MS);
}

/**
 * Start for a signed-in owner. Returns the teardown. Calling it again while
 * installed is a no-op.
 */
export function installCliUpdateNotice(): () => void {
	if (typeof window === 'undefined' || installed) return () => {};
	installed = true;
	// Forget a banner that left the screen (closed, pressed, or pushed off by newer
	// banners), so the next ask can show it again unless the owner closed it.
	stopWatching = liveBanners.subscribe((live) => {
		for (const [provider, banner] of [...shown]) {
			if (!live.some((b) => b.id === banner.id)) shown.delete(provider);
		}
	});
	recheckCliUpdates();
	return () => {
		installed = false;
		if (timer) clearTimeout(timer);
		timer = null;
		for (const provider of [...shown.keys()]) drop(provider);
		rested.clear();
		stopWatching?.();
		stopWatching = null;
	};
}
