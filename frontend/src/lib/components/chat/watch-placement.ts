/**
 * Where watches run (#565): the worker named on each watch row, the workers a
 * watch may move to, and where new watches go.
 *
 * Mirrors app/core/watch_placement.py: automatic placement prefers an online
 * worker marked always-on, then an online Linux server; a desktop that may
 * sleep is never picked automatically, and an unavailable chosen worker is
 * refused rather than silently replaced. The server decides; these helpers
 * only describe it.
 */
import { matches } from '$lib/stores/worker-presence';
import type { Install } from '$lib/api/installs';

export type WorkerTier = 'always_on' | 'server' | 'may_sleep';

export interface WorkerChoice {
	id: string;
	name: string;
	tier: WorkerTier;
	online: boolean;
	isSelf: boolean;
}

const TRANSFER = 'transfer:';

export function workerTier(install: Pick<Install, 'platform' | 'capabilities'>): WorkerTier {
	const caps = install.capabilities ?? {};
	if (caps.always_on === true) return 'always_on';
	if (String(install.platform ?? '').toLowerCase() === 'linux' && caps.desktop !== true) return 'server';
	return 'may_sleep';
}

export function tierLabel(tier: WorkerTier): string {
	return tier === 'always_on' ? 'Always on' : tier === 'server' ? 'Server' : 'May sleep';
}

/** A tooltip that says why the tier matters for a watch. */
export function tierTitle(tier: WorkerTier): string {
	return tier === 'always_on'
		? 'Marked always on: preferred for watches'
		: tier === 'server'
			? 'A Linux server: picked for watches when no worker is marked always on'
			: 'May sleep or shut down: its watches stop polling while it is off';
}

function choice(install: Install): WorkerChoice {
	return {
		id: install.device_id || install.id,
		name: install.device_name || install.id,
		tier: workerTier(install),
		online: !!install.online,
		isSelf: !!install.is_self
	};
}

/** Approved workers, this one first, then by name. */
export function workerChoices(installs: readonly Install[] | null): WorkerChoice[] {
	return (installs ?? [])
		.filter((i) => i.approved !== false && !i.revoked_at)
		.map(choice)
		.sort((a, b) => Number(b.isSelf) - Number(a.isSelf) || a.name.localeCompare(b.name));
}

export function findWorker(installs: readonly Install[] | null, id: string | null | undefined): WorkerChoice | null {
	if (!installs || !id) return null;
	const hit = installs.find((i) => matches(i, id));
	return hit ? choice(hit) : null;
}

/** The worker automatic placement would pick now, or null when none qualifies. */
export function automaticWorker(choices: readonly WorkerChoice[]): WorkerChoice | null {
	const rank = (w: WorkerChoice) => (w.tier === 'always_on' ? 0 : 1);
	const candidates = choices.filter((w) => w.online && w.tier !== 'may_sleep');
	return [...candidates].sort((a, b) => rank(a) - rank(b) || a.id.localeCompare(b.id))[0] ?? null;
}

/**
 * The device a fenced row is moving to ('' when the row does not say), or
 * null when it is not mid-move.
 */
export function transferTarget(workerId: string | null | undefined): string | null {
	return workerId?.startsWith(TRANSFER) ? workerId.slice(TRANSFER.length) : null;
}

export interface WatchPlacement {
	/** The executing worker, or null when the directory does not know it. */
	worker: WorkerChoice | null;
	/** The id the row reports, for the tooltip when the worker is unknown. */
	reportedId: string | null;
}

/**
 * The worker a row runs on. `placementDeviceId` is the device that reported
 * it; `workerId` is the executing install (null while the row is fenced).
 */
export function placementOf(
	row: { placementDeviceId?: string | null; workerId?: string | null },
	installs: readonly Install[] | null
): WatchPlacement {
	const workerId = transferTarget(row.workerId) !== null ? null : row.workerId || null;
	const reportedId = row.placementDeviceId || workerId;
	const worker = findWorker(installs, row.placementDeviceId) ?? findWorker(installs, workerId);
	return { worker, reportedId };
}

export interface MoveOption extends WorkerChoice {
	/** Why it cannot be picked, or null when it can. */
	unavailable: string | null;
}

/** Every worker but the current one, each with why it cannot take the watch. */
export function moveOptions(choices: readonly WorkerChoice[], currentId: string | null): MoveOption[] {
	return choices
		.filter((w) => w.id !== currentId)
		.map((w) => ({ ...w, unavailable: w.online ? null : 'Offline' }));
}

/** What the preference row says, and whether new watches can be placed now. */
export interface PreferenceSummary {
	/** "Automatic · ServerWorker", "LaptopWorker", "LaptopWorker (offline)". */
	label: string;
	/** The worker new watches go to now, or null when placement will refuse. */
	effective: WorkerChoice | null;
	/** Why new watches would be refused, or null. */
	problem: string | null;
}

export function preferenceSummary(
	preferredId: string | null,
	choices: readonly WorkerChoice[],
	installs: readonly Install[] | null
): PreferenceSummary {
	if (!preferredId) {
		const auto = automaticWorker(choices);
		return auto
			? { label: `Automatic · ${auto.name}`, effective: auto, problem: null }
			: {
					label: 'Automatic · none online',
					effective: null,
					problem: 'No always-on worker or server is online, so a new watch will ask you to choose a worker.'
				};
	}
	// The worker directory is still loading: say nothing it does not know.
	if (installs === null) return { label: 'Chosen worker', effective: null, problem: null };
	const chosen = findWorker(installs, preferredId);
	if (!chosen) {
		return {
			label: 'A removed worker',
			effective: null,
			problem: 'The chosen worker is no longer in your network. New watches are refused until you choose another.'
		};
	}
	if (!chosen.online) {
		return {
			label: `${chosen.name} (offline)`,
			effective: null,
			problem: `${chosen.name} is offline. New watches are refused until it is back or you choose another worker; they never fall back silently.`
		};
	}
	return { label: chosen.name, effective: chosen, problem: null };
}

/** One row of the worker picker (WatchWorkerPicker.svelte). */
export interface PickerOption {
	/** Worker id; `null` is "automatic". */
	id: string | null;
	name: string;
	/** A line under the name ("Picks ServerWorker now"). */
	detail?: string | null;
	tier?: WorkerTier | null;
	online?: boolean | null;
	/** Why it cannot be picked; disabled when set. */
	unavailable?: string | null;
}
