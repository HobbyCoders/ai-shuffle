<script lang="ts">
	import Banner from '$lib/components/Banner.svelte';
	/**
	 * AddProjectModal - the shared "New project" dialog.
	 *
	 * Used by WorkspaceSelector and ProjectCard. Two sources:
	 * - "Folder on <this worker>": DirectoryBrowser + path, auto-derived name/ID.
	 * - "Clone from GitHub": pick a repository (GitHubRepoPicker, the same
	 *   picker the Git panel's clone dialog uses) or paste a URL. The project
	 *   is created in this worker's workspace and the repository cloned into it.
	 *
	 * Then "Set it up on": this worker is locked and ticked; the other approved
	 * workers follow decision 3 of cross-worker project sync. For a Git repo
	 * every online worker starts ticked (the owner's last unticks are
	 * remembered); offline ones start unticked and, if ticked, run when they are
	 * back. For a plain folder each worker gets an empty folder of the same
	 * name (files aren't copied), so they start unticked. Setups start only
	 * after the local create (and clone) succeeded. The list shows only when
	 * this worker can set projects up elsewhere (the overview answers).
	 */
	import { X, Folder, Github, Check } from 'lucide-svelte';
	import { appData } from '$lib/stores/app-data.svelte';
	import { loadProjectsOverview, projectsOverviewSupported } from '$lib/stores/projects-overview';
	import { getWorkspaceConfig } from '$lib/api/auth';
	import { cloneRepository } from '$lib/api/git';
	import { startProjectSetup } from '$lib/api/project-workers';
	import type { GitHubRepo } from '$lib/api/github';
	import type { Install } from '$lib/api/installs';
	import { onMount, untrack } from 'svelte';
	import DirectoryBrowser from '$lib/components/DirectoryBrowser.svelte';
	import GitHubRepoPicker from '$lib/components/git/GitHubRepoPicker.svelte';
	import { focusTrap } from '$lib/components/ui/focus-trap';
	import { layout } from '$lib/stores/layout.svelte';
	import { workerInstalls, refreshWorkerPresence } from '$lib/stores/worker-presence';
	import { peerRunners } from '$lib/components/layout/execution-mode-eligibility';
	import { projectSetupTargets } from '$lib/stores/project-setup-targets.svelte';

	interface Props {
		open: boolean;
		onClose: () => void;
		onCreated?: (projectId: string) => void;
	}

	let { open, onClose, onCreated }: Props = $props();

	type Source = 'folder' | 'github';

	let isLocalMode = $state(false);
	let workspacePath = $state('');

	let source = $state<Source>('folder');
	let projectId = $state('');
	let projectName = $state('');
	let projectPath = $state('');
	let cloneUrl = $state('');
	let adding = $state(false);
	let progressText = $state<string | null>(null);
	let addError = $state<string | null>(null);
	let nameManuallyEdited = $state(false);
	let idManuallyEdited = $state(false);
	/** Install ids ticked under "Set it up on" (this worker is implied). */
	let ticked = $state<string[]>([]);
	/** The owner changed a tick since the dialog opened (then their choice stands). */
	let ticksTouched = $state(false);

	onMount(async () => {
		try {
			const config = await getWorkspaceConfig();
			isLocalMode = config.is_local_mode;
			workspacePath = config.workspace_path ?? '';
		} catch {
			// Default to non-local on error
		}
	});

	const installs = $derived<Install[]>($workerInstalls ?? []);
	const selfName = $derived(installs.find((i) => i.is_self)?.device_name ?? 'this worker');
	const others = $derived(peerRunners(installs));
	// The "Set it up on" list needs the cross-worker setup endpoints.
	const canSetUpElsewhere = $derived($projectsOverviewSupported === true && others.length > 0);

	// Reset form when modal opens
	$effect(() => {
		if (open) {
			untrack(() => {
				source = 'folder';
				projectId = '';
				projectName = '';
				projectPath = '';
				cloneUrl = '';
				addError = null;
				progressText = null;
				nameManuallyEdited = false;
				idManuallyEdited = false;
				ticksTouched = false;
				ticked = [];
				void refreshWorkerPresence().catch(() => {});
				void loadProjectsOverview();
			});
		}
	});

	// Default ticks follow the source until the owner changes one.
	$effect(() => {
		const list = others;
		const src = source;
		if (!open || ticksTouched) return;
		untrack(() => {
			if (src === 'folder') { ticked = []; return; }
			const skip = new Set(projectSetupTargets.unticked);
			ticked = list.filter((i) => i.online && !skip.has(i.id)).map((i) => i.id);
		});
	});

	function slugify(name: string): string {
		return name.toLowerCase().replace(/[^a-z0-9-]/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '').slice(0, 50);
	}

	function deriveNameAndId(path: string) {
		const dirName = path.split(/[/\\]/).filter(Boolean).pop() || '';
		if (!nameManuallyEdited) projectName = dirName;
		if (!idManuallyEdited) projectId = slugify(dirName);
	}

	function handleDirectorySelect(path: string) {
		projectPath = path;
		deriveNameAndId(path);
	}

	function handlePathInput(e: Event) {
		const value = (e.target as HTMLInputElement).value;
		projectPath = value;
		if (value.trim()) deriveNameAndId(value);
	}

	/** The repository's name from a clone URL (…/owner/name(.git)). */
	function repoNameFromUrl(url: string): string {
		const tail = url.trim().replace(/\/+$/, '').split(/[/:]/).pop() ?? '';
		return tail.replace(/\.git$/i, '');
	}

	function setCloneUrl(url: string) {
		cloneUrl = url;
		const name = repoNameFromUrl(url);
		if (!nameManuallyEdited) projectName = name;
		if (!idManuallyEdited) projectId = slugify(name);
	}

	function pickRepo(repo: GitHubRepo) {
		setCloneUrl(repo.clone_url);
		addError = null;
	}

	/** Where the clone lands on this worker: its workspace, under the repo's name. */
	const clonePath = $derived.by(() => {
		const leaf = projectId.trim() || slugify(projectName);
		if (!leaf) return '';
		if (!isLocalMode || !workspacePath) return leaf;
		const sep = workspacePath.includes('\\') && !workspacePath.includes('/') ? '\\' : '/';
		return `${workspacePath.replace(/[/\\]+$/, '')}${sep}${leaf}`;
	});

	const folderLeaf = $derived(
		(source === 'github' ? clonePath : projectPath).split(/[/\\]/).filter(Boolean).pop() || projectName || 'project'
	);

	const canSubmit = $derived(
		!adding && !!projectId.trim() && !!projectName.trim() &&
			(source === 'folder' ? !!projectPath.trim() : !!cloneUrl.trim() && !!clonePath)
	);

	const targetIds = $derived(canSetUpElsewhere ? ticked.filter((id) => others.some((i) => i.id === id)) : []);
	const workerCount = $derived(1 + targetIds.length);
	const submitLabel = $derived(
		adding
			? (progressText ?? 'Creating…')
			: canSetUpElsewhere
				? `Create on ${workerCount} ${workerCount === 1 ? 'worker' : 'workers'}`
				: source === 'github' ? 'Clone project' : 'Add Project'
	);

	function toggleTarget(id: string) {
		ticksTouched = true;
		ticked = ticked.includes(id) ? ticked.filter((x) => x !== id) : [...ticked, id];
	}

	async function handleAddProject() {
		if (!canSubmit) return;
		adding = true;
		addError = null;
		const chosen = [...targetIds];
		let created: { id: string } | null = null;
		try {
			progressText = 'Creating…';
			created = await appData.createProject({
				id: projectId.trim(),
				name: projectName.trim(),
				path: source === 'github' ? clonePath : projectPath.trim()
			});
			if (source === 'github') {
				progressText = 'Cloning…';
				await cloneRepository(created.id, { clone_url: cloneUrl.trim() });
			}
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			const reason = err.detail || err.message || 'Failed to add project';
			addError = created
				? `The project was created, but cloning failed: ${reason}`
				: reason;
			adding = false;
			progressText = null;
			if (created) onCreated?.(created.id);
			return;
		}
		// Remember the Git choice (decision 3); a plain folder's ticks are not a preference.
		if (canSetUpElsewhere && source === 'github') {
			projectSetupTargets.remember(others.filter((i) => !chosen.includes(i.id)).map((i) => i.id));
		}
		// Setups start after the local create succeeded. Each runs on its own; a
		// failure shows on the project's Workers tab, not here.
		if (chosen.length) {
			await Promise.allSettled(
				chosen.map((install_id) =>
					startProjectSetup({ project_id: created!.id }, { install_id, folder: folderLeaf })
				)
			);
			void loadProjectsOverview();
		}
		adding = false;
		progressText = null;
		onClose();
		// Use the server-assigned id — the backend may rewrite it (auto-suffix
		// on a cross-install id collision), so passing the requested id would
		// select a project that GET /projects never returns.
		onCreated?.(created.id);
	}

	function openGitHubSettings() {
		onClose();
		layout.openFeaturePage('settings', 'category=integrations&item=github');
	}

	function handleWindowKeydown(e: KeyboardEvent) {
		if (open && e.key === 'Escape' && !e.defaultPrevented) {
			e.preventDefault();
			onClose();
		}
	}

	function onFieldEnter(e: KeyboardEvent) {
		if (e.key === 'Enter' && canSubmit) handleAddProject();
	}
</script>

<svelte:window onkeydown={handleWindowKeydown} />

{#if open}
	<div class="create-modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="add-project-title" tabindex="-1" use:focusTrap>
		<button class="create-modal-backdrop-btn" onclick={onClose} tabindex="-1" aria-label="Close dialog"></button>
		<div class="create-modal add-project-modal">
			<div class="create-modal-header">
				<div>
					<h3 id="add-project-title" class="create-modal-title">New project</h3>
					<p class="create-modal-sub">Add a folder, or clone a repo from GitHub.</p>
				</div>
				<button class="create-modal-close" onclick={onClose} aria-label="Close">
					<X size={16} strokeWidth={1.5} />
				</button>
			</div>

			{#if addError}
				<Banner message={addError} />
			{/if}

			<div class="src" role="radiogroup" aria-label="Source">
				<button type="button" role="radio" aria-checked={source === 'folder'} class:on={source === 'folder'} onclick={() => (source = 'folder')} disabled={adding}>
					<Folder size={14} strokeWidth={1.8} aria-hidden="true" /><span>Folder on {selfName}</span>
				</button>
				<button type="button" role="radio" aria-checked={source === 'github'} class:on={source === 'github'} onclick={() => (source = 'github')} disabled={adding}>
					<Github size={14} strokeWidth={1.8} aria-hidden="true" /><span>Clone from GitHub</span>
				</button>
			</div>

			{#if source === 'folder'}
				<div class="create-modal-field">
					<span class="create-modal-label">Location</span>
					<DirectoryBrowser selectedPath={projectPath} onSelect={handleDirectorySelect} />
				</div>

				<div class="create-modal-field">
					<label class="create-modal-label" for="add-project-path">Path</label>
					<input
						id="add-project-path"
						type="text"
						class="create-modal-input create-modal-input--mono"
						placeholder={isLocalMode ? '/path/to/project' : 'my-project'}
						value={projectPath}
						oninput={handlePathInput}
						onkeydown={onFieldEnter}
					/>
					<span class="path-hint">Select a folder above or type a path. New folders will be created automatically.</span>
				</div>
			{:else}
				<div class="create-modal-field" data-repo-picker>
					<span class="create-modal-label">Repository</span>
					<GitHubRepoPicker selectedUrl={cloneUrl} disabled={adding} onSelect={pickRepo} onConnect={openGitHubSettings} />
				</div>
				<div class="create-modal-field">
					<label class="create-modal-label" for="add-project-url">Or paste a clone URL</label>
					<input
						id="add-project-url"
						type="text"
						class="create-modal-input create-modal-input--mono"
						placeholder="https://github.com/owner/repo.git"
						value={cloneUrl}
						oninput={(e) => setCloneUrl((e.target as HTMLInputElement).value)}
						onkeydown={onFieldEnter}
					/>
					{#if clonePath}<span class="path-hint">Clones into <code>{clonePath}</code> on {selfName}.</span>{/if}
				</div>
			{/if}

			<div class="create-modal-row">
				<div class="create-modal-field create-modal-field--half">
					<label class="create-modal-label" for="add-project-name">Display Name</label>
					<input
						id="add-project-name"
						type="text"
						class="create-modal-input"
						placeholder="My Project"
						bind:value={projectName}
						oninput={() => { nameManuallyEdited = true; }}
						onkeydown={onFieldEnter}
					/>
				</div>
				<div class="create-modal-field create-modal-field--half">
					<label class="create-modal-label" for="add-project-id">Project ID</label>
					<input
						id="add-project-id"
						type="text"
						class="create-modal-input create-modal-input--mono"
						placeholder="my-project"
						bind:value={projectId}
						oninput={() => { idManuallyEdited = true; }}
						onkeydown={onFieldEnter}
					/>
				</div>
			</div>

			{#if canSetUpElsewhere}
				<fieldset class="wpick-set">
					<legend class="sect">Set it up on</legend>
					<div class="wpick">
						<div class="wpick-row locked">
							<span class="box lock" aria-hidden="true"><Check size={11} strokeWidth={3} /></span>
							<span class="wpick-name">{selfName} <span class="hint">· this worker</span></span>
							<span class="hint mono">{source === 'github' ? clonePath || '—' : projectPath || '—'}</span>
						</div>
						{#each others as w (w.id)}
							{@const on = ticked.includes(w.id)}
							<label class="wpick-row" class:dim={!w.online}>
								<input
									type="checkbox"
									class="sr-only"
									checked={on}
									disabled={adding}
									onchange={() => toggleTarget(w.id)}
								/>
								<span class="box" class:on aria-hidden="true">{#if on}<Check size={11} strokeWidth={3} />{/if}</span>
								<span class="wpick-name">{w.device_name}{#if !w.online}<span class="hint">&nbsp;· offline</span>{/if}</span>
								<span class="hint" class:mono={w.online && source === 'github'}>
									{#if !w.online}
										runs when it's back
									{:else if source === 'github'}
										…/{folderLeaf}
									{:else}
										creates an empty {folderLeaf} folder; files aren't copied
									{/if}
								</span>
							</label>
						{/each}
					</div>
					<p class="hint foot">
						{#if source === 'github'}
							Each worker clones with its own GitHub sign-in, into its workspace folder. You can add or remove workers later in the project's Workers tab.
						{:else}
							A Git repo with a remote is cloned on each worker instead. You can add or remove workers later in the project's Workers tab.
						{/if}
					</p>
				</fieldset>
			{/if}

			<div class="create-modal-actions">
				<button class="modal-btn cancel" onclick={onClose}>Cancel</button>
				<button class="modal-btn create" disabled={!canSubmit} onclick={handleAddProject}>{submitLabel}</button>
			</div>
		</div>
	</div>
{/if}

<style>
	.create-modal-backdrop {
		position: fixed;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		z-index: 1000;
		padding: 12px;
	}

	.create-modal-backdrop-btn {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		background: color-mix(in oklch, var(--background) 70%, transparent);
		backdrop-filter: blur(4px);
		border: none;
		cursor: default;
		padding: 0;
	}

	.create-modal {
		position: relative;
		z-index: 1;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 12px;
		padding: 20px;
		width: 380px;
		max-width: 100%;
		max-height: calc(100dvh - 24px);
		overflow-y: auto;
		box-shadow: var(--shadow-l);
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.add-project-modal { width: 520px; }

	.create-modal-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
	.create-modal-title { font-size: 0.9375rem; font-weight: 600; color: var(--foreground); margin: 0; }
	.create-modal-sub { margin: 3px 0 0; font-size: 0.78rem; color: var(--muted-foreground); }

	.create-modal-close {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
		flex: none;
		background: transparent;
		border: none;
		border-radius: 6px;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 150ms ease;
	}
	.create-modal-close:hover { background: var(--hover-overlay); color: var(--foreground); }
	.create-modal-close:focus-visible { outline: 2px solid var(--ring); outline-offset: 1px; }

	/* Source choice */
	.src { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
	.src button {
		display: inline-flex;
		align-items: center;
		gap: 7px;
		min-height: 38px;
		padding: 8px 10px;
		border: 1px solid var(--border);
		border-radius: 9px;
		background: transparent;
		color: var(--muted-foreground);
		font: inherit;
		font-size: 0.8rem;
		text-align: left;
		cursor: pointer;
		transition: border-color 120ms ease, color 120ms ease, background-color 120ms ease;
	}
	.src button span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.src button:hover:not(:disabled) { color: var(--foreground); background: var(--hover-overlay); }
	.src button.on { border-color: var(--primary); color: var(--foreground); background: color-mix(in oklch, var(--primary) 10%, transparent); }
	.src button:focus-visible { outline: 2px solid var(--ring); outline-offset: 1px; }
	.src button:disabled { opacity: 0.6; cursor: not-allowed; }

	.create-modal-field { display: flex; flex-direction: column; gap: 4px; }
	.create-modal-field--half { flex: 1; min-width: 0; }
	.create-modal-row { display: flex; gap: 12px; }
	.create-modal-label { font-size: 0.75rem; font-weight: 500; color: var(--muted-foreground); }

	.create-modal-input {
		width: 100%;
		padding: 8px 12px;
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 8px;
		color: var(--foreground);
		font-size: 0.8125rem;
		outline: none;
		transition: border-color 150ms ease;
	}
	.create-modal-input--mono { font-family: var(--font-mono, 'JetBrains Mono', 'SF Mono', monospace); font-size: 0.75rem; }
	.create-modal-input:focus { border-color: var(--primary); box-shadow: 0 0 0 2px color-mix(in oklch, var(--primary) 15%, transparent); }

	.path-hint { font-size: 0.6875rem; color: var(--muted-foreground); opacity: 0.8; }
	.path-hint code { font-family: var(--font-mono, monospace); color: var(--foreground); word-break: break-all; }

	/* Set it up on */
	.wpick-set { border: none; margin: 0; padding: 0; min-width: 0; }
	.sect { padding: 0; margin: 2px 0 6px; font-size: 0.6875rem; letter-spacing: 0.1em; text-transform: uppercase; color: var(--muted-foreground); }
	.wpick { border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
	.wpick-row {
		display: grid;
		grid-template-columns: 18px minmax(0, auto) minmax(0, 1fr);
		gap: 10px;
		align-items: center;
		min-height: 40px;
		padding: 8px 11px;
		border-top: 1px solid var(--border);
		font-size: 0.8125rem;
		color: var(--foreground);
		cursor: pointer;
	}
	.wpick-row:first-child { border-top: 0; }
	.wpick-row.locked { cursor: default; }
	.wpick-row:not(.locked):hover { background: var(--hover-overlay); }
	.wpick-row:has(input:focus-visible) { outline: 2px solid var(--ring); outline-offset: -2px; }
	.wpick-row.dim .wpick-name { color: var(--muted-foreground); }
	.wpick-name { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.wpick-row > .hint:last-child { justify-self: end; text-align: right; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100%; }
	.box {
		display: grid;
		place-items: center;
		width: 16px;
		height: 16px;
		border-radius: 4px;
		border: 1.5px solid var(--muted-foreground);
		color: var(--primary-foreground);
		transition: background-color 120ms ease, border-color 120ms ease;
	}
	.box.on { background: var(--primary); border-color: var(--primary); }
	.box.lock { background: color-mix(in oklch, var(--muted-foreground) 30%, transparent); border-color: transparent; color: var(--foreground); }
	.hint { font-size: 0.72rem; color: var(--muted-foreground); }
	.mono { font-family: var(--font-mono, monospace); }
	.foot { margin: 8px 0 0; }

	.create-modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 4px; }

	.modal-btn {
		padding: 6px 14px;
		min-height: 32px;
		font-size: 0.8125rem;
		font-weight: 500;
		border-radius: 8px;
		border: none;
		cursor: pointer;
		transition: all 150ms ease;
	}
	.modal-btn:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.modal-btn.cancel { background: var(--secondary); color: var(--muted-foreground); }
	.modal-btn.cancel:hover { background: var(--hover-overlay); color: var(--foreground); }
	.modal-btn.create { background: var(--primary); color: var(--primary-foreground); }
	.modal-btn.create:hover { opacity: 0.9; }
	.modal-btn.create:disabled { opacity: 0.4; cursor: not-allowed; }

	@media (max-width: 480px) {
		.create-modal-row { flex-direction: column; }
		.src { grid-template-columns: 1fr; }
		.wpick-row { grid-template-columns: 18px minmax(0, 1fr); }
		.wpick-row > .hint:last-child { grid-column: 2; justify-self: start; text-align: left; white-space: normal; }
	}
</style>
