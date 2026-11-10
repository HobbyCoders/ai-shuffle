<script lang="ts">
	/**
	 * Security: Sign-in (owner password, trusted LAN) and Agent safety (agents
	 * run as you, computer use, chat instructions).
	 *
	 * Owner access and the chat instructions are worker settings; the
	 * instructions keep an explicit Save and survive a change made elsewhere
	 * (banner + Load latest). Computer use follows the shared computerUse
	 * store, polled only while this page is mounted.
	 */
	import { onMount, tick } from 'svelte';
	import { api, getErrorMessage, isApiError } from '$lib/api/client';
	import { terminalsApi } from '$lib/api/terminals';
	import { fetchOwnerStatus, setTrustedLan, type OwnerStatus, type OwnerStatusFailure } from '$lib/api/owner';
	import { ownerAccess } from '$lib/stores/owner-access.svelte';
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
	import PeerDeviceLinks from '$lib/components/terminal/PeerDeviceLinks.svelte';
	import ExpandToggle from '../rows/ExpandToggle.svelte';
	import InlineConfirm from '../rows/InlineConfirm.svelte';
	import OwnerPasswordForm from './editors/OwnerPasswordForm.svelte';
	import SecurityInstructionsEditor from './editors/SecurityInstructionsEditor.svelte';
	import './editors/editors.css';

	/** A failed load's one-line hint: the HTTP status, or that the worker did not answer. */
	function couldNotLoad(code: number | null | undefined): string {
		return code ? `Could not load (error ${code})` : 'Could not load (no answer)';
	}

	/** The same failure in full, for a screen reader's alert. */
	function couldNotLoadSentence(what: string, code: number | null | undefined): string {
		return `Could not load ${what}. ${code ? `The worker answered with an error (status ${code}).` : 'The worker did not answer.'}`;
	}

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

	/** Try again, in either failed row, reloads every load that failed. */
	async function retryFailed(event: MouseEvent) {
		if (retrying) return;
		const button = event.currentTarget as HTMLElement;
		const page = button.closest('.st-page');
		// The button stays focused (aria-disabled, not disabled) while it runs.
		// A Try again goes away when its row loads or is refused, maybe before
		// the other load ends: follow focus on it (and its row) to the end, so it
		// lands on that row's value, else on the Try again that is left.
		let held: HTMLElement | null = document.activeElement === button ? button : null;
		let row = button.closest('.st-row');
		const keepFocus = async () => {
			await tick();
			const active = document.activeElement;
			if (active instanceof HTMLElement && active.matches('[data-retry-load]')) {
				held = active;
				row = active.closest('.st-row');
				return;
			}
			// Only when focus went with the button, not if the reader moved on.
			if (!held || held.isConnected || (active && active !== document.body)) {
				held = null;
				return;
			}
			held =
				row?.querySelector<HTMLElement>('.st-vbtn:not(:disabled)') ??
				row?.querySelector<HTMLElement>('button:not(:disabled)') ??
				page?.querySelector<HTMLElement>('[data-retry-load]') ??
				null;
			row = held?.closest('.st-row') ?? null;
			held?.focus();
		};
		retrying = true;
		try {
			await Promise.all([
				ownerRetryable ? loadOwner().then(keepFocus) : null,
				instrLoad === 'error' ? loadInstructions().then(keepFocus) : null
			]);
		} finally {
			retrying = false;
		}
		await keepFocus();
	}

	/* ---------- lifecycle ---------- */

	onMount(() => {
		void loadOwner();
		void loadInstructions();
		void loadPeerTerminals();
	});
	watchResources(['settings'], () => { void loadInstructions(true); void loadPeerTerminals(); });
</script>

{#snippet peerDevices()}<PeerDeviceLinks workerName={workerName} full />{/snippet}

<!-- Beside a failed row's value; it retries every failed load on the page. -->
{#snippet retryButton(errorId: string)}
	<button type="button" class="st-btn sm" data-retry-load aria-disabled={retrying || undefined} aria-describedby={retrying ? undefined : errorId} onclick={retryFailed}>
		{retrying ? 'Retrying…' : 'Try again'}
	</button>
{/snippet}

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
				{@render retryButton(ownerErrorId)}
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

	<RowGroup title="Agent safety">
		{#if agentAccess}
			<SettingRow id="security.runsAs">
				<ValueText value="Your user account" />
			</SettingRow>
		{/if}
		<SettingRow
			id="security.computerUse"
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
			id="security.computerUseDedicated"
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
		<SettingRow
			id="security.secInstr"
			hint={instrLoad === 'error' ? couldNotLoad(instrLoadCode) : undefined}
			expanded={instrOpen && instrLoad === 'ok' ? instrEditor : undefined}
		>
			{#if instrLoad === 'ok'}
				<ExpandToggle value={instrValue} bind:open={instrOpen} />
			{:else}
				<!-- Holds the loaded row's shape (#1301). -->
				<ExpandToggle value={instrValue} open={false} pending={instrLoad === 'loading'} disabled={instrLoad !== 'loading'} />
				{#if instrLoad === 'error'}{@render retryButton(instrErrorId)}{/if}
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
