<script lang="ts">
	/** Project identity, location and grouping, using Settings rows. */
	import { tick, untrack } from 'svelte';
	import { getGitStatus } from '$lib/api/git';
	import { git } from '$lib/stores/git.svelte';
	import InitRepoModal from '$lib/components/git/InitRepoModal.svelte';
	import { appData, projects } from '$lib/stores/app-data.svelte';
	import { groups as groupsStore } from '$lib/stores/groups.svelte';
	import { FolderGit2, Check, Plus, Trash2, Loader2 } from 'lucide-svelte';
	import { RowGroup, SettingRow, SelectField, InlineBanner } from '$lib/components/layout/view';
	import TextField from '$lib/components/settings/rows/TextField.svelte';
	import './project-tabs.css';

	interface Props {
		projectId: string;
	}

	let { projectId }: Props = $props();

	// Get current project data
	const project = $derived($projects.find(p => p.id === projectId));

	// Groups data
	const groups = $derived($groupsStore);

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

	// Path editing state
	let editPath = $state('');
	let pathLoading = $state(false);
	let pathError = $state<string | null>(null);
	let pathDirty = $derived(editPath !== (project?.path ?? ''));

	// Group creation state
	let creatingGroup = $state(false);
	let newGroupName = $state('');

	let previousProject: typeof project;
	let remoteChange = $state('');
	$effect(() => {
		const fresh = project;
		untrack(() => {
			const previous = previousProject;
			const nameClean = !nameLoading && editName === (previous?.name ?? '');
			const pathClean = !pathLoading && editPath === (previous?.path ?? '');
			if (fresh) {
				if (!previous || nameClean) editName = fresh.name;
				if (!previous || pathClean) editPath = fresh.path;
				if (previous && ((!nameClean && fresh.name !== previous.name) || (!pathClean && fresh.path !== previous.path))) remoteChange = 'This project changed elsewhere. Your unsaved fields are kept here.';
			} else if (previous) remoteChange = 'This project was deleted elsewhere. Your unsaved fields are kept below.';
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

	async function handleSavePath() {
		if (!pathDirty || pathLoading || !editPath.trim()) return;
		pathLoading = true;
		pathError = null;
		try {
			await appData.updateProject(projectId, { path: editPath.trim() });
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			pathError = err.detail || err.message || 'Failed to update path';
		} finally {
			pathLoading = false;
		}
	}

	// Group handlers
	function handleGroupChange(v: string) {
		if (v) {
			groupsStore.assignToGroup('projects', projectId, v);
		} else {
			groupsStore.removeFromGroup('projects', projectId);
		}
	}

	async function startGroupCreate() {
		creatingGroup = true;
		newGroupName = '';
		await tick();
		document.querySelector<HTMLInputElement>('input[aria-label="Group name"]')?.focus();
	}

	function confirmGroupCreate() {
		const name = newGroupName.trim();
		if (name) {
			groupsStore.createGroup('projects', name);
			groupsStore.assignToGroup('projects', projectId, name);
		}
		creatingGroup = false;
		newGroupName = '';
	}

	function cancelGroupCreate() {
		creatingGroup = false;
		newGroupName = '';
	}

	function deleteCurrentGroup() {
		const g = groups?.projects?.assignments?.[projectId];
		if (g) {
			groupsStore.removeFromGroup('projects', projectId);
			groupsStore.deleteGroup('projects', g);
		}
	}
</script>
{#if showInitRepoModal}
	<InitRepoModal open={showInitRepoModal} {projectId} projectName={project?.name ?? ''} onClose={() => (showInitRepoModal = false)} onInitialized={repositoryInitialized} />
{/if}
{#if remoteChange}<InlineBanner>{remoteChange}</InlineBanner>{/if}
{#if project || remoteChange}
	<RowGroup title="Identity">
		{#if canInitialize}
			<SettingRow label="Version control" hint="This folder is not a Git repository."><button class="st-btn" onclick={() => (showInitRepoModal = true)}>Initialize Git repository</button></SettingRow>
		{/if}
		<SettingRow label={project ? 'Name' : 'Unsaved name'} hint="Display name shown across the app.">
			<form class="project-controls" onsubmit={(e) => { e.preventDefault(); handleSaveName(); }}>
				<TextField onkeydown={(e) => { if (e.key === 'Enter') { e.preventDefault(); handleSaveName(); } }} bind:value={editName} oninput={(value) => (editName = value)} placeholder="Project name" />
				{#if nameDirty && project}<button class="st-btn icon" disabled={nameLoading || !editName.trim()} title="Save name" aria-label="Save name"><Check /></button>{/if}
			</form>
			{#if nameError}<span class="project-error" role="alert">{nameError}</span>{/if}
		</SettingRow>
		{#if project}<SettingRow label="Project ID" hint="Unique identifier (read-only)."><span class="project-muted">{project.id}</span></SettingRow>{/if}
		<SettingRow label={project ? 'Path' : 'Unsaved path'} hint="Filesystem location of this project.">
			{#snippet expanded()}
				<form class="project-controls" onsubmit={(e) => { e.preventDefault(); handleSavePath(); }}>
					<TextField onkeydown={(e) => { if (e.key === 'Enter') { e.preventDefault(); handleSavePath(); } }} bind:value={editPath} oninput={(value) => (editPath = value)} placeholder="Project path" mono width={600} label="Project path" />
					{#if pathDirty && project}<button class="st-btn icon" disabled={pathLoading || !editPath.trim()} title="Save path" aria-label="Save path"><Check /></button>{/if}
				</form>
				{#if pathError}<span class="project-error" role="alert">{pathError}</span>{/if}
			{/snippet}
		</SettingRow>
		{#if project}
			<SettingRow label="Group" hint="Organize related projects together.">
				<SelectField value={groups?.projects?.assignments?.[projectId] ?? ''} options={[{ value: '', label: 'No group' }, ...(groups?.projects?.groups ?? []).map(g => ({ value: g.name, label: g.name }))]} onchange={handleGroupChange} />
				{#if creatingGroup}
					<form class="project-controls" onsubmit={(e) => { e.preventDefault(); confirmGroupCreate(); }}>
						<TextField onkeydown={(e) => { if (e.key === 'Enter') { e.preventDefault(); confirmGroupCreate(); } else if (e.key === 'Escape') { e.preventDefault(); cancelGroupCreate(); } }} bind:value={newGroupName} oninput={(v) => (newGroupName = v)} placeholder="Group name…" label="Group name" />
						<button class="st-btn icon" title="Create" aria-label="Create"><Check /></button>
						<button type="button" class="st-btn" onclick={cancelGroupCreate}>Cancel</button>
					</form>
				{:else}
					<button class="st-btn icon" onclick={startGroupCreate} title="New group" aria-label="New group"><Plus /></button>
					{#if groups?.projects?.assignments?.[projectId]}<button class="st-btn icon danger" onclick={deleteCurrentGroup} title="Delete group" aria-label="Delete group"><Trash2 /></button>{/if}
				{/if}
			</SettingRow>
		{/if}
	</RowGroup>
{/if}
