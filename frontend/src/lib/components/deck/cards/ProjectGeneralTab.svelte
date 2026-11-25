<script lang="ts">
	/**
	 * Project identity (Project settings): name, id, version control and where
	 * its folders are. Each worker has its own folder, changed from that
	 * worker's row; the group is the chip beside the project's name.
	 */
	import { untrack } from 'svelte';
	import { getGitStatus } from '$lib/api/git';
	import { git } from '$lib/stores/git.svelte';
	import InitRepoModal from '$lib/components/git/InitRepoModal.svelte';
	import { appData, projects } from '$lib/stores/app-data.svelte';
	import { Check, Copy } from 'lucide-svelte';
	import { RowGroup, SettingRow, InlineBanner } from '$lib/components/layout/view';
	import TextField from '$lib/components/settings/rows/TextField.svelte';
	import './project-tabs.css';

	interface Props {
		projectId: string;
		/** Open this worker's row in the worker table; without it the folder is shown here. */
		onOpenFolders?: () => void;
		selfName?: string;
	}

	let { projectId, onOpenFolders, selfName = 'this worker' }: Props = $props();

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
		{#if canInitialize}
			<SettingRow label="Version control" hint="This folder is not a Git repository."><button class="st-btn" onclick={() => (showInitRepoModal = true)}>Initialize Git repository</button></SettingRow>
		{/if}
		{#if project}
			{#if onOpenFolders}
				<SettingRow label="Folders" wrapHint hint="Each worker has its own. Change one from its row in Workers.">
					<button class="st-btn" onclick={onOpenFolders}>Open {selfName}</button>
				</SettingRow>
			{:else}
				<SettingRow label="Folder on {selfName}" hint={project.path} hintMono wrapHint />
			{/if}
		{/if}
	</RowGroup>
{/if}
