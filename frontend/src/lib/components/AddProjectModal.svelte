<script lang="ts">
	import '$lib/components/ui/surfaces.css';
	import Checkbox from '$lib/components/ui/Checkbox.svelte';
	import Banner from '$lib/components/Banner.svelte';
	/**
	 * AddProjectModal - the shared "New project" dialog.
	 *
	 * Used by WorkspaceSelector, WorkspaceFolderSelector and ProjectCard. Two sources:
	 * - "Folder on <this worker>": DirectoryBrowser + path, auto-derived name/ID.
	 * - "Clone from GitHub": pick a repository (GitHubRepoPicker, the same
	 *   picker the Git panel's clone dialog uses) or paste a URL. The clone runs
	 *   on this worker as a background setup (POST /projects/clone): the dialog
	 *   shows its stages and progress, and the project is registered only once
	 *   the clone succeeded, so a failed clone leaves nothing behind and Retry
	 *   sends the same request again. Repositories that are already a project
	 *   are marked "Already added" and can't be picked again.
	 *
	 * The whole request is read once when the owner submits; the form is locked
	 * until it finishes. Closing during a clone keeps it running (the dialog
	 * says so) and reports the outcome as a notification.
	 *
	 * Then "Set it up on": this worker is locked and ticked; the other approved
	 * workers follow decision 3 of cross-worker project sync. For a Git repo
	 * every online worker starts ticked (the owner's last unticks are
	 * remembered); offline ones start unticked and, if ticked, run when they are
	 * back. For a plain folder each worker uses its folder of the same name if
	 * it has one, else gets an empty one (files aren't copied), so they start
	 * unticked. Until the owner changes a tick, switching the source applies
	 * that source's default (the list says which). Setups start only after the
	 * local create (and clone) succeeded; a worker that refuses to start one is
	 * named in a notice. Progress stays on the project's Workers tab.
	 */
	import { X, Folder, Github, Check } from 'lucide-svelte';
	import { appData } from '$lib/stores/app-data.svelte';
	import { loadProjectsOverview, projectsOverview, projectsOverviewSupported } from '$lib/stores/projects-overview';
	import { getWorkspaceConfig } from '$lib/api/auth';
	import {
		cancelProjectSetup,
		cloneNewProject,
		getSetupStatus,
		remoteKey,
		startProjectSetup,
		type SetupStatus
	} from '$lib/api/project-workers';
	import type { GitHubRepo } from '$lib/api/github';
	import type { Install } from '$lib/api/installs';
	import { selfWorkerName, workerDisplayName } from '$lib/components/ui/worker-name';
	import { onDestroy, onMount, untrack } from 'svelte';
	import Segmented from '$lib/components/settings/rows/Segmented.svelte';
	import DirectoryBrowser from '$lib/components/DirectoryBrowser.svelte';
	import GitHubRepoPicker from '$lib/components/git/GitHubRepoPicker.svelte';
	import { focusTrap } from '$lib/components/ui/focus-trap';
	import { layout } from '$lib/stores/layout.svelte';
	import { notify } from '$lib/stores/notifications';
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

	/** How often a running clone is read (the server also pushes changes). */
	const POLL_MS = 1000;
	const NAME_MAX = 100;
	const ID_MAX = 50;
	const CANCELLED = 'Clone cancelled. Nothing was added.';

	let isLocalMode = $state(false);
	let workspacePath = $state('');

	let source = $state<Source>('folder');
	let projectId = $state('');
	let projectName = $state('');
	let projectPath = $state('');
	let cloneUrl = $state('');
	let adding = $state(false);
	let progressText = $state<string | null>(null);
	let progress = $state<number | null>(null);
	let addError = $state<string | null>(null);
	/** What the error lets the owner do next. */
	let errorAction = $state<'retry' | 'github' | 'advanced' | null>(null);
	/** A running clone's setup op (cancel target). */
	let runningOp = $state<string | null>(null);
	let cancelling = $state(false);
	/** The server says this repository is already this project (409 already_added). */
	let existingId = $state<string | null>(null);
	let nameManuallyEdited = $state(false);
	let idManuallyEdited = $state(false);
	let advancedOpen = $state(false);
	/** Install ids ticked under "Set it up on" (this worker is implied). */
	let ticked = $state<string[]>([]);
	/** The owner changed a tick since the dialog opened (then their choice stands). */
	let ticksTouched = $state(false);
	/** Bumped on every open: a run from an earlier opening never writes this one's form. */
	let generation = 0;
	let destroyed = false;
	onDestroy(() => { destroyed = true; });

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
	const selfName = $derived(selfWorkerName(installs));
	const others = $derived(peerRunners(installs));
	// The "Set it up on" list needs the cross-worker setup endpoints.
	const canSetUpElsewhere = $derived($projectsOverviewSupported === true && others.length > 0);

	// Reset form when modal opens
	$effect(() => {
		if (open) {
			untrack(() => {
				generation += 1;
				source = 'folder';
				projectId = '';
				projectName = '';
				projectPath = '';
				cloneUrl = '';
				adding = false;
				addError = null;
				errorAction = null;
				existingId = null;
				progressText = null;
				progress = null;
				runningOp = null;
				cancelling = false;
				nameManuallyEdited = false;
				idManuallyEdited = false;
				advancedOpen = false;
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
		return name.toLowerCase().replace(/[^a-z0-9-]/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '').slice(0, ID_MAX);
	}

	function deriveNameAndId(path: string) {
		const dirName = path.split(/[/\\]/).filter(Boolean).pop() || '';
		if (!nameManuallyEdited) projectName = dirName;
		if (!idManuallyEdited) projectId = slugify(dirName);
	}

	function handleDirectorySelect(path: string) {
		if (adding) return;
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
		if (url.trim() !== cloneUrl.trim()) {
			// The last failure was about another repository.
			addError = null;
			errorAction = null;
			existingId = null;
		}
		cloneUrl = url;
		const name = repoNameFromUrl(url);
		if (!nameManuallyEdited) projectName = name;
		if (!idManuallyEdited) projectId = slugify(name);
	}

	function pickRepo(repo: GitHubRepo) {
		setCloneUrl(repo.clone_url);
	}

	/** Projects that already exist (on any worker), by repository. */
	const existingByRemote = $derived.by(() => {
		const map = new Map<string, { name: string; localId: string | null }>();
		for (const row of $projectsOverview) {
			const key = remoteKey(row.remote);
			if (key) map.set(key, { name: row.name, localId: row.local_project_id });
		}
		return map;
	});
	const isAdded = (repo: GitHubRepo) =>
		existingByRemote.has(remoteKey(repo.clone_url)) || existingByRemote.has(remoteKey(repo.html_url));
	/** The pasted or picked repository is already a project. */
	const alreadyAdded = $derived(source === 'github' && cloneUrl.trim() ? existingByRemote.get(remoteKey(cloneUrl)) : undefined);

	/** The folder the clone is sent to: an absolute path in local mode, else the folder name. */
	const cloneFolder = $derived.by(() => {
		const leaf = projectId.trim() || slugify(projectName);
		if (!leaf) return '';
		if (!isLocalMode || !workspacePath) return leaf;
		const sep = workspacePath.includes('\\') && !workspacePath.includes('/') ? '\\' : '/';
		return `${workspacePath.replace(/[/\\]+$/, '')}${sep}${leaf}`;
	});
	/** Where the clone lands, as shown: always under the workspace when it is known. */
	const cloneDestination = $derived.by(() => {
		if (!cloneFolder || isLocalMode || !workspacePath) return cloneFolder;
		return `${workspacePath.replace(/[/\\]+$/, '')}/${cloneFolder}`;
	});

	const folderLeaf = $derived(
		(source === 'github' ? cloneFolder : projectPath).split(/[/\\]/).filter(Boolean).pop() || projectName || 'project'
	);

	// The server's rules (ProjectCreate), shown before the owner submits.
	const nameError = $derived(projectName.trim().length > NAME_MAX ? `Use ${NAME_MAX} characters or fewer.` : null);
	const idError = $derived.by(() => {
		const id = projectId.trim();
		if (!id) return null;
		if (!/^[a-z0-9-]+$/.test(id)) return 'Use lowercase letters, numbers and dashes only.';
		if (id.length > ID_MAX) return `Use ${ID_MAX} characters or fewer.`;
		return null;
	});
	// An ID problem is never hidden behind the closed disclosure.
	$effect(() => { if (idError) advancedOpen = true; });

	const canSubmit = $derived(
		!adding && !!projectId.trim() && !!projectName.trim() && !nameError && !idError &&
			(source === 'folder' ? !!projectPath.trim() : !!cloneUrl.trim() && !!cloneFolder && !alreadyAdded)
	);

	const targetIds = $derived(canSetUpElsewhere ? ticked.filter((id) => others.some((i) => i.id === id)) : []);
	const workerCount = $derived(1 + targetIds.length);
	const submitLabel = $derived(
		adding
			? (progressText ?? 'Creating…')
			: addError && errorAction === 'retry'
				? 'Retry'
				: canSetUpElsewhere
					? `Create on ${workerCount} ${workerCount === 1 ? 'worker' : 'workers'}`
					: source === 'github' ? 'Clone project' : 'Add project'
	);

	function toggleTarget(id: string) {
		ticksTouched = true;
		ticked = ticked.includes(id) ? ticked.filter((x) => x !== id) : [...ticked, id];
	}

	interface Request {
		source: Source;
		id: string;
		name: string;
		path: string;
		cloneUrl: string;
		folder: string;
		folderLeaf: string;
		targets: { id: string; name: string }[];
		/** Workers to remember as unticked (Git only), or null. */
		untick: string[] | null;
	}

	class RunError extends Error {
		action: 'retry' | 'github' | 'advanced' | null;
		existingId: string | null;
		constructor(message: string, action: 'retry' | 'github' | 'advanced' | null, existingId: string | null = null) {
			super(message);
			this.action = action;
			this.existingId = existingId;
		}
	}

	function errorCode(e: unknown): string | null {
		const body = (e as { body?: { detail?: { error_code?: unknown } } } | null)?.body;
		const code = body?.detail?.error_code;
		return typeof code === 'string' ? code : null;
	}

	function reasonOf(e: unknown, fallback: string): string {
		const err = e as { detail?: string; message?: string } | null;
		return err?.detail || err?.message || fallback;
	}

	/** A refused request: what to say, and what the owner can do about it. */
	function requestError(e: unknown, fallback: string): RunError {
		const reason = reasonOf(e, fallback);
		const code = errorCode(e);
		if (code === 'already_added') {
			const existing = (e as { body?: { detail?: { project_id?: unknown } } }).body?.detail?.project_id;
			return new RunError(reason, null, typeof existing === 'string' ? existing : null);
		}
		if (code === 'folder_exists' || /already exists/i.test(reason)) return new RunError(reason, 'advanced');
		const status = (e as { status?: number } | null)?.status;
		// A 4xx is the request itself (fix the form); anything else may pass on a retry.
		return new RunError(reason, status && status >= 400 && status < 500 ? null : 'retry');
	}

	function cloneFailure(status: SetupStatus): RunError {
		if (status.status === 'cancelled') return new RunError(CANCELLED, 'retry');
		const message = status.error || 'The clone failed.';
		switch (status.error_code) {
			case 'github_auth': return new RunError(message, 'github');
			case 'folder_exists': return new RunError(message, 'advanced');
			// The repository's project moved to another one: the message names it.
			case 'moved': return new RunError(message, null);
			default: return new RunError(message, 'retry');
		}
	}

	const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

	/** Clone the new project on this worker; resolves to its id once registered. */
	async function runClone(req: Request, live: () => boolean): Promise<string> {
		const show = (text: string, value: number | null = null) => {
			if (!live()) return;
			progressText = text;
			progress = value;
		};
		show('Checking…');
		let started;
		try {
			started = await cloneNewProject({ id: req.id, name: req.name, clone_url: req.cloneUrl, folder: req.folder });
		} catch (e) {
			throw requestError(e, 'Failed to start the clone');
		}
		if (live()) runningOp = started.op_id;
		show('Waiting to clone…');
		let failures = 0;
		for (;;) {
			let status: SetupStatus;
			try {
				status = await getSetupStatus(started.op_id);
				failures = 0;
			} catch (e) {
				// A blip while polling is not the clone failing; give up only when it persists.
				if (++failures >= 5) throw new RunError(`Lost track of the clone: ${reasonOf(e, 'no answer')}`, null);
				await sleep(POLL_MS);
				continue;
			}
			if (status.status === 'done' && status.project_id) return status.project_id;
			if (status.status === 'error' || status.status === 'cancelled') throw cloneFailure(status);
			if (status.status === 'running') {
				show(status.progress != null ? `Cloning… ${status.progress}%` : 'Cloning…', status.progress);
			}
			await sleep(POLL_MS);
		}
	}

	/** Start the chosen workers' setups; returns a line per worker that refused. */
	async function startPeers(req: Request, createdId: string): Promise<string[]> {
		if (!req.targets.length) return [];
		const results = await Promise.allSettled(
			req.targets.map((t) => startProjectSetup({ project_id: createdId }, { install_id: t.id, folder: req.folderLeaf }))
		);
		void loadProjectsOverview();
		return results.flatMap((r, i) =>
			r.status === 'rejected' ? [`${req.targets[i].name}: ${reasonOf(r.reason, 'could not start')}`] : []
		);
	}

	async function handleAddProject() {
		if (!canSubmit) return;
		// Read the whole request now: the form is locked from here, but nothing
		// below may depend on what the inputs say later.
		const req: Request = {
			source,
			id: projectId.trim(),
			name: projectName.trim(),
			path: projectPath.trim(),
			cloneUrl: cloneUrl.trim(),
			folder: cloneFolder,
			folderLeaf,
			targets: targetIds.map((id) => ({ id, name: workerDisplayName(others.find((i) => i.id === id)?.device_name) })),
			untick: canSetUpElsewhere && source === 'github' ? others.filter((i) => !targetIds.includes(i.id)).map((i) => i.id) : null
		};
		const mine = generation;
		const live = () => !destroyed && open && generation === mine;
		adding = true;
		addError = null;
		errorAction = null;
		existingId = null;
		let createdId: string;
		try {
			if (req.source === 'github') {
				createdId = await runClone(req, live);
				if (live()) progressText = 'Finishing…';
				await appData.loadProjects();
			} else {
				progressText = 'Creating…';
				createdId = (await appData.createProject({ id: req.id, name: req.name, path: req.path })).id;
			}
		} catch (e) {
			const err = e instanceof RunError ? e : requestError(e, 'Failed to add project');
			if (live()) {
				// The dialog stays open with the reason; nothing was added, so no picker selects anything.
				addError = err.message;
				errorAction = err.action;
				existingId = err.existingId;
				if (err.action === 'advanced') advancedOpen = true;
				adding = false;
				progressText = null;
				progress = null;
				runningOp = null;
				cancelling = false;
			} else if (req.source === 'github' && err.message !== CANCELLED) {
				notify.error(`Couldn't clone ${req.name}: ${err.message}`);
			}
			return;
		}
		// Remember the Git choice (decision 3); a plain folder's ticks are not a preference.
		if (req.untick) projectSetupTargets.remember(req.untick);
		if (live()) {
			progressText = req.targets.length ? 'Starting on other workers…' : progressText;
			runningOp = null;
		}
		// Setups start after the local create succeeded. Their progress shows on
		// the project's Workers tab; a worker that refuses to start is named here.
		const refused = await startPeers(req, createdId);
		if (refused.length) notify.warning(`${req.name} was added, but setup didn't start on ${refused.join('; ')}`);
		if (!live()) {
			if (req.source === 'github') notify.success(`${req.name} is cloned and ready`);
			return;
		}
		adding = false;
		progressText = null;
		progress = null;
		onClose();
		// Use the server-assigned id — the backend may rewrite it (auto-suffix
		// on a cross-install id collision), so passing the requested id would
		// select a project that GET /projects never returns.
		onCreated?.(createdId);
	}

	async function cancelClone() {
		if (!runningOp || cancelling) return;
		cancelling = true;
		try {
			await cancelProjectSetup(runningOp);
		} catch {
			// Already finished: the poll reports how it ended.
		}
	}

	function openGitHubSettings() {
		onClose();
		layout.openFeaturePage('settings', 'category=integrations&item=github');
	}

	function openExisting(localId: string) {
		onClose();
		onCreated?.(localId);
	}

	/** Escape and the backdrop never dismiss a running create by accident. */
	function dismiss() {
		if (!adding) onClose();
	}

	function handleWindowKeydown(e: KeyboardEvent) {
		if (open && e.key === 'Escape' && !e.defaultPrevented) {
			e.preventDefault();
			dismiss();
		}
	}

	function onFieldEnter(e: KeyboardEvent) {
		if (e.key === 'Enter' && canSubmit) handleAddProject();
	}

	// Some openers sit inside a transformed or stacking parent (the new chat's
	// "in <project>" sentence), which would trap this fixed overlay under the
	// composer and pin it to that parent. Render it on <body> instead.
	function portal(node: HTMLElement) {
		document.body.appendChild(node);
		return {
			destroy() {
				node.remove();
			}
		};
	}
</script>

<svelte:window onkeydown={handleWindowKeydown} />

{#if open}
	<div class="ui-overlay ui-surface" role="dialog" aria-modal="true" aria-labelledby="add-project-title" tabindex="-1" use:portal use:focusTrap>
		<button class="ui-scrim" onclick={dismiss} tabindex="-1" aria-label="Close dialog"></button>
		<div class="ui-dialog">
			<div class="ui-dialog-header">
				<div>
					<h3 id="add-project-title" class="ui-dialog-title">New project</h3>
					<p class="ui-dialog-subtitle">Add a folder, or clone a repo from GitHub.</p>
				</div>
				<button class="ui-button ghost icon" onclick={onClose} aria-label="Close">
					<X size={16} strokeWidth={1.5} />
				</button>
			</div>

			<div class="ui-dialog-body project-body">
			{#if addError}
				<Banner
					message={addError}
					action={errorAction === 'github'
						? { label: 'GitHub settings', onClick: openGitHubSettings }
						: existingId
							? { label: 'Open it', onClick: () => openExisting(existingId!) }
							: undefined}
				/>
			{/if}
			{#if adding && source === 'github'}
				<div class="run" role="status" aria-live="polite">
					<div class="run-bar" class:indeterminate={progress == null}>
						<span style:width={progress != null ? `${progress}%` : undefined}></span>
					</div>
					<span class="hint">{progressText ?? 'Cloning…'} Closing this keeps the clone running; the project appears in your list when it's done.</span>
				</div>
			{/if}

			<fieldset class="form-set" disabled={adding}>
			<Segmented label="Source" selection="radio" stretch bind:value={source} disabled={adding}
				options={[{ value: 'folder', label: `Folder on ${selfName}`, icon: Folder }, { value: 'github', label: 'Clone from GitHub', icon: Github }]} />

			{#if source === 'folder'}
				<div class="ui-field">
					<span class="ui-label">Location</span>
					<DirectoryBrowser selectedPath={projectPath} onSelect={handleDirectorySelect} />
				</div>

				<div class="ui-field">
					<label class="ui-label" for="add-project-path">Path</label>
					<input
						id="add-project-path"
						type="text"
						class="ui-input mono"
						placeholder={isLocalMode ? '/path/to/project' : 'my-project'}
						value={projectPath}
						oninput={handlePathInput}
						onkeydown={onFieldEnter}
					/>
					<span class="path-hint">Select a folder above or type a path. New folders will be created automatically.</span>
				</div>
			{:else}
				<div class="ui-field" data-repo-picker>
					<span class="ui-label">Repository</span>
					<GitHubRepoPicker selectedUrl={cloneUrl} disabled={adding} onSelect={pickRepo} onConnect={openGitHubSettings} {isAdded} />
				</div>
				<div class="ui-field">
					<label class="ui-label" for="add-project-url">Or paste a clone URL</label>
					<input
						id="add-project-url"
						type="text"
						class="ui-input mono"
						placeholder="https://github.com/owner/repo.git"
						value={cloneUrl}
						oninput={(e) => setCloneUrl((e.target as HTMLInputElement).value)}
						onkeydown={onFieldEnter}
					/>
					{#if alreadyAdded}
						<span class="field-note" data-testid="already-added">
							Already added as <strong>{alreadyAdded.name}</strong>.
							{#if alreadyAdded.localId}
								{@const localId = alreadyAdded.localId}
								<button type="button" class="link-btn" onclick={() => openExisting(localId)}>Open it</button>
							{:else}
								Set it up on this worker from the project's Workers tab.
							{/if}
						</span>
					{:else if cloneDestination}
						<span class="path-hint" data-testid="clone-destination">Clones to <code>{cloneDestination}</code> on {selfName}.</span>
					{/if}
				</div>
			{/if}

			<div class="ui-field">
				<label class="ui-label" for="add-project-name">Display name</label>
				<input
					id="add-project-name"
					type="text"
					class="ui-input"
					placeholder="My project"
					bind:value={projectName}
					aria-invalid={nameError ? 'true' : undefined}
					aria-describedby={nameError ? 'add-project-name-error' : undefined}
					oninput={() => { nameManuallyEdited = true; }}
					onkeydown={onFieldEnter}
				/>
				{#if nameError}<span class="field-error" id="add-project-name-error">{nameError}</span>{/if}
			</div>

			<details class="advanced" bind:open={advancedOpen}>
				<summary>Advanced{#if !advancedOpen && projectId.trim()}<span class="hint mono">{` · ID ${projectId.trim()}`}</span>{/if}</summary>
				<div class="ui-field">
					<label class="ui-label" for="add-project-id">Project ID</label>
					<input
						id="add-project-id"
						type="text"
						class="ui-input mono"
						placeholder="my-project"
						bind:value={projectId}
						aria-invalid={idError ? 'true' : undefined}
						aria-describedby="add-project-id-help"
						oninput={() => { idManuallyEdited = true; }}
						onkeydown={onFieldEnter}
					/>
					{#if idError}
						<span class="field-error" id="add-project-id-help">{idError}</span>
					{:else}
						<span class="path-hint" id="add-project-id-help">Made from the name. Lowercase letters, numbers and dashes, up to {ID_MAX}{source === 'github' ? '; also the folder name' : ''}.</span>
					{/if}
				</div>
			</details>

			{#if canSetUpElsewhere}
				<div class="wpick-set" role="group" aria-labelledby="add-project-workers">
					<div class="sect" id="add-project-workers">Set it up on</div>
					<div class="wpick">
						<div class="wpick-row locked">
							<span class="box lock" aria-hidden="true"><Check size={11} strokeWidth={3} /></span>
							<span class="wpick-name">{selfName} <span class="hint">· this worker</span></span>
							{#if source === 'github' ? cloneDestination : projectPath.trim()}
								<span class="hint mono self-path">{source === 'github' ? cloneDestination : projectPath.trim()}</span>
							{/if}
						</div>
						{#each others as w (w.id)}
							{@const on = ticked.includes(w.id)}
							<div class="wpick-row" class:dim={!w.online}>
								<Checkbox block checked={on} disabled={adding} label={workerDisplayName(w.device_name)} onchange={() => toggleTarget(w.id)}><span class="worker-copy">
								<span class="wpick-name">{workerDisplayName(w.device_name)}{#if !w.online}<span class="hint">&nbsp;· offline</span>{/if}</span>
								<span class="hint" class:mono={w.online && source === 'github'}>
									{#if !w.online}
										runs when it's back
									{:else if source === 'github'}
										…/{folderLeaf}
									{:else}
										uses an existing {folderLeaf} folder there, or creates an empty one; files aren't copied
									{/if}
								</span>
                            </span></Checkbox>
							</div>
						{/each}
					</div>
					<p class="hint foot">
						{#if source === 'github'}
							{#if !ticksTouched}Online workers start ticked for a repository.{/if}
							Each worker clones with its own GitHub sign-in, into its workspace folder. You can add or remove workers later in the project's Workers tab.
						{:else}
							{#if !ticksTouched}Other workers start unticked for a folder.{/if}
							A Git repo with a remote is cloned on each worker instead. You can add or remove workers later in the project's Workers tab.
						{/if}
					</p>
				</div>
			{/if}
			</fieldset>

			</div>
			<div class="ui-dialog-footer">
				{#if adding && runningOp}
					<button class="ui-button" onclick={cancelClone} disabled={cancelling}>{cancelling ? 'Cancelling…' : 'Cancel clone'}</button>
				{:else}
					<button class="ui-button" onclick={onClose} disabled={adding}>Cancel</button>
				{/if}
				<button class="ui-button primary" disabled={!canSubmit} aria-busy={adding} onclick={handleAddProject}>{submitLabel}</button>
			</div>
		</div>
	</div>
{/if}

<style>
 .project-body { display: grid; gap: 12px; }
 .form-set { display: grid; gap: 12px; border: 0; margin: 0; padding: 0; min-width: 0; }

 .path-hint, .hint { font-size: 12px; color: var(--muted-foreground); }
 .field-error { font-size: 12px; color: var(--destructive); }
 .field-note { font-size: 12px; color: var(--foreground); }
 .mono, code { font-family: var(--font-mono); }
 code { overflow-wrap: anywhere; }
 .link-btn { background: none; border: 0; padding: 0; color: var(--primary); font: inherit; cursor: pointer; text-decoration: underline; }
 .advanced > summary { font-size: 12px; color: var(--muted-foreground); cursor: pointer; padding: 4px 0; overflow-wrap: anywhere; }
 .advanced[open] > summary { margin-bottom: 8px; }
 .run { display: grid; gap: 6px; }
 .run-bar { height: 4px; border-radius: 999px; background: var(--muted); overflow: hidden; }
 .run-bar > span { display: block; height: 100%; background: var(--primary); transition: width 200ms ease; }
 .run-bar.indeterminate > span { width: 30%; animation: run-slide 1.2s ease-in-out infinite; }
 @keyframes run-slide { from { transform: translateX(-100%); } to { transform: translateX(340%); } }
 @media (prefers-reduced-motion: reduce) { .run-bar.indeterminate > span { animation: none; width: 100%; opacity: 0.4; } }
 .wpick-set { min-width: 0; }
 .sect { font-size: 12px; color: var(--muted-foreground); margin-bottom: 6px; }
 .wpick-row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; min-height: 40px; padding: 8px 0; border-bottom: 1px solid var(--border); }
 .wpick-name { min-width: 0; overflow-wrap: anywhere; }
 .self-path { margin-left: auto; min-width: 0; overflow-wrap: anywhere; }
 .worker-copy { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
 .worker-copy > .hint:last-child { margin-left: auto; }
 .box { display: grid; place-items: center; width: 16px; height: 16px; border: 1px solid var(--border); border-radius: 4px; background: var(--muted); color: var(--foreground); }
 .foot { margin: 8px 0 0; }
</style>
