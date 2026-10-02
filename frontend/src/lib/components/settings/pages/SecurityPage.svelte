<script lang="ts">
	/**
	 * Security: Sign-in (owner password, trusted LAN) and Agent safety (agents
	 * run as you, computer use, code sandbox, security instructions).
	 *
	 * Owner access and the security instructions are worker settings; the
	 * instructions keep an explicit Save and survive a change made elsewhere
	 * (banner + Load latest). Computer use follows the shared computerUse
	 * store, polled only while this page is mounted.
	 */
	import { onMount } from 'svelte';
	import { BookOpen } from 'lucide-svelte';
	import { api, getErrorMessage, type CodeSandboxStatus } from '$lib/api/client';
	import { fetchOwnerStatus, setTrustedLan, type OwnerStatus, type OwnerStatusFailure } from '$lib/api/owner';
	import { ownerAccess } from '$lib/stores/owner-access.svelte';
	import { computerUse } from '$lib/stores/computerUse.svelte';
	import { layout } from '$lib/stores/layout.svelte';
	import { desktop } from '$lib/services/desktop';
	import { watchResources } from '$lib/services/watch-resources';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import { FORBIDDEN_TEXT, isForbidden, loadState } from '$lib/settings/access.svelte';
	import { getEntry } from '$lib/settings/registry';
	import type { StatusTone } from '$lib/settings/types';
	import PreferencePage from '../shell/PreferencePage.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import Switch from '../rows/Switch.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import ExpandToggle from '../rows/ExpandToggle.svelte';
	import InlineConfirm from '../rows/InlineConfirm.svelte';
	import OwnerPasswordForm from './editors/OwnerPasswordForm.svelte';
	import SecurityInstructionsEditor from './editors/SecurityInstructionsEditor.svelte';
	import './editors/editors.css';

	/* ---------- owner access ---------- */

	let owner = $state<OwnerStatus | null>(null);
	/** Why the first load failed; a later reload failure keeps the last status. */
	let ownerFailure = $state<OwnerStatusFailure | null>(null);
	let ownerLoading = $state(false);
	let ownerOpen = $state(false);
	let ownerOutcome = $state<'changed' | 'set' | null>(null);
	let lanBusy = $state(false);
	let signInError = $state('');

	const ownerHint = $derived(
		ownerOutcome === 'changed'
			? 'Other browsers signed out'
			: ownerOutcome === 'set'
				? 'Password set'
				: owner
					? owner.password_set
						? 'Asked once per browser'
						: 'Not set yet'
					: undefined
	);

	async function loadOwner() {
		ownerLoading = true;
		try {
			const result = await fetchOwnerStatus();
			if (result.ok) {
				owner = result.status;
				ownerFailure = null;
			} else if (!owner) {
				ownerFailure = result;
			}
		} finally {
			ownerLoading = false;
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

	/* ---------- agents run as you (desktop app only) ---------- */

	const agentAccess = desktop?.agentAccess ?? null;

	/* ---------- computer use ---------- */

	const UNAVAILABLE: Record<string, { hint: string; help: string }> = {
		docker: {
			hint: 'Unavailable in Docker',
			help: 'AI Shuffle is running in Docker, which cannot control the host desktop. Run it natively to use this.'
		},
		'no-display': { hint: 'No display on this host', help: 'No desktop display detected on this host.' },
		'no-monitors': { hint: 'No monitors detected', help: 'No monitors detected on this host.' },
		'missing-deps': { hint: 'mss or pynput missing', help: 'The mss/pynput packages are not installed on the server.' },
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

	$effect(() => {
		computerUse.start();
		return () => computerUse.stop();
	});

	/* ---------- code sandbox ---------- */

	// The install guide's section "The code-mode sandbox".
	const SANDBOX_DOCS = 'getting-started/install-docker-compose';
	let sandbox = $state<CodeSandboxStatus | null>(null);
	let sandboxForbidden = $state(false);

	const sandboxView = $derived.by((): { text: string; tone: StatusTone } => {
		if (sandboxForbidden) return { text: FORBIDDEN_TEXT, tone: 'off' };
		if (!sandbox || sandbox.state === 'checking') return { text: 'Checking', tone: 'busy' };
		if (sandbox.active) return { text: 'Active', tone: 'ok' };
		if (sandbox.state === 'off') return { text: 'Off', tone: 'off' };
		return { text: 'Unavailable', tone: 'warn' };
	});
	const sandboxHelp = $derived(
		sandbox && !sandbox.active && sandbox.state !== 'checking' && sandbox.reason
			? `${sandbox.reason}. ${getEntry('security.sandbox')?.help ?? ''}`
			: undefined
	);

	async function loadSandbox() {
		const result = await loadState(() => api.get<{ code_sandbox?: CodeSandboxStatus }>('/deployment'));
		if (result.status === 'ok') sandbox = result.data?.code_sandbox ?? null;
		else if (result.status === 'forbidden') sandboxForbidden = true;
	}

	/* ---------- security instructions ---------- */

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
	let instrBusy = $state(false);
	let instrError = $state('');
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
				instrError = getErrorMessage(e, 'Could not load the security instructions.');
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
			instrError = getErrorMessage(e, 'Could not save the security instructions.');
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
			instrError = getErrorMessage(e, 'Could not revert the security instructions.');
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

	/* ---------- lifecycle ---------- */

	onMount(() => {
		void loadOwner();
		void loadSandbox();
		void loadInstructions();
	});
	watchResources(['settings'], () => loadInstructions(true));
</script>

<PreferencePage category="security">
	<RowGroup title="Sign-in">
		<SettingRow id="security.ownerPassword" hint={ownerHint} expanded={ownerOpen && owner ? ownerForm : undefined}>
			{#if owner}
				<ExpandToggle value={owner.password_set ? 'Set' : 'Not set'} bind:open={ownerOpen} />
			{:else if ownerFailure?.reason === 'unsupported'}
				<ValueText value="Not supported" />
			{:else if ownerFailure?.reason === 'error' && ownerFailure.code === 403}
				<ValueText value={FORBIDDEN_TEXT} />
			{:else if ownerFailure}
				<ValueText value="Could not load" />
				<button type="button" class="st-btn sm" disabled={ownerLoading} onclick={() => void loadOwner()}>
					{ownerLoading ? 'Retrying…' : 'Try again'}
				</button>
			{:else}
				<ValueText value="Loading…" />
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
	{#if ownerFailure && !owner && ownerFailure.reason !== 'unsupported' && !(ownerFailure.reason === 'error' && ownerFailure.code === 403)}
		<p class="st-msg err" role="alert">
			Could not load owner access status. {ownerFailure.reason === 'error'
				? `The worker answered with an error (status ${ownerFailure.code}).`
				: 'The worker did not answer.'}
		</p>
	{:else if ownerFailure?.reason === 'unsupported' && !owner}
		<p class="st-msg" role="status">This worker does not support owner passwords. Update the worker to manage them here.</p>
	{/if}
	{#if signInError}<p class="st-msg err" role="alert">{signInError}</p>{/if}
	{#if ownerOutcome}
		<p class="st-vh" role="status">
			{ownerOutcome === 'changed' ? 'Password changed. Every other browser was signed out.' : 'Owner password set.'}
		</p>
	{/if}

	<RowGroup title="Agent safety">
		{#if agentAccess}
			<SettingRow id="security.runsAs">
				<ValueText value="Your user account" />
			</SettingRow>
		{/if}
		<SettingRow
			id="security.computerUse"
			hint={computerHint}
			help={computerUse.available ? undefined : computerOff.help}
			dim={!computerUse.available}
			value={computerUse.available ? computerUse.enabled : undefined}
			onReset={() => void setComputerUse(false)}
		>
			<Switch
				checked={computerUse.enabled}
				disabled={!computerUse.available || computerBusy}
				onchange={(on) => void setComputerUse(on)}
			/>
		</SettingRow>
		<SettingRow id="security.sandbox" hint="bubblewrap" hintMono help={sandboxHelp}>
			<button
				type="button"
				class="st-btn ghost icon sm"
				aria-label="Open the code sandbox guide"
				title="Code sandbox guide"
				onclick={() => layout.openFeaturePage('docs', SANDBOX_DOCS)}><BookOpen /></button
			>
			<ValueText value={sandboxView.text} status={sandboxView.tone} />
		</SettingRow>
		<SettingRow id="security.secInstr" expanded={instrOpen && instrLoad === 'ok' ? instrEditor : undefined}>
			{#if instrLoad === 'ok'}
				<ExpandToggle value={instrValue} bind:open={instrOpen} />
			{:else}
				<ValueText value={instrValue} />
			{/if}
		</SettingRow>
	</RowGroup>
	{#if instrError && !instrOpen}<p class="st-msg err" role="alert">{instrError}</p>{/if}
</PreferencePage>

{#snippet ownerForm()}
	{#if owner}
		<OwnerPasswordForm status={owner} onSaved={passwordSaved} onCancel={() => (ownerOpen = false)} />
	{/if}
{/snippet}

{#snippet instrEditor()}
	<SecurityInstructionsEditor
		bind:value={instrText}
		saved={instrSaved}
		customized={instrCustomized}
		busy={instrBusy}
		changedElsewhere={!!instrRemote}
		error={instrError}
		onSave={saveInstructions}
		onRevert={revertInstructions}
		onLoadLatest={() => instrRemote && applyInstructions(instrRemote)}
	/>
{/snippet}
