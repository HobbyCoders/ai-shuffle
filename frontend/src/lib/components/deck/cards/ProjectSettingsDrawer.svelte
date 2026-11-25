<script lang="ts">
	/**
	 * Project settings: a side drawer with the project's identity and its one
	 * danger zone. Removal is per worker (this worker's copy here, another's
	 * from its row); "Forget project" takes it off the Projects list, which
	 * only makes sense once no worker holds a copy.
	 */
	import { FolderGit2, FolderOpen } from 'lucide-svelte';
	import { RowGroup, SettingRow } from '$lib/components/layout/view';
	import { isAdmin } from '$lib/stores/auth.svelte';
	import { forgetProject, type ProjectKind, type ProjectRef } from '$lib/api/project-workers';
	import ProjectSheet from './ProjectSheet.svelte';
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
		selfName: string;
		/** No worker holds a copy any more. */
		orphaned: boolean;
		/** Open this worker's row in the worker table (folders are changed there). */
		onOpenSelf?: () => void;
		/** The project left the list or this worker. */
		onRemoved: () => void;
		onClose: () => void;
	}

	let { projectName, localProjectId, projectRef, kind, selfName, orphaned, onOpenSelf, onRemoved, onClose }: Props = $props();

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

<ProjectSheet side title="Project settings" subtitle={projectName} {onClose}>
	<div data-ui-project-settings>
		{#if localProjectId}
			<ProjectGeneralTab projectId={localProjectId} {selfName} onOpenFolders={onOpenSelf ? () => { onClose(); onOpenSelf?.(); } : undefined} />
		{:else}
			<RowGroup title="Identity">
				<SettingRow label="Name" hint={projectName} />
				<SettingRow label="Version control" hint={kind === 'git' ? 'Git repository' : kind === 'folder' ? 'Plain folder' : 'Not reported'}>
					{#if kind === 'git'}<FolderGit2 aria-hidden="true" />{:else}<FolderOpen aria-hidden="true" />{/if}
				</SettingRow>
				<SettingRow label="Folders" wrapHint hint={`Not on ${selfName}. Each worker has its own folder; change one from its row in Workers.`}>
					{#if onOpenSelf}<button class="st-btn" onclick={() => { onClose(); onOpenSelf?.(); }}>Open {selfName}</button>{/if}
				</SettingRow>
			</RowGroup>
		{/if}
		{#if $isAdmin && (localProjectId || projectRef)}
			<RowGroup title="Danger zone" tone="danger">
				{#if localProjectId}
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
</ProjectSheet>
