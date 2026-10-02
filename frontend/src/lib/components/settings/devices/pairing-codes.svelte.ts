/**
 * Pairing codes: "Add device" mints one code for one device.
 *
 * - Worker: a network invite (QR, link, code) another worker scans or pastes
 *   to join this worker's network. Only on an enrolled worker, and only with
 *   an address other computers can reach (never loopback).
 * - Phone or browser: a pairing code the Shuffle app scans or a browser opens.
 * - Shared database: a legacy runner enrollment code (no address embedded),
 *   only offered while the worker has legacy installs and no network.
 *
 * Presenting the code is the authorization; nothing is approved afterwards.
 * Codes work once and expire; an expired code can be replaced.
 */
import { copyText, COPY_ERROR_MESSAGE } from '$lib/utils/clipboard';
import { getErrorMessage } from '$lib/api/client';
import {
	createDeviceGrant,
	listPairingEndpoints,
	revokePairingGrant,
	type DeviceGrant,
	type DeviceGrantPurpose,
	type PairingEndpoint
} from '$lib/api/pairing';
import { formatCountdown, secondsUntil } from '$lib/api/network-status';

export type PairingKind = 'worker' | 'client' | 'runner';

export const KIND_LABEL: Record<PairingKind, string> = {
	worker: 'Worker',
	client: 'Phone or browser',
	runner: 'Shared database'
};

export const KIND_NAME: Record<PairingKind, string> = {
	worker: 'Worker invite',
	client: 'Phone pairing code',
	runner: 'Enrollment code'
};

const PURPOSE: Record<PairingKind, DeviceGrantPurpose> = {
	worker: 'network:join',
	client: 'controller',
	runner: 'runner'
};

export const NO_REACHABLE_ADDRESS = 'No address other computers can reach.';

export function enrollmentCommand(code: string): string {
	return `python run_local.py --join ${code}`;
}

export function endpointLabel(endpoint: PairingEndpoint): string {
	return `${endpoint.label}, ${endpoint.url}${endpoint.loopback ? ' (this computer only)' : ''}`;
}

export interface PairingItem {
	id: string;
	kind: PairingKind;
	endpointUrl: string;
	grant: DeviceGrant | null;
	creating: boolean;
	cancelling: boolean;
	error: string;
	expired: boolean;
	copied: 'code' | 'link' | null;
}

export interface PairingContext {
	enrolled: () => boolean;
	allowLegacyRunner: () => boolean;
}

export class PairingCodes {
	endpoints = $state<PairingEndpoint[]>([]);
	endpointsLoaded = $state(false);
	loadingEndpoints = $state(false);
	endpointError = $state('');
	items = $state<PairingItem[]>([]);
	now = $state(Date.now());

	#ctx: PairingContext;
	#seq = 0;
	#tick: ReturnType<typeof setInterval> | undefined;
	#copyTimer: ReturnType<typeof setTimeout> | undefined;

	constructor(ctx: PairingContext) {
		this.#ctx = ctx;
	}

	get usableEndpoints(): PairingEndpoint[] {
		return this.endpoints.filter((e) => !e.loopback);
	}

	/** The kinds this worker can create now. */
	get kinds(): PairingKind[] {
		return [
			...(this.#ctx.enrolled() ? (['worker'] as const) : []),
			'client' as const,
			...(this.#ctx.allowLegacyRunner() ? (['runner'] as const) : [])
		];
	}

	byId(id: string | null | undefined): PairingItem | undefined {
		return id ? this.items.find((i) => i.id === id) : undefined;
	}

	secondsLeft(item: PairingItem): number {
		return item.grant ? secondsUntil(item.grant.grant.expires_at, this.now) : 0;
	}

	countdown(item: PairingItem): string {
		return formatCountdown(this.secondsLeft(item));
	}

	link(item: PairingItem): string | null {
		return item.grant?.join?.url ?? item.grant?.pairing_url ?? null;
	}

	/** Why Create is not possible right now, or ''. */
	blocker(item: PairingItem): string {
		if (item.kind === 'worker' && this.endpointsLoaded && this.usableEndpoints.length === 0) return NO_REACHABLE_ADDRESS;
		return '';
	}

	async loadEndpoints(): Promise<void> {
		this.loadingEndpoints = true;
		try {
			this.endpoints = await listPairingEndpoints();
			this.endpointError = '';
		} catch (e) {
			this.endpointError = getErrorMessage(e, "Could not load this worker's addresses");
		} finally {
			this.loadingEndpoints = false;
			this.endpointsLoaded = true;
		}
		for (const item of this.items) {
			if (!this.endpoints.some((e) => e.url === item.endpointUrl)) item.endpointUrl = this.defaultEndpoint();
		}
	}

	defaultEndpoint(): string {
		return this.endpoints.find((e) => e.qr_eligible)?.url ?? this.endpoints[0]?.url ?? '';
	}

	/** "Add device": a new code of the default kind, minted right away. */
	async add(): Promise<string> {
		const id = `code:${++this.#seq}`;
		const kind: PairingKind = this.#ctx.enrolled() ? 'worker' : 'client';
		this.items.push({
			id,
			kind,
			endpointUrl: this.defaultEndpoint(),
			grant: null,
			creating: false,
			cancelling: false,
			error: '',
			expired: false,
			copied: null
		});
		this.#startTick();
		void (async () => {
			if (!this.endpointsLoaded) await this.loadEndpoints();
			const item = this.byId(id);
			if (item && !item.grant && !item.creating && !this.blocker(item)) await this.create(item);
		})();
		return id;
	}

	/** Mint (or re-mint) the item's code. */
	async create(item: PairingItem): Promise<void> {
		if (item.creating || item.cancelling) return;
		if (item.kind === 'worker' && !this.#ctx.enrolled()) return;
		if (item.kind === 'runner' && !this.#ctx.allowLegacyRunner()) return;
		item.creating = true;
		item.error = '';
		item.expired = false;
		item.copied = null;
		try {
			const address = this.endpoints.find((e) => e.url === item.endpointUrl) ?? null;
			const endpoint = item.kind === 'runner' ? null : address && !address.loopback ? address.url : null;
			if (item.kind === 'worker' && !endpoint) throw new Error('Choose an address other workers can reach.');
			item.grant = await createDeviceGrant(PURPOSE[item.kind], endpoint);
			this.now = Date.now();
		} catch (e) {
			item.error = getErrorMessage(e, 'Could not create the code');
		} finally {
			item.creating = false;
		}
	}

	/** Change kind or address: the old code is withdrawn and a new one made. */
	async change(item: PairingItem, patch: { kind?: PairingKind; endpointUrl?: string }): Promise<void> {
		if (item.creating || item.cancelling) return;
		if (patch.kind) item.kind = patch.kind;
		if (patch.endpointUrl !== undefined) item.endpointUrl = patch.endpointUrl;
		const old = item.grant;
		item.grant = null;
		item.error = '';
		item.expired = false;
		if (old) await revokePairingGrant(old.grant.id).catch(() => {});
		if (!this.blocker(item)) await this.create(item);
	}

	/** Withdraw the code and drop the item. A failure keeps it to retry. */
	async cancel(item: PairingItem): Promise<boolean> {
		if (item.cancelling) return false;
		const current = item.grant;
		if (!current) {
			this.remove(item.id);
			return true;
		}
		item.cancelling = true;
		item.error = '';
		try {
			await revokePairingGrant(current.grant.id);
			this.remove(item.id);
			return true;
		} catch (e) {
			item.error = getErrorMessage(e, 'Could not cancel the code');
			return false;
		} finally {
			item.cancelling = false;
		}
	}

	remove(id: string): void {
		this.items = this.items.filter((i) => i.id !== id);
		if (!this.items.length) this.#stopTick();
	}

	async copy(item: PairingItem, what: 'code' | 'link', value: string): Promise<void> {
		try {
			await copyText(value);
			item.copied = what;
			clearTimeout(this.#copyTimer);
			this.#copyTimer = setTimeout(() => (item.copied = null), 1800);
		} catch {
			item.copied = null;
			item.error = COPY_ERROR_MESSAGE;
		}
	}

	#startTick() {
		if (this.#tick) return;
		this.#tick = setInterval(() => this.tick(), 1000);
	}

	#stopTick() {
		clearInterval(this.#tick);
		this.#tick = undefined;
	}

	/** Advance the clock and expire codes past their time. */
	tick(now = Date.now()): void {
		this.now = now;
		for (const item of this.items) {
			if (item.grant && new Date(item.grant.grant.expires_at).getTime() <= now) {
				item.grant = null;
				item.expired = true;
			}
		}
	}

	/** Pause the clock (Devices left the screen); codes stay. */
	pause(): void {
		this.#stopTick();
	}

	resume(): void {
		if (this.items.length) {
			this.tick();
			this.#startTick();
		}
	}

	reset(): void {
		this.#stopTick();
		clearTimeout(this.#copyTimer);
		this.endpoints = [];
		this.endpointsLoaded = false;
		this.loadingEndpoints = false;
		this.endpointError = '';
		this.items = [];
		this.#seq = 0;
	}
}
