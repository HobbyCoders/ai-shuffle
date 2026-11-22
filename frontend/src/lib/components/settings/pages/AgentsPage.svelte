<script lang="ts">
	/**
	 * Agent instructions: the chat instructions sent to every agent on this
	 * worker (the default block of every system prompt). This row was on
	 * Security until the Settings reorganization.
	 *
	 * The chat instructions are a worker setting; they keep an explicit Save
	 * and survive a change made elsewhere (banner + Load latest).
	 */
	import { onMount } from 'svelte';
	import { api, getErrorMessage, isApiError } from '$lib/api/client';
	import { watchResources } from '$lib/services/watch-resources';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import { FORBIDDEN_TEXT, isForbidden } from '$lib/settings/access.svelte';
	import PreferencePage from '../shell/PreferencePage.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import ExpandToggle from '../rows/ExpandToggle.svelte';
	import SecurityInstructionsEditor from './editors/SecurityInstructionsEditor.svelte';
	import { couldNotLoad, couldNotLoadSentence, handOffRetryFocus } from './load-failure';
	import './editors/editors.css';

	const uid = $props.id();

	/* ---------- chat instructions ---------- */

	// Formerly "security instructions". The worker also answers
	// /settings/chat-instructions, but this UI keeps the old path so it still
	// works against workers that predate the rename.

	interface Instructions {
		current: string;
		default: string;
		is_customized: boolean;
	}

	let instrText = $state('');
	let instrSaved = $state('');
	let instrDefault = $state('');
	let instrCustomized = $state(false);
	let instrLoad = $state<'loading' | 'ok' | 'forbidden' | 'error'>('loading');
	/** The failed first load's HTTP status (null: no answer), for the row's hint. */
	let instrLoadCode = $state<number | null>(null);
	let instrBusy = $state(false);
	/** A failed save or revert (a failed load is `instrLoad`, so a later read or a 403 ends it). */
	let instrError = $state('');
	const instrErrorId = `${uid}-instr-error`;
	let instrOpen = $state(false);
	/** Newest saved text from the worker while local edits were unsaved. */
	let instrRemote = $state<Instructions | null>(null);

	function applyInstructions(value: Instructions) {
		instrText = value.current;
		instrSaved = value.current;
		instrDefault = value.default;
		instrCustomized = value.is_customized;
		instrRemote = null;
	}

	async function loadInstructions(quiet = false) {
		const clean = !instrBusy && instrText === instrSaved;
		try {
			const value = await api.get<Instructions>('/settings/security-instructions');
			instrLoad = 'ok';
			if (quiet && (!clean || instrBusy || instrText !== instrSaved)) {
				if (value.current !== instrSaved) instrRemote = value;
				return;
			}
			applyInstructions(value);
		} catch (e: unknown) {
			if (isForbidden(e)) instrLoad = 'forbidden';
			else if (!quiet) {
				instrLoad = 'error';
				instrLoadCode = isApiError(e) ? e.status : null;
			}
		}
	}

	async function saveInstructions() {
		if (!instrText.trim()) {
			instrError = 'Instructions cannot be empty. Use Revert to restore defaults.';
			return;
		}
		instrBusy = true;
		instrError = '';
		try {
			await api.put('/settings/security-instructions', { instructions: instrText });
			instrSaved = instrText;
			instrCustomized = true;
			instrRemote = null;
			flashSaved();
		} catch (e: unknown) {
			instrError = getErrorMessage(e, 'Could not save the chat instructions.');
		} finally {
			instrBusy = false;
		}
	}

	async function revertInstructions() {
		instrBusy = true;
		instrError = '';
		try {
			await api.delete('/settings/security-instructions');
			instrText = instrDefault;
			instrSaved = instrDefault;
			instrCustomized = false;
			instrRemote = null;
			flashSaved();
		} catch (e: unknown) {
			instrError = getErrorMessage(e, 'Could not revert the chat instructions.');
		} finally {
			instrBusy = false;
		}
	}

	const instrValue = $derived(
		instrLoad === 'ok'
			? instrCustomized
				? 'Customized'
				: 'Default'
			: instrLoad === 'forbidden'
				? FORBIDDEN_TEXT
				: instrLoad === 'error'
					? 'Unavailable'
					: 'Loading…'
	);

	/* ---------- retrying a failed load ---------- */

	let retrying = $state(false);

	async function retryInstructions() {
		if (retrying) return;
		retrying = true;
		try {
			await loadInstructions();
		} finally {
			retrying = false;
		}
	}

	$effect.pre(() => {
		void instrLoad;
		handOffRetryFocus();
	});

	/* ---------- lifecycle ---------- */

	onMount(() => {
		void loadInstructions();
	});
	watchResources(['settings'], () => void loadInstructions(true));
</script>

<PreferencePage category="agents">
	<RowGroup title="Every chat">
		<SettingRow
			id="agents.instructions"
			hint={instrLoad === 'error' ? couldNotLoad(instrLoadCode) : undefined}
			expanded={instrOpen && instrLoad === 'ok' ? instrEditor : undefined}
		>
			{#if instrLoad === 'ok'}
				<ExpandToggle value={instrValue} bind:open={instrOpen} />
			{:else}
				<!-- Holds the loaded row's shape (#1301). -->
				<ExpandToggle value={instrValue} open={false} pending={instrLoad === 'loading'} disabled={instrLoad !== 'loading'} />
				{#if instrLoad === 'error'}
					<button type="button" class="st-btn sm" data-retry-load aria-disabled={retrying || undefined} aria-describedby={retrying ? undefined : instrErrorId} onclick={retryInstructions}>
						{retrying ? 'Retrying…' : 'Try again'}
					</button>
				{/if}
			{/if}
		</SettingRow>
	</RowGroup>
	<!-- A failed load is the row's hint (#1403); a failed save or revert, after
	     the reader acted, keeps its visible line. -->
	{#if instrLoad === 'error'}
		{#if !retrying}
			<p class="st-vh" role="alert" id={instrErrorId}>{couldNotLoadSentence('the chat instructions', instrLoadCode)}</p>
		{/if}
	{:else if instrError && !(instrOpen && instrLoad === 'ok')}
		<p class="st-msg err" role="alert">{instrError}</p>
	{/if}
</PreferencePage>

{#snippet instrEditor()}
	<SecurityInstructionsEditor
		bind:value={instrText}
		saved={instrSaved}
		defaultText={instrDefault}
		customized={instrCustomized}
		busy={instrBusy}
		changedElsewhere={!!instrRemote}
		error={instrError}
		onSave={saveInstructions}
		onRevert={revertInstructions}
		onLoadLatest={() => instrRemote && applyInstructions(instrRemote)}
	/>
{/snippet}
