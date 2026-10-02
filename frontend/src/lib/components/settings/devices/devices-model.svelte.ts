/**
 * Devices: everything that connects to this worker. The Shuffle account,
 * approval requests, pairing codes, this worker and its network, the other
 * workers, phones and browsers, devices known only to the account, and
 * legacy shared-database installs.
 *
 * Rules carried over from DevicesSection:
 * - Status polls every 5s (3s while something is joining), only while the
 *   tab is visible; workers and phones every 10s; network/settings resource
 *   events reload all three. A failed status read keeps the last data and
 *   marks it stale (Offline).
 * - Revoking a device revokes its account registration first, so the account
 *   cannot readmit it, then the local membership, pairing or install. When
 *   the account part succeeded and the local part failed, the error says so
 *   and a retry finishes the rest. The current worker's own registration is
 *   never revoked from here.
 * - Revoking a network worker rotates the network key; its copies of
 *   provider keys are assumed compromised, so a toast offers Providers.
 * - Removing a legacy install also drops its sessions from the app data.
 * - Account registrations are matched to local rows by identity, never name
 *   (`account-devices.ts`).
 */
import { getErrorMessage } from '$lib/api/client';
import { appData } from '$lib/stores/app-data.svelte';
import { notify } from '$lib/stores/notifications';
import { resourceSync } from '$lib/services/resource-sync';
import { listPairedClients, revokePairedClient, type PairedClient } from '$lib/api/pairing';
import { revokeAccountRegistration } from '$lib/api/account';
import { deleteInstall, listInstalls, renameInstall, setInstallApproval, type Install } from '$lib/api/installs';
import {
	adoptAccountNetwork,
	createNetwork,
	getNetworkStatus,
	renameNetworkMember,
	revokeNetworkMember,
	type NetworkStatus
} from '$lib/api/network';
import { deriveNetworkState, relativeTime, type NetworkUiState } from '$lib/api/network-status';
import { openSettings } from '$lib/settings/navigation.svelte';
import { isForbidden } from '$lib/settings/access.svelte';
import { accountRegistrations, type AccountDeviceRow } from '$lib/components/deck/cards/settings/account-devices';
import { AccountFlow } from './account-flow.svelte';
import { DeviceRequests } from './device-requests.svelte';
import { PairingCodes } from './pairing-codes.svelte';

export const STATUS_MS = 5000;
export const JOINING_MS = 3000;
export const CLIENTS_MS = 10_000;
const NOTICE_MS = 4000;

export interface JoiningDevice {
	key: string;
	name: string;
	platform: string | null;
	via: 'invite' | 'account';
}

export function clientKind(client: PairedClient): 'Browser' | 'Phone' | 'Device' {
	if (/browser/i.test(client.platform) || ['browser', 'pwa'].includes(client.client_type)) return 'Browser';
	return client.client_type === 'mobile' ? 'Phone' : 'Device';
}

/** Platform text worth showing ("linux-browser" -> "linux"; bare kinds dropped). */
export function clientPlatform(client: PairedClient): string {
	if (!client.platform || /^(browser|pwa|mobile)$/i.test(client.platform)) return '';
	return client.platform.replace(/-browser$/i, '');
}

export class DevicesModel {
	status = $state<NetworkStatus | null>(null);
	stale = $state(false);
	statusError = $state('');
	/** Reads were refused: this device is not approved yet. */
	forbidden = $state(false);
	installs = $state<Install[]>([]);
	selfInstallId = $state('');
	workersLoaded = $state(false);
	workersError = $state('');
	clients = $state<PairedClient[]>([]);
	clientsLoaded = $state(false);
	clientsError = $state('');
	/** Last action failure. */
	error = $state('');
	/** Short-lived success line. */
	notice = $state('');
	/** Key of the device an action is running on. */
	saving = $state<string | null>(null);
	adopting = $state(false);
	startingFresh = $state(false);
	now = $state(Date.now());

	readonly account: AccountFlow;
	readonly requests: DeviceRequests;
	readonly pairing: PairingCodes;

	#mounts = 0;
	#stopped = true;
	#statusTimer: ReturnType<typeof setTimeout> | null = null;
	#clientsTimer: ReturnType<typeof setInterval> | null = null;
	#noticeTimer: ReturnType<typeof setTimeout> | undefined;
	#unsubscribe: (() => void) | null = null;
	#pendingClients = 0;
	#pendingWorkers = 0;

	constructor() {
		this.account = new AccountFlow(() => this.workerName);
		this.requests = new DeviceRequests();
		this.pairing = new PairingCodes({
			enrolled: () => this.enrolled,
			allowLegacyRunner: () => this.allowLegacyRunner
		});
	}

	// ── Derived ───────────────────────────────────────────────────────────
	get uiState(): NetworkUiState {
		return deriveNetworkState(this.status, this.stale);
	}
	get enrolled(): boolean {
		return Boolean(this.status?.enrolled);
	}
	get self() {
		return this.status?.members?.find((m) => m.is_self) ?? null;
	}
	get members() {
		return (this.status?.members ?? []).filter((m) => m.status === 'active' && !m.is_self);
	}
	get legacyInstalls(): Install[] {
		return this.installs.filter((i) => !i.device_id && i.id !== this.selfInstallId && !i.is_self);
	}
	get joining(): JoiningDevice[] {
		const s = this.status;
		return [
			...(s?.pending_joins ?? []).map((j) => ({
				key: `join:${j.id}`,
				name: j.name,
				platform: j.platform,
				via: 'invite' as const
			})),
			...(s?.hub?.pending_devices ?? []).map((d) => ({
				key: `hub:${d.device_id}`,
				name: d.name || d.device_id,
				platform: d.platform,
				via: 'account' as const
			}))
		];
	}
	get workerName(): string {
		return (
			this.self?.name ?? this.installs.find((i) => i.is_self || i.id === this.selfInstallId)?.device_name ?? 'My worker'
		);
	}
	get registrations(): AccountDeviceRow[] {
		return accountRegistrations(this.account.profile, this.status, this.installs, this.clients);
	}
	/** Registrations with no local row: shown as "On your account". */
	get accountDevices(): AccountDeviceRow[] {
		return this.registrations.filter((d) => !d.localKey);
	}
	get workerCount(): number {
		return (this.status ? 1 : 0) + this.members.length + this.legacyInstalls.length;
	}
	get total(): number {
		return this.workerCount + this.clients.length + this.accountDevices.length;
	}
	get complete(): boolean {
		return (
			this.workersLoaded &&
			this.clientsLoaded &&
			this.account.inventoryReady &&
			!this.workersError &&
			!this.clientsError &&
			this.status !== null
		);
	}
	get enrollmentPending(): boolean {
		const e = this.status?.enrollment?.status;
		return !this.enrolled && (e === 'pending' || e === 'syncing');
	}
	get waitingOnAccount(): boolean {
		const s = this.status;
		return !this.enrolled && !s?.enrollment && Boolean(s?.hub?.linked) && s?.hub?.status === 'pending';
	}
	get switchingToAccount(): boolean {
		const s = this.status;
		return this.enrolled && Boolean(s?.hub?.linked) && s?.hub?.status === 'conflict';
	}
	get accountWorkers() {
		return (this.status?.hub?.account_network?.devices ?? []).filter(
			(d) => d.status === 'active' && d.kind !== 'consumer'
		);
	}
	get allowLegacyRunner(): boolean {
		return this.workersLoaded && !this.enrolled && this.installs.length > 0;
	}
	get startupSyncNote(): string {
		const st = this.status?.startup_sync;
		if (st?.status !== 'offline' && st?.status !== 'partial') return '';
		return st.reason || 'Startup sync could not reach every worker. Showing saved data while sync retries.';
	}

	/** Account registration linked to a local row, if any. */
	linkedRegistration(key: string): AccountDeviceRow | undefined {
		return this.registrations.find((d) => d.localKey === key);
	}

	seen(input: string | null | undefined): string {
		if (!input) return 'Not seen yet';
		return `Seen ${relativeTime(input, this.now)}`;
	}

	// ── Lifecycle ─────────────────────────────────────────────────────────
	/** Start loading and polling while a Devices view is mounted. Returns stop. */
	start(): () => void {
		this.#mounts += 1;
		if (this.#mounts === 1) this.#begin();
		let done = false;
		return () => {
			if (done) return;
			done = true;
			this.#mounts -= 1;
			if (this.#mounts === 0) this.#end();
		};
	}

	#begin() {
		this.#stopped = false;
		void this.loadAll();
		this.#scheduleStatus();
		this.#clientsTimer = setInterval(() => {
			if (!this.#pendingClients) void this.loadClients();
			if (!this.#pendingWorkers) void this.loadWorkers();
			this.now = Date.now();
		}, CLIENTS_MS);
		this.#unsubscribe = resourceSync.subscribe(['settings', 'network'], () => this.loadAll());
		this.account.start();
		this.requests.start();
		this.pairing.resume();
	}

	#end() {
		this.#stopped = true;
		if (this.#statusTimer) clearTimeout(this.#statusTimer);
		if (this.#clientsTimer) clearInterval(this.#clientsTimer);
		this.#statusTimer = null;
		this.#clientsTimer = null;
		this.#unsubscribe?.();
		this.#unsubscribe = null;
		this.account.stop();
		this.requests.stop();
		this.pairing.pause();
	}

	#scheduleStatus() {
		if (this.#stopped) return;
		if (this.#statusTimer) clearTimeout(this.#statusTimer);
		const busy = this.enrollmentPending || this.waitingOnAccount || this.switchingToAccount || this.joining.length > 0;
		this.#statusTimer = setTimeout(
			async () => {
				if (typeof document === 'undefined' || document.visibilityState === 'visible') await this.loadStatus();
				this.#scheduleStatus();
			},
			busy ? JOINING_MS : STATUS_MS
		);
	}

	async loadAll(): Promise<void> {
		await Promise.all([this.loadStatus(), this.loadWorkers(), this.loadClients()]);
	}

	async loadStatus(): Promise<void> {
		try {
			const next = await getNetworkStatus();
			if (this.#stopped) return;
			const prev = this.status;
			const wasJoining = prev !== null && !prev.enrolled && Boolean(prev.enrollment);
			this.status = next;
			this.stale = false;
			this.statusError = '';
			if (wasJoining && next.enrolled) this.flash(`Joined ${next.network?.name || 'the network'}`);
		} catch (e) {
			if (this.#stopped) return;
			if (isForbidden(e)) {
				this.forbidden = true;
				return;
			}
			this.stale = true;
			if (this.status === null) this.statusError = getErrorMessage(e, 'Could not load the worker network');
		}
	}

	async loadWorkers(): Promise<void> {
		if (this.saving?.startsWith('legacy:')) return;
		this.#pendingWorkers++;
		try {
			const response = await listInstalls();
			if (this.#stopped) return;
			this.selfInstallId = response.self_instance_id;
			this.installs = response.installs;
			this.workersLoaded = true;
			this.workersError = '';
		} catch (e) {
			if (this.#stopped) return;
			if (isForbidden(e)) this.forbidden = true;
			else this.workersError = getErrorMessage(e, 'Could not load workers');
		} finally {
			this.#pendingWorkers--;
		}
	}

	async loadClients(): Promise<void> {
		if (this.saving?.startsWith('client:')) return;
		this.#pendingClients++;
		try {
			const response = await listPairedClients();
			if (this.#stopped) return;
			this.clients = response;
			this.clientsLoaded = true;
			this.clientsError = '';
		} catch (e) {
			if (this.#stopped) return;
			if (isForbidden(e)) this.forbidden = true;
			else this.clientsError = getErrorMessage(e, 'Could not load phones and browsers');
		} finally {
			this.#pendingClients--;
		}
	}

	flash(message: string): void {
		clearTimeout(this.#noticeTimer);
		this.notice = message;
		this.#noticeTimer = setTimeout(() => (this.notice = ''), NOTICE_MS);
	}

	// ── Actions ───────────────────────────────────────────────────────────
	/** Rename `self`, `member:<id>` or `legacy:<id>`. Returns true when saved. */
	async rename(key: string, rawName: string): Promise<boolean> {
		const name = rawName.trim();
		if (!name || this.saving) return false;
		this.saving = key;
		this.error = '';
		try {
			if (key === 'self') {
				const self = this.self;
				if (!self) return false;
				await renameNetworkMember(self.device_id, name);
			} else if (key.startsWith('legacy:')) {
				const id = key.slice('legacy:'.length);
				const updated = await renameInstall(id, name);
				this.installs = this.installs.map((i) => (i.id === id ? { ...i, ...updated } : i));
			} else if (key.startsWith('member:')) {
				await renameNetworkMember(key.slice('member:'.length), name);
			} else {
				return false;
			}
			await this.loadStatus();
			return true;
		} catch (e) {
			this.error = getErrorMessage(e, 'Could not rename the device');
			return false;
		} finally {
			this.saving = null;
		}
	}

	/**
	 * Revoke a device's access (account registration first), or with
	 * `remove` delete a legacy install. Returns true when everything went.
	 */
	async revoke(key: string, name: string, action: 'revoke' | 'remove' = 'revoke'): Promise<boolean> {
		if (this.saving) return false;
		const accounts = this.registrations.filter((d) => d.key === key || d.localKey === key);
		if (accounts.some((d) => d.current)) return false;
		this.saving = key;
		this.error = '';
		let accountRevoked = false;
		let cleanupPending = false;
		let ok = false;
		try {
			// Revoke account authority first so it cannot readmit the local device.
			for (const device of accounts) {
				const result = await revokeAccountRegistration(device.registration, device.id);
				if (!result.ok) throw new Error('Account revocation was not confirmed');
				accountRevoked = true;
				cleanupPending ||= Boolean(result.cleanup_pending);
				const profile = this.account.profile;
				if (profile)
					this.account.profile = {
						...profile,
						servers: profile.servers.filter((w) => device.registration !== 'servers' || w.id !== device.id),
						devices: profile.devices?.filter((d) => device.registration !== 'devices' || d.id !== device.id)
					};
			}
			if (key.startsWith('client:')) {
				const id = key.slice('client:'.length);
				await revokePairedClient(id);
				this.clients = this.clients.filter((c) => c.id !== id);
			} else if (key.startsWith('member:')) {
				const result = await revokeNetworkMember(key.slice('member:'.length));
				if (result?.rotate_provider_keys_recommended) this.#rotateKeysToast(name);
				await this.loadStatus();
			} else if (key.startsWith('legacy:')) {
				const id = key.slice('legacy:'.length);
				if (action === 'remove') {
					await deleteInstall(id);
					this.installs = this.installs.filter((i) => i.id !== id);
					appData.dropDeviceSessions(id);
				} else {
					const updated = await setInstallApproval(id, false);
					this.installs = this.installs.map((i) => (i.id === id ? { ...i, ...updated } : i));
				}
			}
			this.flash(action === 'remove' ? `${name} removed` : `${name} access revoked`);
			if (cleanupPending) this.error = 'Access was revoked, but remote connection cleanup is still pending.';
			ok = true;
		} catch (e) {
			this.error = getErrorMessage(e, 'Could not revoke device access');
			if (accountRevoked) this.error += ' Account access was revoked. Retry to finish removing the remaining access.';
		} finally {
			if (accountRevoked) await this.account.loadProfile(true);
			this.saving = null;
		}
		return ok;
	}

	#rotateKeysToast(name: string) {
		notify.banner(`${name} revoked. Rotate the provider keys it held.`, {
			severity: 'warning',
			action: { label: 'Open providers', onClick: () => openSettings({ category: 'providers' }) }
		});
	}

	async approveLegacy(install: Install, approved: boolean): Promise<boolean> {
		if (this.saving) return false;
		this.saving = `legacy:${install.id}`;
		this.error = '';
		try {
			const updated = await setInstallApproval(install.id, approved);
			this.installs = this.installs.map((i) => (i.id === install.id ? { ...i, ...updated } : i));
			this.flash(approved ? 'Worker approved' : 'Worker access revoked');
			return true;
		} catch (e) {
			this.error = getErrorMessage(e, approved ? 'Could not approve the worker' : 'Could not revoke the worker');
			return false;
		} finally {
			this.saving = null;
		}
	}

	/** Keep separate settings: found this worker's own network. */
	async startFresh(): Promise<void> {
		this.startingFresh = true;
		this.error = '';
		try {
			await createNetwork();
			this.flash('This worker now keeps its own settings');
			await this.loadStatus();
		} catch (e) {
			this.error = getErrorMessage(e, 'Could not set up this worker');
		} finally {
			this.startingFresh = false;
		}
	}

	/** Retry a failed switch to the account's network. */
	async adopt(): Promise<void> {
		this.adopting = true;
		this.error = '';
		try {
			await adoptAccountNetwork();
			this.flash("Switching to your account's settings");
			await this.loadStatus();
			this.#scheduleStatus();
		} catch (e) {
			this.error = getErrorMessage(e, "Could not switch to the account's settings");
		} finally {
			this.adopting = false;
		}
	}

	/** Back to a fresh state (tests). */
	reset(): void {
		if (this.#mounts > 0) {
			this.#mounts = 0;
			this.#end();
		}
		clearTimeout(this.#noticeTimer);
		this.status = null;
		this.stale = false;
		this.statusError = '';
		this.forbidden = false;
		this.installs = [];
		this.selfInstallId = '';
		this.workersLoaded = false;
		this.workersError = '';
		this.clients = [];
		this.clientsLoaded = false;
		this.clientsError = '';
		this.error = '';
		this.notice = '';
		this.saving = null;
		this.adopting = false;
		this.startingFresh = false;
		this.account.reset();
		this.requests.reset();
		this.pairing.reset();
	}
}

/** One model per app: pairing codes survive leaving Devices, and the index reads pending requests. */
export const devicesModel = new DevicesModel();
