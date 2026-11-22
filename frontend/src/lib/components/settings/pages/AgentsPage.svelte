<script lang="ts">
	/**
	 * Agent behavior: how every agent on this worker behaves and what it can
	 * reach. Instructions (the chat instructions sent to every agent) and
	 * Computer access (agents run as you in the desktop app, computer use). These rows were on
	 * Security until the Settings reorganization.
	 *
	 * The chat instructions are a worker setting; they keep an explicit Save
	 * and survive a change made elsewhere (banner + Load latest). Computer use
	 * follows the shared computerUse store, polled only while this page is
	 * mounted.
	 */
	import { onMount } from 'svelte';
	import { api, getErrorMessage, isApiError } from '$lib/api/client';
	import { computerUse } from '$lib/stores/computerUse.svelte';
	import { desktop } from '$lib/services/desktop';
	import { watchResources } from '$lib/services/watch-resources';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import { FORBIDDEN_TEXT, isForbidden } from '$lib/settings/access.svelte';
	import PreferencePage from '../shell/PreferencePage.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import Switch from '../rows/Switch.svelte';
	import ValueText from '../rows/ValueText.svelte';
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

	/* ---------- agents run as you (desktop app only) ---------- */

	const agentAccess = desktop?.agentAccess ?? null;

	/* ---------- computer use ---------- */

	const UNAVAILABLE: Record<string, { hint: string; help: string }> = {
		docker: {
			hint: 'Unavailable in Docker',
			help: 'AI Shuffle is running in Docker, which cannot control the host desktop. Run it natively to use this.'
		},
		'no-display': {
			hint: 'No desktop on this host',
			help: 'No desktop display detected on this host. On Linux, start AI Shuffle from an X11 desktop session.'
		},
		wayland: {
			hint: 'Wayland is not supported',
			help: 'Computer use needs an X11 session on Linux. Log in with an X11 session (for example GNOME on Xorg) and restart AI Shuffle.'
		},
		'no-monitors': { hint: 'No monitors detected', help: 'No monitors detected on this host.' },
		'missing-deps': {
			hint: 'Python packages missing',
			help: 'The computer-use Python packages are not installed. Run pip install -r requirements-native.txt.'
		},
		'missing-system-packages': {
			hint: 'System libraries missing',
			help: 'Screen-capture libraries are missing on this host.'
		},
		'x11-unreachable': {
			hint: 'Cannot reach the X display',
			help: 'AI Shuffle could not connect to the X display. Check DISPLAY and XAUTHORITY.'
		},
		'x11-no-xtest': {
			hint: 'X server lacks XTEST',
			help: 'The X server has no XTEST extension, which mouse and keyboard control needs.'
		},
		'env-disabled': {
			hint: 'Turned off by environment',
			help: 'Disabled by the COMPUTER_USE_DISABLED environment variable.'
		},
		'screenshot-probe-failed': {
			hint: 'Screen capture check failed',
			help: 'The screen-capture probe failed on this host.'
		}
	};

	let computerBusy = $state(false);
	const computerOff = $derived(UNAVAILABLE[computerUse.reason] ?? {
		hint: 'Unavailable on this host',
		help: `Unavailable (${computerUse.reason}).`
	});
	const computerHint = $derived(
		!computerUse.available
			? computerOff.hint
			: computerUse.active
				? `Controlling ${computerUse.displayLabel ?? 'your screen'}`
				: computerUse.enabled
					? 'On for every chat'
					: undefined
	);

	async function setComputerUse(on: boolean) {
		if (computerBusy || !computerUse.available) return;
		computerBusy = true;
		try {
			await computerUse.setEnabled(on);
			if (computerUse.enabled === on) flashSaved();
		} finally {
			computerBusy = false;
		}
	}

	async function setDedicatedDesktop(on: boolean) {
		if (computerBusy || !computerUse.available) return;
		const mode = on ? 'dedicated_desktop' : 'shared_desktop';
		computerBusy = true;
		try {
			await computerUse.setInputMode(mode);
			if (computerUse.inputMode === mode) flashSaved();
		} finally {
			computerBusy = false;
		}
	}

	$effect(() => {
		computerUse.start();
		return () => computerUse.stop();
	});

	/* ---------- lifecycle ---------- */

	onMount(() => {
		void loadInstructions();
	});
	watchResources(['settings'], () => void loadInstructions(true));
</script>

<PreferencePage category="agents">
	<RowGroup title="Instructions">
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

	<RowGroup title="Computer access">
		{#if agentAccess}
			<SettingRow id="agents.runsAs">
				<ValueText value="Your user account" />
			</SettingRow>
		{/if}
		<SettingRow
			id="agents.computerUse"
			hint={computerHint}
			help={computerUse.available ? undefined : (computerUse.detail ?? computerOff.help)}
			dim={!computerUse.available}
			value={computerUse.available ? computerUse.enabled : undefined}
			onReset={() => void setComputerUse(false)}
		>
			<!-- A saved "on" cannot run on a host without computer use: show it off. -->
			<Switch
				checked={computerUse.available && computerUse.enabled}
				disabled={!computerUse.available || computerBusy}
				onchange={(on) => void setComputerUse(on)}
			/>
		</SettingRow>
		<SettingRow
			id="agents.computerUseDedicated"
			dim={!computerUse.available}
			value={computerUse.available ? computerUse.inputMode === 'dedicated_desktop' : undefined}
			onReset={() => void setDedicatedDesktop(false)}
		>
			<Switch
				checked={computerUse.inputMode === 'dedicated_desktop'}
				disabled={!computerUse.available || computerBusy}
				onchange={(on) => void setDedicatedDesktop(on)}
			/>
		</SettingRow>
	</RowGroup>
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
