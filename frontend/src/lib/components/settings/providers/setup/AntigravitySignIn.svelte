<script lang="ts">
	/**
	 * Google sign-in through Antigravity's own flow. The worker runs the agent's
	 * sign-in, which prints a Google link; the owner opens it, signs in, and
	 * pastes the code Google shows back here. The agent keeps the sign-in in its
	 * own folder on the worker: AI Shuffle never sees or stores it.
	 */
	import { onDestroy } from 'svelte';
	import { getErrorMessage } from '$lib/api/client';
	import {
		cancelAntigravityLogin,
		completeAntigravityLogin,
		startAntigravityLogin,
		type AntigravityLogin
	} from '$lib/api/antigravity';
	import RowGroup from '../../rows/RowGroup.svelte';
	import SettingRow from '../../rows/SettingRow.svelte';
	import TextField from '../../rows/TextField.svelte';

	interface Props {
		rowId: string;
		/** Start label ("I understand, start sign-in" after the terms warning). */
		startLabel?: string;
		onSignedIn: () => void;
		onCancel?: () => void;
	}

	let { rowId, startLabel = 'Start sign-in', onSignedIn, onCancel }: Props = $props();

	let login = $state<AntigravityLogin | null>(null);
	let code = $state('');
	let busy = $state(false);
	let error = $state('');
	let done = false;

	async function start() {
		if (busy) return;
		busy = true;
		error = '';
		try {
			login = await startAntigravityLogin(rowId);
			if (login.state !== 'awaiting_code') error = login.message || 'Antigravity could not start the sign-in.';
		} catch (e) {
			error = getErrorMessage(e, 'Could not start the sign-in');
		} finally {
			busy = false;
		}
	}

	async function complete() {
		const value = code.trim();
		if (!value || busy || !login?.login_id) return;
		busy = true;
		error = '';
		try {
			const result = await completeAntigravityLogin(rowId, login.login_id, value);
			code = '';
			if (result.state === 'signed_in') {
				done = true;
				login = null;
				onSignedIn();
				return;
			}
			login = result;
			error = result.message || 'Google did not accept the code. Start the sign-in again.';
		} catch (e) {
			error = getErrorMessage(e, 'Could not finish the sign-in');
		} finally {
			busy = false;
		}
	}

	function cancel() {
		const id = login?.login_id;
		login = null;
		code = '';
		error = '';
		if (id) void cancelAntigravityLogin(rowId, id).catch(() => {});
		onCancel?.();
	}

	// Leaving mid-sign-in ends the agent's sign-in process on the worker.
	onDestroy(() => {
		if (!done && login?.state === 'awaiting_code' && login.login_id) {
			void cancelAntigravityLogin(rowId, login.login_id).catch(() => {});
		}
	});

	const awaiting = $derived(login?.state === 'awaiting_code' && !!login.url);
</script>

{#if awaiting}
	<RowGroup title="Sign in with Google" id="providers-antigravity-signin">
		<SettingRow label="1. Open Google sign-in" hint="Opens in a new tab">
			<a class="st-btn primary" href={login!.url} target="_blank" rel="noopener noreferrer">Open Google ↗</a>
		</SettingRow>
		<SettingRow label="2. Paste the code" hint="The code or the full page address Google shows">
			<TextField bind:value={code} mono width={260} disabled={busy} placeholder="Code from Google" label="Code from Google" />
		</SettingRow>
	</RowGroup>
	<p class="sr-only" role="status">Waiting for the code from Google</p>
{/if}
{#if error}<p class="pv-msg err" role="alert">{error}</p>{/if}
<div class="pv-acts">
	{#if onCancel || awaiting}
		<button type="button" class="st-btn ghost" disabled={busy && !awaiting} onclick={cancel}>Cancel</button>
	{/if}
	{#if awaiting}
		<button type="button" class="st-btn primary" disabled={busy || !code.trim()} onclick={complete}>{busy ? 'Checking' : 'Finish sign-in'}</button>
	{:else}
		<button type="button" class="st-btn primary" disabled={busy} onclick={start}>{busy ? 'Starting' : login ? 'Start again' : startLabel}</button>
	{/if}
</div>
