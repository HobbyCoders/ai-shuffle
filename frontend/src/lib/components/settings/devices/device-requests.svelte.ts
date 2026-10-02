/**
 * Approval requests: devices that signed in through the Shuffle account and
 * ask this worker to approve them. Signing in to the account does not grant
 * access to this worker; it is approved here, from this worker's own
 * browser or an already-approved device. A LAN connection alone cannot
 * approve.
 *
 * Rules kept from WorkerDeviceConfirmations:
 * - Approve and Deny each need a second, explicit Confirm.
 * - A decision carries the exact enrollment ids seen when it was armed; a
 *   refresh that shows a replaced credential disarms it.
 * - A failure stays visible and blocks further decisions until Refresh.
 * - Expired or revoked credentials are never offered for approval.
 * - Only an approved viewer reads the list.
 * - Background refresh every 10s, only while visible, nothing armed, no error.
 */
import { get } from 'svelte/store';
import { getErrorMessage } from '$lib/api/client';
import { isAdmin } from '$lib/stores/auth.svelte';
import {
	confirmWorkerDevice,
	listWorkerDeviceConfirmations,
	type WorkerDeviceConfirmation
} from '$lib/api/worker-device-confirmations';

export const REQUESTS_POLL_MS = 10_000;

/** List and selection id of a request. */
export function requestId(device: WorkerDeviceConfirmation): string {
	return `req:${device.device_id}:${device.paired_client_id}`;
}

/** Everything that must be unchanged for an armed decision to still apply. */
export function requestIdentity(device: WorkerDeviceConfirmation): string {
	return [
		device.device_id,
		device.paired_client_id,
		device.mobile_record_id,
		device.confirmation,
		device.revoked_at ?? ''
	].join('|');
}

export function requestExpired(device: WorkerDeviceConfirmation, now = Date.now()): boolean {
	return Boolean(device.api_key_expires_at && Date.parse(device.api_key_expires_at) <= now);
}

export function requestStatusText(device: WorkerDeviceConfirmation): string {
	if (device.revoked_at) return 'Revoked';
	if (device.confirmation === 'pending') return 'Wants to be approved';
	return device.confirmation === 'approved' ? 'Approved' : 'Denied';
}

export function isPendingRequest(device: WorkerDeviceConfirmation, now = Date.now()): boolean {
	return !device.revoked_at && device.confirmation === 'pending' && !requestExpired(device, now);
}

export class DeviceRequests {
	devices = $state<WorkerDeviceConfirmation[]>([]);
	loaded = $state(false);
	loading = $state(false);
	error = $state('');
	notice = $state('');
	saving = $state(false);
	/** The armed decision, with a snapshot of the device as it was seen. */
	armed = $state<{ device: WorkerDeviceConfirmation; approved: boolean } | null>(null);

	#stopped = true;
	#timer: ReturnType<typeof setInterval> | undefined;

	get pendingCount(): number {
		return this.devices.filter((d) => isPendingRequest(d)).length;
	}

	byId(id: string | null | undefined): WorkerDeviceConfirmation | undefined {
		return id ? this.devices.find((d) => requestId(d) === id) : undefined;
	}

	isArmed(device: WorkerDeviceConfirmation, approved: boolean): boolean {
		return (
			this.armed !== null &&
			this.armed.approved === approved &&
			requestIdentity(this.armed.device) === requestIdentity(device)
		);
	}

	arm(device: WorkerDeviceConfirmation, approved: boolean): void {
		this.notice = '';
		this.armed = { device: { ...device }, approved };
	}

	disarm(): void {
		this.armed = null;
	}

	start(): void {
		this.#stopped = false;
		void this.refresh();
		clearInterval(this.#timer);
		this.#timer = setInterval(() => {
			if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return;
			if (!this.armed && !this.error) void this.refresh();
		}, REQUESTS_POLL_MS);
	}

	stop(): void {
		this.#stopped = true;
		clearInterval(this.#timer);
	}

	async refresh(): Promise<void> {
		if (this.loading || this.saving || this.#stopped || !get(isAdmin)) return;
		this.loading = true;
		try {
			const next = await listWorkerDeviceConfirmations();
			if (this.#stopped) return;
			this.devices = next;
			this.loaded = true;
			this.error = '';
			// Never apply an armed decision to a replacement enrollment or credential.
			const armed = this.armed;
			if (armed && !next.some((d) => requestIdentity(d) === requestIdentity(armed.device))) this.armed = null;
		} catch (e) {
			if (!this.#stopped) this.error = getErrorMessage(e, 'Could not load approval requests');
		} finally {
			this.loading = false;
		}
	}

	/** Apply the armed decision. */
	async decide(): Promise<void> {
		const selected = this.armed;
		if (!selected || this.saving || this.error || !get(isAdmin)) return;
		this.saving = true;
		this.notice = '';
		try {
			await confirmWorkerDevice(selected.device, selected.approved);
			if (this.#stopped) return;
			this.armed = null;
			this.notice = selected.approved
				? 'Approved on this worker. Retry the connection on the device.'
				: 'Denied on this worker. Pair a new identity to restore access.';
		} catch (e) {
			if (!this.#stopped) {
				this.armed = null;
				this.error = getErrorMessage(e, 'Could not update this device');
			}
		} finally {
			this.saving = false;
		}
		// Keep a failure visible until the owner refreshes on purpose.
		if (!this.error) await this.refresh();
	}

	reset(): void {
		this.stop();
		this.devices = [];
		this.loaded = false;
		this.loading = false;
		this.error = '';
		this.notice = '';
		this.saving = false;
		this.armed = null;
	}
}
