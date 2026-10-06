<script lang="ts">
	/**
	 * System: Workspace (folder, import from Claude Code and Codex), Updates
	 * (policy, check for updates, version), Memory (read-only health),
	 * License and Help (report a problem to HobbyCoders support).
	 *
	 * The workspace path saves when "Check path" finds it valid. A path edited
	 * here is never replaced by a change made elsewhere; a banner offers the
	 * newer one instead. The license key, like every secret, needs an explicit
	 * Activate.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { getWorkspaceConfig, setWorkspaceConfig, validateWorkspacePath } from '$lib/api/auth';
	import {
		activateLicense,
		deactivateLicense,
		getLicense,
		LICENSE_CHANGED_EVENT,
		planName,
		refreshLicense,
		releaseSeat,
		type LicenseState,
		type LicenseStatus,
		type SeatHolder
	} from '$lib/api/license';
	import { watchResources } from '$lib/services/watch-resources';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import { FORBIDDEN_TEXT, loadState } from '$lib/settings/access.svelte';
	import type { StatusTone } from '$lib/settings/types';
	import PreferencePage from '../shell/PreferencePage.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import Switch from '../rows/Switch.svelte';
	import PathField, { type PathCheck } from '../rows/PathField.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import StatusDot from '../rows/StatusDot.svelte';
	import ExpandToggle from '../rows/ExpandToggle.svelte';
	import InlineConfirm from '../rows/InlineConfirm.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import ImportWizard, { type ImportStep } from './editors/ImportWizard.svelte';
	import UpdateCheckResults from './editors/UpdateCheckResults.svelte';
	import MemoryHealthList from './editors/MemoryHealthList.svelte';
	import LicenseKeyForm from './editors/LicenseKeyForm.svelte';
	import LicenseSeats from './editors/LicenseSeats.svelte';
	import { UpdatesState } from './updates-state.svelte';
	import { deployRows, deploySummary, loadDeployStatus, type DeployReport } from './deploy-status';
	import { fetchMemoryHealth, memoryRows, memorySummary, type MemoryHealth } from './memory-health';
	import ReportProblemForm from './editors/ReportProblemForm.svelte';
	import { canSendSupportReport, openSupportReport, supportCaps } from '$lib/stores/support-report.svelte';
	import './editors/editors.css';

	/* ---------- workspace ---------- */

	let workspacePath = $state('');
	/** Path last loaded or saved; differs from the field while it is being edited. */
	let workspaceSaved = $state<string | null>(null);
	let workspaceLoad = $state<'loading' | 'ok' | 'forbidden' | 'error'>('loading');
	let workspaceRemote = $state<string | null>(null);

	async function loadWorkspace(quiet = false) {
		const clean = workspacePath === (workspaceSaved ?? '');
		const result = await loadState(getWorkspaceConfig);
		if (result.status === 'ok') {
			const fresh = result.data.workspace_path;
			workspaceLoad = 'ok';
			if (!quiet || clean) {
				workspacePath = fresh;
				workspaceSaved = fresh;
				workspaceRemote = null;
			} else if (fresh !== workspaceSaved) {
				workspaceRemote = fresh;
			}
		} else if (!quiet || workspaceSaved === null) {
			workspaceLoad = result.status;
		}
	}

	async function checkWorkspace(path: string): Promise<PathCheck> {
		const validation = await validateWorkspacePath(path);
		if (!validation.valid) return { ok: false, message: validation.error || 'Not found' };
		await setWorkspaceConfig(path);
		workspaceSaved = path;
		workspaceRemote = null;
		flashSaved();
		return { ok: true, message: 'Valid, saved' };
	}

	/* ---------- import ---------- */

	let importOpen = $state(false);
	let importStep = $state<ImportStep>('source');
	let imported = $state<number | null>(null);
	const importValue = $derived(
		importStep === 'preview' ? 'Choosing items' : imported !== null ? `Imported ${imported}` : 'Scan'
	);

	/* ---------- updates ---------- */

	const updates = new UpdatesState();
	let resultsOpen = $state(false);
	const allow = $derived(updates.policy?.allow_updates ?? true);
	const updatesLocked = $derived(!updates.policy || updates.saving);

	function runCheck() {
		resultsOpen = true;
		void updates.check();
	}

	/* ---------- running revisions (deploy status) ---------- */

	let deploy = $state<DeployReport | null>(null);
	let deployError = $state('');
	let deployOpen = $state(false);
	let deployLoading = $state(false);

	async function refreshDeploy() {
		deployLoading = true;
		const result = await loadDeployStatus();
		deployLoading = false;
		if (!alive) return;
		deploy = result.report;
		deployError = result.error;
	}

	/* ---------- memory health (read-only; the worker repairs by itself) ---------- */

	let memory = $state<MemoryHealth | null>(null);
	let memoryLoad = $state<'loading' | 'ok' | 'forbidden' | 'error'>('loading');
	let memoryError = $state('');
	let memoryOpen = $state(false);

	async function loadMemory() {
		const result = await loadState(fetchMemoryHealth);
		if (!alive) return;
		memoryLoad = result.status;
		if (result.status === 'ok') {
			memory = result.data;
			memoryError = '';
		} else if (result.status === 'error') {
			memoryError = result.message;
		}
	}

	const memoryValue = $derived(
		memoryLoad === 'error' ? 'Unavailable' : memoryLoad === 'forbidden' ? FORBIDDEN_TEXT : memorySummary(memory)
	);

	/* ---------- license ---------- */

	const STATE: Record<LicenseState, { text: string; tone: StatusTone }> = {
		beta: { text: 'Beta', tone: 'ok' },
		active: { text: 'Active', tone: 'ok' },
		trialing: { text: 'Trial', tone: 'ok' },
		grace: { text: 'Payment grace period', tone: 'warn' },
		expiring: { text: 'Renewal overdue', tone: 'warn' },
		ending: { text: 'Ending', tone: 'warn' },
		expired: { text: 'Expired', tone: 'err' },
		unlicensed: { text: 'Not activated', tone: 'warn' },
		invalid: { text: 'Needs attention', tone: 'err' }
	};

	let license = $state<LicenseStatus | null>(null);
	let licenseLoad = $state<'loading' | 'ok' | 'forbidden' | 'error'>('loading');
	let licenseKey = $state('');
	let licenseBusy = $state(false);
	let licenseError = $state('');
	let licenseNotice = $state('');
	let seatHolders = $state<SeatHolder[] | null>(null);
	let keyOpen = $state(false);
	let seatsOpen = $state(false);
	let alive = true;

	const licensed = $derived(license?.mode === 'licensed');
	const hasLease = $derived(!!license && licensed && !['unlicensed', 'invalid'].includes(license.state));
	const stateView = $derived(
		license
			? (STATE[license.state] ?? { text: license.state, tone: 'off' as StatusTone })
			: {
					text: licenseLoad === 'forbidden' ? FORBIDDEN_TEXT : licenseLoad === 'error' ? 'Unavailable' : 'Loading…',
					tone: undefined
				}
	);

	function shortDate(iso: string | null): string {
		if (!iso) return '';
		const date = new Date(iso);
		return Number.isNaN(date.getTime()) ? iso : date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
	}

	function ago(iso: string | null): string {
		if (!iso) return 'Never checked';
		const t = new Date(iso).getTime();
		if (Number.isNaN(t)) return 'Checked';
		const minutes = Math.max(0, Math.round((Date.now() - t) / 60_000));
		if (minutes < 1) return 'Checked just now';
		if (minutes < 60) return `Checked ${minutes} min ago`;
		const hours = Math.round(minutes / 60);
		if (hours < 48) return `Checked ${hours} h ago`;
		return `Checked ${shortDate(iso)}`;
	}

	const licenseHint = $derived.by(() => {
		if (!license) return undefined;
		if (license.mode === 'beta') return 'No key needed in beta';
		if (!hasLease) return undefined;
		if (license.state === 'grace' && license.grace_until) return `Grace until ${shortDate(license.grace_until)}`;
		if (license.state === 'expired') return license.expires_at ? `Ended ${shortDate(license.expires_at)}` : undefined;
		const plan = planName(license.plan);
		return license.expires_at ? `${plan}, until ${shortDate(license.expires_at)}` : plan;
	});

	async function loadLicense() {
		const result = await loadState(getLicense);
		if (!alive) return;
		if (result.status === 'ok') {
			license = result.data;
			licenseLoad = 'ok';
		} else {
			licenseLoad = result.status;
			if (result.status === 'error') licenseError = result.message;
		}
	}

	function licenseChanged(next: LicenseStatus) {
		license = next;
		window.dispatchEvent(new Event(LICENSE_CHANGED_EVENT));
	}

	async function runLicense(action: () => Promise<void>) {
		if (licenseBusy) return;
		licenseBusy = true;
		licenseError = '';
		licenseNotice = '';
		try {
			await action();
		} catch (cause) {
			licenseError = getErrorMessage(cause, 'The license server could not be reached');
		} finally {
			licenseBusy = false;
		}
	}

	const activate = () =>
		runLicense(async () => {
			const result = await activateLicense(licenseKey.trim());
			if (!result.activated) {
				seatHolders = result.installs;
				seatsOpen = true;
				licenseError = result.detail;
				return;
			}
			const { activated: _activated, ...next } = result;
			seatHolders = null;
			licenseKey = '';
			keyOpen = false;
			licenseNotice = 'This worker is licensed.';
			flashSaved();
			licenseChanged(next);
		});

	const freeSeat = (holder: SeatHolder) =>
		runLicense(async () => {
			await releaseSeat(licenseKey.trim(), holder.install_id);
			seatHolders = seatHolders?.filter((h) => h.install_id !== holder.install_id) ?? null;
			licenseNotice = `Freed the seat of ${holder.name || 'that worker'}. Activate again to use it here.`;
		});

	const checkLicense = () =>
		runLicense(async () => {
			licenseChanged(await refreshLicense());
			licenseNotice = 'Checked with the license server.';
		});

	const deactivate = () =>
		runLicense(async () => {
			const result = await deactivateLicense();
			licenseNotice = result.seat_released
				? 'License deactivated and the seat freed.'
				: 'License removed from this worker. The license server could not be reached, so the seat frees itself after 21 days.';
			licenseChanged(result);
		});

	const showSeats = $derived(hasLease || !!seatHolders?.length);
	const seatsValue = $derived(
		seatHolders?.length && !hasLease
			? `${seatHolders.length} in use`
			: `${license?.seats_used ?? '?'} of ${license?.seats ?? 3}`
	);

	/* ---------- help: report a problem ---------- */

	let reportOpen = $state(false);
	let problem = $state('');

	/** The description becomes the report's error message; the dialog does the rest. */
	function reportProblem(description: string) {
		openSupportReport({ error: { message: description, source: 'frontend' } });
	}

	function closeReport() {
		reportOpen = false;
		// The editor held focus; give it back to the row's toggle.
		requestAnimationFrame(() =>
			document.querySelector<HTMLElement>('[data-setting-anchor="system.reportProblem"] button[aria-expanded]')?.focus()
		);
	}

	/* ---------- lifecycle ---------- */

	onMount(() => {
		void loadWorkspace();
		void loadLicense();
		void refreshDeploy();
		void loadMemory();
		void supportCaps.load();
		updates.start();
	});
	onDestroy(() => {
		alive = false;
		updates.destroy();
	});
	watchResources(['settings'], () => loadWorkspace(true));
</script>

<PreferencePage category="system">
	<RowGroup title="Workspace">
		<SettingRow id="system.workspace">
			{#if workspaceLoad === 'forbidden'}
				<ValueText value={FORBIDDEN_TEXT} />
			{:else}
				<PathField
					bind:value={workspacePath}
					placeholder="/path/to/workspace"
					disabled={workspaceLoad === 'loading'}
					onCheck={checkWorkspace}
				/>
			{/if}
		</SettingRow>
		<SettingRow id="system.import" expanded={importOpen ? importWizard : undefined}>
			<ExpandToggle value={importValue} bind:open={importOpen} />
		</SettingRow>
	</RowGroup>
	{#if workspaceRemote !== null}
		<InlineBanner tone="warn">
			Workspace folder changed elsewhere, your path kept
			{#snippet actions()}
				<button
					type="button"
					class="st-btn sm"
					onclick={() => {
						workspacePath = workspaceRemote ?? workspacePath;
						workspaceSaved = workspaceRemote;
						workspaceRemote = null;
					}}>Load latest</button
				>
			{/snippet}
		</InlineBanner>
	{/if}
	{#if workspaceLoad === 'error'}<p class="st-msg err" role="alert">Could not load the workspace folder.</p>{/if}

	<RowGroup title="Updates">
		<SettingRow
			id="system.allowUpdates"
			hint={allow ? 'Signed, without a new release' : 'Paused, this version keeps running'}
			value={updates.policy?.allow_updates}
			onReset={() => updates.set('allow_updates', true)}
		>
			<Switch checked={allow} disabled={updatesLocked} onchange={(v) => updates.set('allow_updates', v)} />
		</SettingRow>
		<SettingRow
			id="system.updateAll"
			dim={!allow}
			value={updates.policy?.update_all_workers}
			onReset={() => updates.set('update_all_workers', false)}
		>
			<Switch
				checked={updates.policy?.update_all_workers ?? false}
				disabled={updatesLocked || !allow}
				onchange={(v) => updates.set('update_all_workers', v)}
			/>
		</SettingRow>
		<SettingRow
			id="system.checkUpdates"
			hint={updates.policy?.update_all_workers ? 'Checks every online worker' : 'Checks the connected worker'}
			dim={!allow}
			expanded={resultsOpen && (updates.rows || updates.checkError) ? results : undefined}
		>
			<button type="button" class="st-btn" aria-busy={updates.checking} disabled={!updates.policy || !allow || updates.checking} onclick={runCheck}>
				{updates.checking ? 'Checking…' : 'Check now'}
			</button>
		</SettingRow>
		<SettingRow id="system.deployStatus" expanded={deployOpen ? deployList : undefined}>
			<ExpandToggle value={deployError ? 'Unavailable' : deploySummary(deploy)} bind:open={deployOpen} />
		</SettingRow>
		<SettingRow id="system.version">
			<ValueText value={__APP_VERSION__} mono />
		</SettingRow>
	</RowGroup>
	{#if updates.error}<p class="st-msg err" role="alert">{updates.error}</p>{/if}
	{#if updates.status === 'forbidden'}<p class="st-msg">Update settings: {FORBIDDEN_TEXT.toLowerCase()}</p>{/if}

	<RowGroup title="Memory">
		<SettingRow id="system.memoryHealth" expanded={memoryOpen && memory ? memoryList : undefined}>
			<span class="st-status" data-testid="memory-health">
				{#if memoryLoad === 'ok' && memory}
					<StatusDot tone={memory.state === 'healthy' ? 'ok' : 'warn'} /><ExpandToggle value={memoryValue} bind:open={memoryOpen} />
				{:else}
					<ValueText value={memoryValue} />
				{/if}
			</span>
		</SettingRow>
	</RowGroup>
	{#if memoryError}<p class="st-msg err" role="alert">{memoryError}</p>{/if}

	{#if license?.mode === 'licensed' && (license.banner || (!license.allowed && license.reason))}
		<InlineBanner tone="warn">
			{license.banner?.message ?? license.reason}
		</InlineBanner>
	{/if}
	<RowGroup title="License">
		<SettingRow id="system.license" hint={licenseHint}>
			<span data-testid="license-state"><ValueText value={stateView.text} status={stateView.tone} /></span>
		</SettingRow>
		{#if showSeats}
			<SettingRow id="system.seats" expanded={seatsOpen && seatHolders?.length ? seatList : undefined}>
				{#if seatHolders?.length}
					<ExpandToggle value={seatsValue} bind:open={seatsOpen} />
				{:else}
					<span data-testid="license-seats"><ValueText value={seatsValue} tone="fg" /></span>
				{/if}
			</SettingRow>
		{/if}
		{#if licensed && license}
			<SettingRow
				id="system.licenseKey"
				hint={hasLease ? ago(license.last_refresh_at) : 'Paste the key to activate'}
				expanded={keyOpen ? keyForm : undefined}
			>
				<ExpandToggle value={license.key_hint ? `ASH-…${license.key_hint}` : 'Not set'} mono={!!license.key_hint} bind:open={keyOpen} />
			</SettingRow>
		{/if}
		{#if hasLease}
			<SettingRow id="system.deactivate">
				<InlineConfirm
					label="Deactivate"
					question="Pause new runs here?"
					confirmLabel="Deactivate"
					disabled={licenseBusy}
					onConfirm={deactivate}
				/>
			</SettingRow>
		{/if}
	</RowGroup>
	{#if licenseNotice}<p class="st-msg" role="status">{licenseNotice}</p>{/if}
	{#if licenseError}<p class="st-msg err" role="alert">{licenseError}</p>{/if}

	{#if canSendSupportReport()}
		<RowGroup title="Help">
			<SettingRow id="system.reportProblem" expanded={reportOpen ? reportForm : undefined}>
				<ExpandToggle value="Describe it" bind:open={reportOpen} />
			</SettingRow>
		</RowGroup>
	{/if}
</PreferencePage>

{#snippet reportForm()}
	<ReportProblemForm bind:description={problem} onContinue={reportProblem} onCancel={closeReport} />
{/snippet}

{#snippet importWizard()}
	<ImportWizard bind:step={importStep} onImported={(n) => (imported = n)} onClose={() => (importOpen = false)} />
{/snippet}

{#snippet results()}
	<UpdateCheckResults rows={updates.rows ?? []} running={updates.checking} error={updates.checkError} />
{/snippet}

{#snippet deployList()}
	<UpdateCheckResults rows={deploy ? deployRows(deploy) : []} running={deployLoading} error={deployError} />
{/snippet}

{#snippet memoryList()}
	<MemoryHealthList rows={memory ? memoryRows(memory) : []} />
{/snippet}

{#snippet keyForm()}
	{#if license}
		<LicenseKeyForm
			status={license}
			{hasLease}
			busy={licenseBusy}
			bind:key={licenseKey}
			onActivate={activate}
			onCheck={checkLicense}
		/>
	{/if}
{/snippet}

{#snippet seatList()}
	<LicenseSeats holders={seatHolders ?? []} busy={licenseBusy} onFree={freeSeat} />
{/snippet}
