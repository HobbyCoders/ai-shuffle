<script lang="ts">
	/**
	 * OwnerPasswordForm: set or change the owner password, opened under
	 * Security > Owner password. A secret, so it keeps an explicit Save.
	 * The worker's minimum length and the repeat are checked before the call;
	 * a rejected save keeps what was typed.
	 */
	import { getErrorMessage } from '$lib/api/client';
	import { changeOwnerPassword, createOwnerPassword, type OwnerStatus } from '$lib/api/owner';
	import SettingRow from '../../rows/SettingRow.svelte';
	import TextField from '../../rows/TextField.svelte';

	interface Props {
		status: OwnerStatus;
		/** After a save: reload the status, then report what happened. */
		onSaved: (outcome: 'changed' | 'set') => Promise<void> | void;
		onCancel: () => void;
	}

	let { status, onSaved, onCancel }: Props = $props();

	let current = $state('');
	let next = $state('');
	let repeat = $state('');
	let busy = $state(false);
	let error = $state('');

	async function submit(event: SubmitEvent) {
		event.preventDefault();
		if (busy) return;
		error = '';
		if (status.password_set && !current) {
			error = 'Enter the current password.';
			return;
		}
		if (next.length < status.min_length) {
			error = `Use at least ${status.min_length} characters.`;
			return;
		}
		if (next !== repeat) {
			error = 'The two new passwords are different.';
			return;
		}
		busy = true;
		try {
			const outcome = status.password_set ? 'changed' : 'set';
			if (status.password_set) await changeOwnerPassword(current, next);
			else await createOwnerPassword(next);
			current = '';
			next = '';
			repeat = '';
			await onSaved(outcome);
		} catch (e: unknown) {
			error = getErrorMessage(e, 'The password could not be saved.');
		} finally {
			busy = false;
		}
	}
</script>

<form onsubmit={submit} aria-label="Owner password">
	<div class="st-xlist">
		{#if status.password_set}
			<SettingRow label="Current password">
				<TextField type="password" autocomplete="current-password" bind:value={current} disabled={busy} />
			</SettingRow>
		{/if}
		<SettingRow label="New password" hint="At least {status.min_length} characters">
			<TextField type="password" autocomplete="new-password" bind:value={next} disabled={busy} />
		</SettingRow>
		<SettingRow label="Repeat new password">
			<TextField type="password" autocomplete="new-password" bind:value={repeat} disabled={busy} />
		</SettingRow>
	</div>
	{#if error}<p class="st-msg err" role="alert">{error}</p>{/if}
	<div class="st-acts">
		<span class="st-left st-note">Linked devices use device sign-in</span>
		<button type="button" class="st-btn ghost" disabled={busy} onclick={onCancel}>Cancel</button>
		<button type="submit" class="st-btn primary" disabled={busy}>{busy ? 'Saving…' : 'Save password'}</button>
	</div>
</form>

<style>
	.st-acts > .st-note {
		margin: 0;
	}
</style>
