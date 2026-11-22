<script lang="ts">
	/**
	 * Security: who can reach this worker. Sign-in (owner password, trusted
	 * LAN) and terminals opened from other workers. The agent rows (chat
	 * instructions, computer use, agents run as you) are on AgentsPage.svelte.
	 */
	import { onMount } from 'svelte';
	import { api, getErrorMessage } from '$lib/api/client';
	import { terminalsApi } from '$lib/api/terminals';
	import { fetchOwnerStatus, setTrustedLan, type OwnerStatus, type OwnerStatusFailure } from '$lib/api/owner';
	import { ownerAccess } from '$lib/stores/owner-access.svelte';
	import { watchResources } from '$lib/services/watch-resources';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import { FORBIDDEN_TEXT, isForbidden } from '$lib/settings/access.svelte';
	import PreferencePage from '../shell/PreferencePage.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import Switch from '../rows/Switch.svelte';
	import PeerDeviceLinks from '$lib/components/terminal/PeerDeviceLinks.svelte';
	import ExpandToggle from '../rows/ExpandToggle.svelte';
	import InlineConfirm from '../rows/InlineConfirm.svelte';
	import OwnerPasswordForm from './editors/OwnerPasswordForm.svelte';
	import { couldNotLoad, couldNotLoadSentence, handOffRetryFocus } from './load-failure';
	import './editors/editors.css';

	/* ---------- owner access ---------- */

	let owner = $state<OwnerStatus | null>(null);
	/** Why the first load failed; a later reload failure keeps the last status. */
	let ownerFailure = $state<OwnerStatusFailure | null>(null);
	let ownerOpen = $state(false);
	let ownerOutcome = $state<'changed' | 'set' | null>(null);
	let lanBusy = $state(false);
	let signInError = $state('');
	const uid = $props.id();
	const ownerErrorId = `${uid}-owner-error`;

	/* Loading, loaded or failed, the row keeps one shape: a hint line and the
	   value button (which a narrow page drops under the label), so the rows
	   under it are not pushed around when the status lands (#1301). A failed
	   load says so in that hint, not in a notice inserted under the group,
	   which would push the groups below down (#1403). */
	const ownerPending = $derived(!owner && !ownerFailure);
	/** The load failed for a reason Try again can fix (not 403, not an older worker). */
	const ownerRetryable = $derived(
		!owner && !!ownerFailure && ownerFailure.reason !== 'unsupported' && !(ownerFailure.reason === 'error' && ownerFailure.code === 403)
	);
	const ownerHint = $derived(
		ownerOutcome === 'changed'
			? 'Other browsers signed out'
			: ownerOutcome === 'set'
				? 'Password set'
				: owner
					? owner.password_set
						? 'Asked once per browser'
						: 'Not set yet'
					: ownerPending
						? 'Loading…'
						: ownerFailure?.reason === 'unsupported'
							? 'Needs a newer worker'
							: ownerFailure?.reason === 'error' && ownerFailure.code === 403
								? 'Owner only'
								: couldNotLoad(ownerFailure?.reason === 'error' ? ownerFailure.code : null)
	);

	async function loadOwner() {
		const result = await fetchOwnerStatus();
		if (result.ok) {
			owner = result.status;
			ownerFailure = null;
		} else if (!owner) {
			ownerFailure = result;
		}
	}

	async function passwordSaved(outcome: 'changed' | 'set') {
		ownerOutcome = outcome;
		ownerOpen = false;
		signInError = '';
		flashSaved();
		await loadOwner();
		await ownerAccess.refresh();
	}

	async function setLan(enabled: boolean) {
		if (lanBusy || !owner) return;
		lanBusy = true;
		signInError = '';
		const before = owner.trusted_lan;
		owner = { ...owner, trusted_lan: enabled };
		try {
			await setTrustedLan(enabled);
			flashSaved();
			await loadOwner();
			await ownerAccess.refresh();
		} catch (e: unknown) {
			if (owner) owner = { ...owner, trusted_lan: before };
			signInError = getErrorMessage(e, 'The setting could not be saved.');
		} finally {
			lanBusy = false;
		}
	}

	/* ---------- terminals on other workers ---------- */

	let peerTerminalsOn = $state<boolean | null>(null);
	let peerBusy = $state(false);
	let peerError = $state('');
	let workerName = $state('this worker');

	async function loadPeerTerminals() {
		try {
			const policy = await api.get<{ network_peer_terminals: boolean }>('/settings/owner-policy');
			peerTerminalsOn = policy.network_peer_terminals === true;
			peerError = '';
		} catch (e: unknown) {
			if (isForbidden(e)) peerTerminalsOn = false;
			else peerError = getErrorMessage(e, 'Could not load this setting.');
		}
		try {
			const worker = await terminalsApi.list();
			workerName = worker.worker?.name || 'this worker';
		} catch {
			// The list is only for the worker's name; the default words still work.
		}
	}

	async function setPeerTerminals(on: boolean) {
		peerBusy = true;
		peerError = '';
		try {
			const saved = await api.put<{ network_peer_terminals: boolean }>('/settings/owner-policy', { network_peer_terminals: on });
			peerTerminalsOn = saved.network_peer_terminals === true;
			flashSaved();
		} catch (e: unknown) {
			peerError = isForbidden(e) ? FORBIDDEN_TEXT : getErrorMessage(e, 'Could not save this setting.');
		} finally {
			peerBusy = false;
		}
	}

	/* ---------- retrying a failed load ---------- */

	let retrying = $state(false);

	async function retryOwner() {
		if (retrying) return;
		retrying = true;
		try {
			if (ownerRetryable) await loadOwner();
		} finally {
			retrying = false;
		}
	}

	$effect.pre(() => {
		void ownerRetryable;
		handOffRetryFocus();
	});

	/* ---------- lifecycle ---------- */

	onMount(() => {
		void loadOwner();
		void loadPeerTerminals();
	});
	watchResources(['settings'], () => void loadPeerTerminals());
</script>

{#snippet peerDevices()}<PeerDeviceLinks workerName={workerName} full />{/snippet}

<PreferencePage category="security">
	<RowGroup title="Sign-in">
		<SettingRow id="security.ownerPassword" hint={ownerHint} expanded={ownerOpen && owner ? ownerForm : undefined}>
			{#if owner}
				<ExpandToggle value={owner.password_set ? 'Set' : 'Not set'} bind:open={ownerOpen} />
			{:else if ownerFailure?.reason === 'unsupported'}
				<ExpandToggle value="Not supported" open={false} disabled />
			{:else if ownerFailure?.reason === 'error' && ownerFailure.code === 403}
				<ExpandToggle value={FORBIDDEN_TEXT} open={false} disabled />
			{:else if ownerFailure}
				<ExpandToggle value="Unavailable" open={false} disabled />
				<button type="button" class="st-btn sm" data-retry-load aria-disabled={retrying || undefined} aria-describedby={retrying ? undefined : ownerErrorId} onclick={retryOwner}>
					{retrying ? 'Retrying…' : 'Try again'}
				</button>
			{:else}
				<ExpandToggle value="" open={false} pending />
			{/if}
		</SettingRow>
		<SettingRow
			id="security.trustedLan"
			value={owner?.trusted_lan}
			onReset={() => void setLan(false)}
		>
			<InlineConfirm
				question="Any device becomes owner?"
				confirmLabel="Trust network"
				onConfirm={() => setLan(true)}
			>
				{#snippet trigger(arm)}
					<Switch
						checked={owner?.trusted_lan ?? false}
						disabled={!owner || lanBusy}
						onchange={(on) => {
							// Turning on asks first: the question replaces the switch, so it
							// is back off if the question is cancelled.
							if (on) arm();
							else void setLan(false);
						}}
					/>
				{/snippet}
			</InlineConfirm>
		</SettingRow>
	</RowGroup>
	<!-- The row's hint shows a failed load; these say it in full to a screen
	     reader without taking space between the groups (#1403). The alert
	     leaves while Try again runs, so a second failure is announced too. -->
	{#if ownerRetryable && !retrying && ownerFailure}
		<p class="st-vh" role="alert" id={ownerErrorId}>
			{couldNotLoadSentence('owner access status', ownerFailure.reason === 'error' ? ownerFailure.code : null)}
		</p>
	{:else if ownerFailure?.reason === 'unsupported' && !owner}
		<p class="st-vh" role="status">This worker does not support owner passwords. Update the worker to manage them here.</p>
	{/if}
	{#if signInError}<p class="st-msg err" role="alert">{signInError}</p>{/if}
	{#if ownerOutcome}
		<p class="st-vh" role="status">
			{ownerOutcome === 'changed' ? 'Password changed. Every other browser was signed out.' : 'Owner password set.'}
		</p>
	{/if}

	<RowGroup title="Terminals on other workers">
		<SettingRow id="security.peerTerminals" dim={peerTerminalsOn === null} value={peerTerminalsOn ?? undefined} onReset={() => void setPeerTerminals(false)}
			expanded={peerTerminalsOn ? peerDevices : undefined}>
			<Switch checked={peerTerminalsOn === true} disabled={peerBusy || peerTerminalsOn === null} onchange={(on) => void setPeerTerminals(on)} />
		</SettingRow>
	</RowGroup>
	{#if peerError}<p class="st-msg err" role="alert">{peerError}</p>{/if}
</PreferencePage>

{#snippet ownerForm()}
	{#if owner}
		<OwnerPasswordForm status={owner} onSaved={passwordSaved} onCancel={() => (ownerOpen = false)} />
	{/if}
{/snippet}
