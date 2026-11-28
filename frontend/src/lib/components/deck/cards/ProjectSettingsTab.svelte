<script lang="ts">
	/**
	 * The Settings tab: the project's identity and its one danger zone, as rows
	 * of the page (the same anatomy an agent's Profile tab has). Removal is per
	 * worker (this worker's copy here, another's from its row); "Forget
	 * project" takes it off the Projects list, which only makes sense once no
	 * worker holds a copy.
	 */
	import { RowGroup, SettingRow } from '$lib/components/layout/view';
	import { isAdmin } from '$lib/stores/auth.svelte';
	import { forgetProject, type ProjectKind, type ProjectRef } from '$lib/api/project-workers';
	import ProjectGeneralTab from './ProjectGeneralTab.svelte';
	import ProjectLocalRemoval from './ProjectLocalRemoval.svelte';
	import './project-tabs.css';

	interface Props {
		projectName: string;
		/** This worker's id for the project; null when it isn't set up here. */
		localProjectId: string | null;
		/** The project across workers; null without the worker overview. */
		projectRef: ProjectRef | null;
		kind: ProjectKind | null;
		/** A Git project: it has a Repository row, a missing remote included. */
		isGit: boolean;
		remote: string | null;
		selfName: string;
		/** No worker holds a copy any more. */
		orphaned: boolean;
		/** The Workers tab shows this project; without it, this worker's folder is changed here. */
		workerTable?: boolean;
		/** Open this worker's row in the Workers tab (folders are changed there). */
		onOpenSelf?: () => void;
		/**
		 * This worker's row in the Workers tab can remove its copy: the danger
		 * zone sends the owner there instead of offering a second removal.
		 */
		onRemoveSelf?: () => void;
		/** Open "Change repository on all workers" (the page owns the dialog). */
		onChangeRepository?: () => void;
		/** The project left the list or this worker. */
		onRemoved: () => void;
	}

	let { projectName, localProjectId, projectRef, kind, isGit, remote, selfName, orphaned, workerTable = false, onOpenSelf, onRemoveSelf, onChangeRepository, onRemoved }: Props = $props();

	let forgetBusy = $state(false);
	let forgetError = $state<string | null>(null);

	async function forget() {
		if (!projectRef || !orphaned) return;
		forgetBusy = true;
		forgetError = null;
		try {
			await forgetProject(projectRef);
			onRemoved();
		} catch (e) {
			const err = e as { detail?: unknown; message?: string };
			forgetError = `Couldn't remove it from the list: ${typeof err.detail === 'string' ? err.detail : err.message || 'unknown error'}`;
		} finally {
			forgetBusy = false;
		}
	}
</script>

<!-- Every Git project offers the change, a missing remote too (it takes a URL). -->
{#snippet repository()}
	{#if isGit}
		<SettingRow label="Repository" hint={remote || 'Not reported'} hintMono={!!remote} wrapHint>
			{#if $isAdmin && projectRef && onChangeRepository}
				<button type="button" class="st-btn" title="Change the repository on all workers"
					aria-label={remote ? 'Change the repository on all workers' : 'Set the repository on all workers'} onclick={onChangeRepository}>{remote ? 'Change…' : 'Set…'}</button>
			{/if}
		</SettingRow>
	{/if}
{/snippet}

<div data-ui-project-settings>
	{#if localProjectId}
		<ProjectGeneralTab projectId={localProjectId} {selfName} editFolderHere={!workerTable} onOpenFolders={workerTable ? onOpenSelf : undefined} {repository} />
	{:else}
		<RowGroup title="Identity">
			<SettingRow label="Name" hint={projectName} />
			{@render repository()}
			<SettingRow label="Version control" hint={kind === 'git' ? 'Git repository' : kind === 'folder' ? 'Plain folder' : 'Not reported'} />
			<SettingRow label="Folders" wrapHint hint={`Not on ${selfName}. Each worker has its own folder; change one from its row in Workers.`}>
				{#if onOpenSelf}<button class="st-btn" onclick={onOpenSelf}>Open {selfName}</button>{/if}
			</SettingRow>
		</RowGroup>
	{/if}
	{#if $isAdmin && (localProjectId || projectRef)}
		<RowGroup title="Danger zone" tone="danger">
			{#if localProjectId && onRemoveSelf}
				<SettingRow label="Remove project" wrapHint hint={`Removal is done per worker and leaves files on disk. Remove it from ${selfName} in its row in Workers, where the chats it removes are counted first.`}>
					<button class="st-btn danger" onclick={onRemoveSelf}>Remove from {selfName}…</button>
				</SettingRow>
			{:else if localProjectId}
				<ProjectLocalRemoval projectId={localProjectId} {projectName} where={projectRef ? selfName : null} onRemoved={onRemoved} />
				{#if projectRef}<p class="st-footnote">Removal is done per worker and leaves files on disk. Remove it from another worker from that worker's row.</p>{/if}
			{/if}
			{#if projectRef}
				<SettingRow label="Forget project" wrapHint
					hint={orphaned ? 'No worker has it any more. Takes it off the Projects list; its chats keep it, nothing on disk is touched, and it comes back if you set it up again.' : 'Available once no worker holds a copy. Remove it from each worker first.'}>
					<button class="st-btn danger" disabled={!orphaned || forgetBusy} onclick={forget}>{forgetBusy ? 'Removing…' : 'Forget project'}</button>
				</SettingRow>
				{#if forgetError}<p class="project-error" role="alert">{forgetError}</p>{/if}
			{/if}
		</RowGroup>
	{/if}
</div>
