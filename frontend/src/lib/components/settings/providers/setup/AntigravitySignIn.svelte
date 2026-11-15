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
		getAntigravityLogin,
		startAntigravityLogin,
		type AntigravityLogin
	} from '$lib/api/antigravity';
	import RowGroup from '../../rows/RowGroup.svelte';
	import SettingRow from '../../rows/SettingRow.svelte';
	import TextField from '../../rows/TextField.svelte';

	interface Props {
		rowId: string;
		/** Start label ("Start sign-in"). */
		startLabel?: string;
		/** A sign-in already waiting for its code (started on another device
		 * or tab): shown with its code entry instead of a start that would
		 * replace it. */
		pending?: AntigravityLogin | null;
		/** ``note``: why nothing was started (already signed in). */
		onSignedIn: (note?: string) => void;
		onCancel?: () => void;
		/** The sign-in ended elsewhere (another tab finished, cancelled or let it expire). */
		onEnded?: () => void;
	}

	let { rowId, startLabel = 'Start sign-in', pending = null, onSignedIn, onCancel, onEnded }: Props = $props();
	/** How often a waiting panel reads the sign-in's state (a memory read on the worker). */
	const WATCH_MS = 3000;

	let login = $state<AntigravityLogin | null>(null);
	let code = $state('');
	let busy = $state(false);
	let error = $state('');
	let done = false;
	// Only a sign-in this screen started ends when the screen goes away; one
	// adopted from elsewhere is ended only by its Cancel.
	let owned = $state(false);
	let dismissed = '';

	$effect(() => {
		const waiting = pending;
		if (busy || login || !waiting?.login_id || waiting.state !== 'awaiting_code') return;
		if (waiting.login_id === dismissed) return;
		login = waiting;
		owned = false;
		error = '';
	});

	async function start() {
		if (busy) return;
		busy = true;
		error = '';
		try {
			const started = await startAntigravityLogin(rowId);
			if (started.state === 'signed_in') {
				// Already signed in on this worker: nothing was started.
				login = null;
				done = true;
				onSignedIn(started.message ?? undefined);
				return;
			}
			login = started;
			owned = true;
			if (started.state !== 'awaiting_code') error = started.message || 'Antigravity could not start the sign-in.';
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
				dismissed = login.login_id;
				login = null;
				onSignedIn();
				return;
			}
			dismissed = login.login_id;
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
		if (id) dismissed = id;
		login = null;
		code = '';
		error = '';
		if (id) void cancelAntigravityLogin(rowId, id).catch(() => {});
		onCancel?.();
	}

	// A sign-in completed, cancelled or expired in another tab must not leave
	// this panel waiting for a code (#1533). Focus alone missed it: poll the
	// cheap state read while the code entry shows, never the agent probe.
	$effect(() => {
		const id = login?.state === 'awaiting_code' ? login.login_id : null;
		if (!id) return;
		const timer = setInterval(async () => {
			if (busy) return;
			let answer;
			try {
				answer = await getAntigravityLogin(rowId, id);
			} catch {
				return; // a dropped read says nothing; the next one may
			}
			// Our own Finish or Cancel since owns the panel.
			if (busy || login?.login_id !== id || login.state !== 'awaiting_code') return;
			if (answer.state === 'awaiting_code' || answer.state === 'completing') return;
			dismissed = id;
			login = null;
			owned = false;
			code = '';
			error = answer.message || 'This sign-in ended in another tab or expired. Start the sign-in again if you still need it.';
			onEnded?.();
		}, WATCH_MS);
		return () => clearInterval(timer);
	});

	// Leaving mid-sign-in ends the agent's sign-in process on the worker.
	onDestroy(() => {
		if (!done && owned && login?.state === 'awaiting_code' && login.login_id) {
			void cancelAntigravityLogin(rowId, login.login_id).catch(() => {});
		}
	});

	const awaiting = $derived(login?.state === 'awaiting_code' && !!login.url);
</script>

{#if awaiting}
	{#if !owned}<p class="pv-note">A sign-in started on another device or tab is waiting for its code. Finish it here, or cancel it to start again.</p>{/if}
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
