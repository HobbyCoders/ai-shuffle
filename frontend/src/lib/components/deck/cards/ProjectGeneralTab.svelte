<script lang="ts">
	/**
	 * Project identity (the Settings tab): name, id, repository, group, version
	 * control and where its folders are. Each worker has its own folder, changed
	 * from that worker's row.
	 */
	import { tick, untrack, type Snippet } from 'svelte';
	import { getGitStatus } from '$lib/api/git';
	import { git } from '$lib/stores/git.svelte';
	import InitRepoModal from '$lib/components/git/InitRepoModal.svelte';
	import { appData, projects } from '$lib/stores/app-data.svelte';
	import { Check, Copy } from 'lucide-svelte';
	import { RowGroup, SettingRow, InlineBanner } from '$lib/components/layout/view';
	import TextField from '$lib/components/settings/rows/TextField.svelte';
	import DirectoryBrowser from '$lib/components/DirectoryBrowser.svelte';
	import { isAdmin } from '$lib/stores/auth.svelte';
	import ProjectGroupRow from './ProjectGroupRow.svelte';
	import './project-tabs.css';

	interface Props {
		projectId: string;
		/** Open this worker's row in the worker table; without it the folder is shown here. */
		onOpenFolders?: () => void;
		/**
		 * No worker table can show this project (no overview row, or this worker's
		 * Workers API is unavailable): its folder is changed here, through the
		 * local update, which the server checks like the Workers endpoint.
		 */
		editFolderHere?: boolean;
		selfName?: string;
		/** The project's Repository row (it belongs to every worker, so the page owns it). */
		repository?: Snippet;
	}

	let { projectId, onOpenFolders, editFolderHere = false, selfName = 'this worker', repository }: Props = $props();

	// Get current project data
	const project = $derived($projects.find(p => p.id === projectId));

	// Initialization belongs to project management, not the chat's Git header.
	let canInitialize = $state(false);
	let showInitRepoModal = $state(false);
	let capabilityVersion = $state(0);
	$effect(() => {
		const id = projectId;
		void capabilityVersion;
		let cancelled = false;
		canInitialize = false;
		getGitStatus(id).then(status => {
			if (!cancelled) canInitialize = status.is_git_repo === false;
		}).catch(() => { /* An unavailable checkout is not an initialization offer. */ });
		return () => { cancelled = true; };
	});
	function repositoryInitialized() {
		capabilityVersion++;
		void git.refreshStatus();
	}

	// Name editing state
	let editName = $state('');
	let nameLoading = $state(false);
	let nameError = $state<string | null>(null);
	let nameDirty = $derived(editName !== (project?.name ?? ''));

	let previousProject: typeof project;
	let remoteChange = $state('');
	$effect(() => {
		const fresh = project;
		untrack(() => {
			const previous = previousProject;
			const nameClean = !nameLoading && editName === (previous?.name ?? '');
			if (fresh) {
				if (!previous || nameClean) editName = fresh.name;
				if (previous && !nameClean && fresh.name !== previous.name) remoteChange = 'This project changed elsewhere. Your unsaved name is kept here.';
			} else if (previous) remoteChange = 'This project was deleted elsewhere. Your unsaved name is kept below.';
			previousProject = fresh;
		});
	});

	async function handleSaveName() {
		if (!nameDirty || nameLoading || !editName.trim()) return;
		nameLoading = true;
		nameError = null;
		try {
			await appData.updateProject(projectId, { name: editName.trim() });
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			nameError = err.detail || err.message || 'Failed to update name';
		} finally {
			nameLoading = false;
		}
	}

	// Folder fallback (editFolderHere): the folder and revision it opened on, so a
	// change made elsewhere meanwhile blocks the save instead of being
	// overwritten. A catalog that already shows the change blocks it here; one
	// that does not yet is refused by the server (409, the Workers editor's
	// revision check), and the refreshed catalog then shows it here too.
	let folderDraft = $state<{ opened: string; revision: string | null; folder: string; browsing: boolean; saving: boolean; error: string | null } | null>(null);
	const folderStale = $derived(!!folderDraft && !!project && project.path !== folderDraft.opened);
	$effect(() => {
		// Only the name or settings changed (this tab's own rename included): the
		// folder it opened on is still current, so its newer revision is too.
		const fresh = project;
		untrack(() => {
			if (folderDraft && fresh && fresh.path === folderDraft.opened && fresh.folder_revision) folderDraft.revision = fresh.folder_revision;
		});
	});
	let folderForm = $state<HTMLFormElement | null>(null);
	function openFolderDraft() {
		if (!project) return;
		folderDraft = { opened: project.path, revision: project.folder_revision ?? null, folder: project.path, browsing: false, saving: false, error: null };
	}
	async function saveFolderDraft() {
		const draft = folderDraft;
		if (!draft || !project || draft.saving || folderStale) return;
		const next = draft.folder.trim();
		if (!next || next === project.path) return;
		draft.saving = true;
		draft.error = null;
		try {
			await appData.updateProject(projectId, draft.revision ? { path: next, expected_revision: draft.revision } : { path: next });
			if (folderDraft === draft) folderDraft = null;
		} catch (e: unknown) {
			const err = e as { detail?: unknown; message?: string; status?: number };
			draft.error = typeof err.detail === 'string' ? err.detail : err.message || "Couldn't change the folder. Check the current folder before retrying.";
			// Refused as stale: this window's catalog is behind. Read it again so
			// the folder changed elsewhere shows and locks the editor.
			if (err.status === 409) void appData.loadProjects();
			// Saving disabled the focused control; put the keyboard back on the field
			// to correct the folder.
			draft.saving = false;
			await tick();
			if (folderDraft === draft) folderForm?.querySelector<HTMLInputElement>('input')?.focus();
		} finally {
			draft.saving = false;
		}
	}

	let copied = $state(false);
	async function copyId() {
		if (!project) return;
		try {
			await navigator.clipboard.writeText(project.id);
			copied = true;
			setTimeout(() => (copied = false), 1500);
		} catch { /* The id stays selectable. */ }
	}
</script>
{#if showInitRepoModal}
	<InitRepoModal open={showInitRepoModal} {projectId} projectName={project?.name ?? ''} onClose={() => (showInitRepoModal = false)} onInitialized={repositoryInitialized} />
{/if}
{#if remoteChange}<InlineBanner>{remoteChange}</InlineBanner>{/if}
{#if project || remoteChange}
	<RowGroup title="Identity">
		<SettingRow label={project ? 'Name' : 'Unsaved name'} hint="Display name shown across the app.">
			<form class="project-controls" onsubmit={(e) => { e.preventDefault(); handleSaveName(); }}>
				<TextField onkeydown={(e) => { if (e.key === 'Enter') { e.preventDefault(); handleSaveName(); } }} bind:value={editName} oninput={(value) => (editName = value)} placeholder="Project name" label="Project name" />
				{#if nameDirty && project}<button class="st-btn icon" disabled={nameLoading || !editName.trim()} title="Save name" aria-label="Save name"><Check /></button>{/if}
			</form>
			{#if nameError}<span class="project-error" role="alert">{nameError}</span>{/if}
		</SettingRow>
		{#if project}
			<SettingRow label="Project ID" hint={project.id} hintMono>
				<button class="st-btn icon" onclick={copyId} title={copied ? 'Copied' : 'Copy project ID'} aria-label={copied ? 'Copied' : 'Copy project ID'}>{#if copied}<Check />{:else}<Copy />{/if}</button>
			</SettingRow>
		{/if}
		{@render repository?.()}
		{#if project}<ProjectGroupRow {projectId} projectName={project.name} />{/if}
		{#if canInitialize}
			<SettingRow label="Version control" hint="This folder is not a Git repository."><button class="st-btn" onclick={() => (showInitRepoModal = true)}>Initialize Git repository</button></SettingRow>
		{/if}
		{#if project}
			{#if onOpenFolders}
				<SettingRow label="Folders" wrapHint hint="Each worker has its own. Change one from its row in Workers.">
					<button class="st-btn" onclick={onOpenFolders}>Open {selfName}</button>
				</SettingRow>
			{:else if editFolderHere && $isAdmin}
				<SettingRow label="Folder on {selfName}" hint={project.path} hintMono wrapHint>
					{#if !folderDraft}<button class="st-btn" onclick={openFolderDraft}>Change folder</button>{/if}
				</SettingRow>
				{#if folderDraft}
					{@const draft = folderDraft}
					<SettingRow label="Change folder" wrapHint hint={`Choose an existing folder on ${selfName}. No files are moved or copied. New chats and the next turn in chats without worktrees use the new folder. Existing worktrees stay where they are; keep their original checkout in place.`}>
						{#snippet expanded()}
							<form class="flex flex-col gap-2" data-local-folder-editor bind:this={folderForm} onsubmit={(e) => { e.preventDefault(); void saveFolderDraft(); }}>
								<div class="project-controls">
									<TextField bind:value={draft.folder} width="full" mono pathDisplay label={`New folder on ${selfName}`} disabled={draft.saving || folderStale} />
									<button type="button" class="st-btn" disabled={draft.saving || folderStale} aria-expanded={draft.browsing} onclick={() => (draft.browsing = !draft.browsing)}>Browse</button>
								</div>
								{#if draft.browsing && !folderStale && !draft.saving}
									<DirectoryBrowser selectedPath={draft.folder} onSelect={(path) => { draft.folder = path; draft.browsing = false; }} />
								{/if}
								{#if folderStale}<p class="project-error" role="alert">The folder changed elsewhere. Cancel and reopen to edit the current folder.</p>{/if}
								{#if draft.error}<p class="project-error" role="alert">{draft.error}</p>{/if}
								<div class="project-controls">
									<button type="button" class="st-btn" disabled={draft.saving} onclick={() => (folderDraft = null)}>Cancel</button>
									<button type="submit" class="st-btn primary" disabled={draft.saving || folderStale || !draft.folder.trim() || draft.folder.trim() === project.path}>{draft.saving ? 'Saving…' : 'Save folder'}</button>
								</div>
							</form>
						{/snippet}
					</SettingRow>
				{/if}
			{:else}
				<SettingRow label="Folder on {selfName}" hint={project.path} hintMono wrapHint />
			{/if}
		{/if}
	</RowGroup>
{/if}
