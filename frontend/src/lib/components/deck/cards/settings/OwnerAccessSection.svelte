<script lang="ts">
	/**
	 * Settings → Owner access: change the owner password and the
	 * "trusted LAN, no login" toggle. Owner only.
	 */
	import { onMount } from 'svelte';
	import { KeyRound } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { changeOwnerPassword, createOwnerPassword, fetchOwnerStatus, setTrustedLan, type OwnerStatus, type OwnerStatusFailure } from '$lib/api/owner';
	import { ownerAccess } from '$lib/stores/owner-access.svelte';

	let status = $state<OwnerStatus | null>(null);
	/** Why the first load failed; a later reload failure keeps the last status. */
	let loadFailure = $state<OwnerStatusFailure | null>(null);
	let loading = $state(false);
	let current = $state('');
	let next = $state('');
	let repeat = $state('');
	let busy = $state(false);
	let error = $state('');
	let notice = $state('');

	async function load() {
		loading = true;
		try {
			const result = await fetchOwnerStatus();
			if (result.ok) {
				status = result.status;
				loadFailure = null;
			} else if (!status) {
				loadFailure = result;
			}
		} finally {
			loading = false;
		}
	}

	async function savePassword(event: SubmitEvent) {
		event.preventDefault();
		if (busy || !status) return;
		error = ''; notice = '';
		if (next.length < status.min_length) { error = `Use at least ${status.min_length} characters.`; return; }
		if (next !== repeat) { error = 'The two new passwords are different.'; return; }
		busy = true;
		try {
			if (status.password_set) await changeOwnerPassword(current, next);
			else await createOwnerPassword(next);
			notice = status.password_set
				? 'Password changed. Every other browser was signed out.'
				: 'Owner password set.';
			current = ''; next = ''; repeat = '';
			await load();
			await ownerAccess.refresh();
		} catch (e: unknown) {
			error = getErrorMessage(e, 'The password could not be saved.');
		} finally {
			busy = false;
		}
	}

	async function toggleLan(box: HTMLInputElement) {
		const enabled = box.checked;
		if (busy) { box.checked = !enabled; return; }
		if (enabled && !window.confirm('Trust every device on your network? Any browser that can reach this address becomes the owner without a password: guests on your Wi-Fi, other people on your tailnet, and any compromised device. Leave this off unless your network is only yours.')) { box.checked = false; return; }
		busy = true; error = ''; notice = '';
		try {
			await setTrustedLan(enabled);
			await load();
			await ownerAccess.refresh();
		} catch (e: unknown) {
			box.checked = !enabled;
			error = getErrorMessage(e, 'The setting could not be saved.');
		} finally {
			busy = false;
		}
	}

	onMount(() => { void load(); });
</script>

<div class="set-group">
	<div class="set-grouphead"><h3><KeyRound />Owner access</h3></div>
	{#if !status && loadFailure?.reason === 'unsupported'}
		<div class="set-row stack" role="alert">
			<p class="hint">This worker does not support owner passwords. Update the worker to manage owner access here.</p>
		</div>
	{:else if !status && loadFailure}
		<div class="set-row stack">
			<p class="msg err" role="alert">Could not load owner access status. {loadFailure.reason === 'error' ? `The worker answered with an error (status ${loadFailure.code}).` : 'The worker did not answer.'}</p>
			<div><button type="button" class="ed-btn" disabled={loading} onclick={() => void load()}>{loading ? 'Retrying…' : 'Try again'}</button></div>
		</div>
	{:else if !status}
		<div class="set-row"><p class="hint">Loading…</p></div>
	{:else}
		<form class="set-row stack" onsubmit={savePassword} aria-label="Owner password">
			<div class="lab">
				<h4>{status.password_set ? 'Change the owner password' : 'Set an owner password'}</h4>
				<p class="hint">Browsers sign in with it once per device. Phones and browsers linked through your HobbyCoders account use their own device sign-in.</p>
			</div>
			<div class="fields">
				{#if status.password_set}
					<input class="inp" type="password" aria-label="Current password" placeholder="Current password" autocomplete="current-password" bind:value={current} required />
				{/if}
				<input class="inp" type="password" aria-label="New password" placeholder={`New password (at least ${status.min_length} characters)`} autocomplete="new-password" bind:value={next} required />
				<input class="inp" type="password" aria-label="Repeat the new password" placeholder="Repeat the new password" autocomplete="new-password" bind:value={repeat} required />
				<button type="submit" class="ed-btn primary" disabled={busy}>{busy ? '…' : 'Save password'}</button>
			</div>
		</form>
		<div class="set-row stack">
			<label class="lan">
				<input type="checkbox" checked={status.trusted_lan} disabled={busy} onchange={(e) => toggleLan(e.currentTarget)} />
				<span>Trusted LAN, no login</span>
			</label>
			<p class="hint">Off by default. When on, every device on your local network or tailnet is the owner without a password.</p>
		</div>
	{/if}
	{#if notice}<p class="msg ok row-pad" role="status">{notice}</p>{/if}
	{#if error}<p class="msg err row-pad" role="alert">{error}</p>{/if}
</div>

<style>
	.hint { margin: 0; color: var(--muted-foreground); font-size: 12px; line-height: 1.5; }
	.fields { display: grid; gap: 8px; max-width: 420px; }
	.lan { display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 600; }
	.row-pad { padding: 4px 14px 10px; }
	.msg { margin: 0; font-size: 12.5px; }
	.msg.ok { color: var(--success, #16a34a); }
	.msg.err { color: var(--destructive); }
</style>
