<script lang="ts">
	/**
	 * System: Workspace (folder, import from Claude Code and Codex), Your data
	 * (export everything as one ZIP), Updates
	 * (policy, check for updates, version), Memory (read-only health)
	 * and Help (report a problem to HobbyCoders support). The former License
	 * rows live in Cloud (CloudPage.svelte).
	 *
	 * The workspace path saves when "Check path" finds it valid. A path edited
	 * here is never replaced by a change made elsewhere; a banner offers the
	 * newer one instead.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { getWorkspaceConfig, setWorkspaceConfig, validateWorkspacePath } from '$lib/api/auth';
	import { exportAllData } from '$lib/api/sessions';
	import { watchResources } from '$lib/services/watch-resources';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import { FORBIDDEN_TEXT, loadState } from '$lib/settings/access.svelte';
	import PreferencePage from '../shell/PreferencePage.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import Switch from '../rows/Switch.svelte';
	import PathField, { type PathCheck } from '../rows/PathField.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import StatusDot from '../rows/StatusDot.svelte';
	import ExpandToggle from '../rows/ExpandToggle.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import ImportWizard, { type ImportStep } from './editors/ImportWizard.svelte';
	import UpdateCheckResults from './editors/UpdateCheckResults.svelte';
	import MemoryHealthList from './editors/MemoryHealthList.svelte';
	import { UpdatesState } from './updates-state.svelte';
	import { deployRows, deploySummary, loadDeployStatus, type DeployReport } from './deploy-status';
	import { fetchMemoryHealth, memoryRows, memorySummary, type MemoryHealth } from './memory-health';
	import ReportProblemForm from './editors/ReportProblemForm.svelte';
	import { canSendSupportReport, openSupportReport, supportCaps } from '$lib/stores/support-report.svelte';
	import './editors/editors.css';

	let alive = true;

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

	/* ---------- export all data ---------- */

	let exporting = $state(false);
	let exportNotice = $state('');
	let exportError = $state('');

	async function runExport() {
		if (exporting) return;
		exporting = true;
		exportError = '';
		exportNotice = 'Building the ZIP. A long history takes a few minutes; keep this page open.';
		try {
			await exportAllData();
			exportNotice = 'Downloaded. Chats are raw backups, so keep the file private.';
		} catch (cause) {
			exportNotice = '';
			exportError = getErrorMessage(cause, 'The export could not be downloaded');
		} finally {
			exporting = false;
		}
	}

	/* ---------- updates ---------- */

	const updates = new UpdatesState();
	let resultsOpen = $state(false);
	const allow = $derived(updates.policy?.allow_updates ?? true);
	const updatesLocked = $derived(!updates.policy || updates.saving);

	function runCheck() {
		resultsOpen = true;
		void updates.check();
		void refreshDeploy();
	}

	/* ---------- running revisions (deploy status) ---------- */

	let deploy = $state<DeployReport | null>(null);
	let deployError = $state('');
	let deployOpen = $state(false);
	let deployLoading = $state(false);
	// Being behind (even with an idle update check) does not prove a publication
	// gap. Only structured current-main gate evidence proves a refusal; older
	// backends safely omit it. Successful publication supersedes that evidence.
	const unpublishedEdgeMain = $derived.by(() => {
		const report = deploy;
		if (deployLoading || deployError || !report?.origin_main) return null;
		return report.workers.find((worker) =>
			worker.online && worker.channel === 'edge' && (worker.behind ?? 0) > 0 &&
			worker.edge_publication_refusal?.revision === report.origin_main
		)?.edge_publication_refusal ?? null;
	});

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

	<RowGroup title="Your data">
		<SettingRow id="system.exportAll">
			<button type="button" class="st-btn" aria-busy={exporting} disabled={exporting} onclick={runExport}>
				{exporting ? 'Exporting…' : 'Export'}
			</button>
		</SettingRow>
	</RowGroup>
	{#if exportNotice}<p class="st-msg" role="status">{exportNotice}</p>{/if}
	{#if exportError}<p class="st-msg err" role="alert">{exportError}</p>{/if}

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
	{#if unpublishedEdgeMain}
		<p class="st-msg" role="status">“Up to date” means the last published update. Main is at {unpublishedEdgeMain.revision}, not published to edge yet: {unpublishedEdgeMain.reason}</p>
	{/if}
{/snippet}

{#snippet deployList()}
	<p class="st-msg">Running revisions compares workers with main. “Up to date” in Check for updates means the latest published worker update.</p>
	{#if unpublishedEdgeMain}
		<p class="st-msg" role="status">The newest main revision is not published as an edge worker update yet. See the publication refusal and next step below.</p>
	{/if}
	<UpdateCheckResults rows={deploy ? deployRows(deploy) : []} running={deployLoading} error={deployError} />
{/snippet}

{#snippet memoryList()}
	<MemoryHealthList rows={memory ? memoryRows(memory) : []} />
{/snippet}
