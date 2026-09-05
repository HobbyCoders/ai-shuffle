<script lang="ts">
	/**
	 * ProjectGeneralTab - General settings tab for project detail view
	 *
	 * Studio-v2 grouped-field design (.set-group / .set-row), mirroring the
	 * Profile editor (ProfileForm general tab). Field primitives come from
	 * project-tabs.css, gated under the `.ptab` wrapper.
	 *
	 * - Editable project name + path (inline save)
	 * - Read-only project ID
	 * - Group assignment with select + plus/delete buttons + new group input
	 */
	import { untrack } from 'svelte';
	import { appData, projects } from '$lib/stores/app-data.svelte';
	import { groups as groupsStore } from '$lib/stores/groups.svelte';
	import { FolderGit2, Check, Plus, Trash2, Loader2 } from 'lucide-svelte';
	import './project-tabs.css';

	interface Props {
		projectId: string;
	}

	let { projectId }: Props = $props();

	// Get current project data
	const project = $derived($projects.find(p => p.id === projectId));

	// Groups data
	const groups = $derived($groupsStore);

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
	function handleGroupChange(e: Event) {
		const v = (e.target as HTMLSelectElement).value;
		if (v) {
			groupsStore.assignToGroup('projects', projectId, v);
		} else {
			groupsStore.removeFromGroup('projects', projectId);
		}
	}

	function startGroupCreate() {
		creatingGroup = true;
		newGroupName = '';
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

{#if remoteChange}<p role="status" style="flex: none; height: auto;" class="px-4 py-2 text-sm text-muted-foreground">{remoteChange}</p>{/if}
{#if !project && remoteChange}
	<label class="block p-4">Unsaved name<input class="inp" bind:value={editName} /></label>
	<label class="block p-4">Unsaved path<input class="inp" bind:value={editPath} /></label>
{/if}
{#if project}
	<div class="ptab">
		<div class="set-group">
			<div class="set-grouphead"><h3><FolderGit2 />Identity</h3></div>

			<!-- Project Name -->
			<div class="set-row">
				<div class="lab"><h4>Name</h4><p>Display name shown across the app.</p></div>
				<div class="ctl ctl-col">
					<div class="grp-row">
						<input
							type="text"
							bind:value={editName}
							class="inp"
							placeholder="Project name"
							onkeydown={(e) => { if (e.key === 'Enter') handleSaveName(); }}
						/>
						{#if nameDirty}
							<button class="icon-btn" onclick={handleSaveName} disabled={nameLoading || !editName.trim()} title="Save name">
								{#if nameLoading}<Loader2 size={14} class="animate-spin" />{:else}<Check size={14} />{/if}
							</button>
						{/if}
					</div>
					{#if nameError}<p class="fld-err">{nameError}</p>{/if}
				</div>
			</div>

			<!-- Project ID -->
			<div class="set-row">
				<div class="lab"><h4>Project ID</h4><p>Unique identifier (read-only).</p></div>
				<div class="ctl">
					<input type="text" class="inp mono" value={project.id} readonly />
				</div>
			</div>

			<!-- Project Path -->
			<div class="set-row stack">
				<div class="lab"><h4>Path</h4><p>Filesystem location of this project.</p></div>
				<div class="ctl ctl-col">
					<div class="grp-row">
						<input
							type="text"
							bind:value={editPath}
							class="inp mono"
							placeholder="Project path"
							onkeydown={(e) => { if (e.key === 'Enter') handleSavePath(); }}
						/>
						{#if pathDirty}
							<button class="icon-btn" onclick={handleSavePath} disabled={pathLoading || !editPath.trim()} title="Save path">
								{#if pathLoading}<Loader2 size={14} class="animate-spin" />{:else}<Check size={14} />{/if}
							</button>
						{/if}
					</div>
					{#if pathError}<p class="fld-err">{pathError}</p>{/if}
				</div>
			</div>

			<!-- Group Assignment -->
			<div class="set-row">
				<div class="lab"><h4>Group</h4><p>Organize related projects together.</p></div>
				<div class="ctl ctl-col">
					<div class="grp-row">
						<select
							class="sel"
							value={groups?.projects?.assignments?.[projectId] ?? ''}
							onchange={handleGroupChange}
						>
							<option value="">No group</option>
							{#each groups?.projects?.groups ?? [] as group}
								<option value={group.name}>{group.name}</option>
							{/each}
						</select>
						{#if creatingGroup}
							<button type="button" class="icon-btn" onclick={confirmGroupCreate} title="Create">
								<Check size={14} />
							</button>
						{:else}
							<button type="button" class="icon-btn" onclick={startGroupCreate} title="New group">
								<Plus size={14} />
							</button>
							{#if (groups?.projects?.assignments?.[projectId] ?? "")}
								<button type="button" class="icon-btn danger" onclick={deleteCurrentGroup} title="Delete group">
									<Trash2 size={14} />
								</button>
							{/if}
						{/if}
					</div>
					{#if creatingGroup}
						<!-- svelte-ignore a11y_autofocus -->
						<input
							type="text"
							class="inp"
							placeholder="Group name…"
							bind:value={newGroupName}
							onkeydown={(e) => {
								if (e.key === 'Enter') { e.preventDefault(); confirmGroupCreate(); }
								if (e.key === 'Escape') { e.preventDefault(); cancelGroupCreate(); }
							}}
							autofocus
						/>
					{/if}
				</div>
			</div>
		</div>
	</div>
{/if}

<style>
	:global(.animate-spin) {
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		from { transform: rotate(0deg); }
		to { transform: rotate(360deg); }
	}
</style>
