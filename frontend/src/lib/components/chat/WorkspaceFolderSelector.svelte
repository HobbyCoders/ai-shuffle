<script lang="ts">
	import { onDestroy, tick } from 'svelte';
	import { ChevronDown, FolderClosed, Plus, Search, X } from 'lucide-svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { projects } from '$lib/stores/app-data.svelte';
	import { openHeaderDropdown, openDropdown, closeDropdown } from '$lib/stores/header-dropdowns';
	import AddProjectModal from '$lib/components/AddProjectModal.svelte';

	let { projectName, sheetMode = false }: { projectName: string; sheetMode?: boolean } = $props();
	const dropdownId = $derived(sheetMode ? 'sheet-workspace-folders' as const : 'workspace-folders' as const);
	const open = $derived($openHeaderDropdown === dropdownId);
	const folderIds = $derived($activeSession.workspaceProjectIds ?? []);
	const mainProject = $derived($projects.find(p => p.id === $activeSession.project));
	const available = $derived($projects.filter(p =>
		p.id !== $activeSession.project && !folderIds.includes(p.id) &&
		`${p.name} ${p.path}`.toLowerCase().includes(search.toLowerCase().trim())
	));
	let root = $state<HTMLDivElement>();
	let trigger = $state<HTMLButtonElement>();
	let searchInput = $state<HTMLInputElement>();
	let adding = $state(false);
	let search = $state('');
	let saving = $state(false);
	let error = $state('');
	let showCreateProject = $state(false);
	let createOwner: { sessionId: string | null; project: string } | null = null;
	const contextKey = $derived(`${$activeSession.sessionId}:${$activeSession.project}:${$activeSession.projectV2Id}`);
	$effect(() => {
		contextKey;
		closeDropdown(dropdownId);
		adding = false;
		search = '';
		error = '';
		showCreateProject = false;
	});

	function close() {
		closeDropdown(dropdownId);
		trigger?.focus();
	}
	function toggle() {
		if (open) close();
		else { adding = false; search = ''; error = ''; openDropdown(dropdownId); }
	}
	async function startAdding() {
		adding = true;
		await tick();
		searchInput?.focus();
	}
	async function save(ids: string[]) {
		if (saving) return;
		const owner = contextKey;
		saving = true;
		error = '';
		try {
			await activeSession.setWorkspaceProjectIds(ids);
			if (owner === contextKey) { adding = false; search = ''; }
		} catch (e) {
			if (owner === contextKey) {
				const err = e as { detail?: string; message?: string };
				error = err.detail || err.message || 'Could not save workspace folders. Try again.';
			}
		} finally { saving = false; }
	}
	function createProject() {
		createOwner = { sessionId: $activeSession.sessionId, project: $activeSession.project };
		showCreateProject = true;
		closeDropdown(dropdownId);
	}
	function projectCreated(id: string) {
		if (createOwner?.sessionId !== $activeSession.sessionId || createOwner.project !== $activeSession.project) return;
		openDropdown(dropdownId);
		void save([...folderIds, id]);
	}
	function clickOutside(event: MouseEvent) {
		// The Add button is replaced by the search view during this click. Its
		// original event path still includes the popover after it leaves the DOM.
		if (open && root && !event.composedPath().includes(root)) closeDropdown(dropdownId);
	}
	function keydown(event: KeyboardEvent) {
		if (event.key === 'Escape' && open) { event.preventDefault(); close(); }
	}
	onDestroy(() => closeDropdown(dropdownId));
</script>

<svelte:window onclick={clickOutside} onkeydown={keydown} />

<div class="workspace-selector" class:sheet-mode={sheetMode} bind:this={root}>
	<button type="button" class="ctx-chip repo-chip" class:open bind:this={trigger}
		aria-label="Workspace folders: {projectName}" aria-haspopup="dialog" aria-expanded={open}
		title="Workspace folders" onclick={toggle}>
		<FolderClosed size={12} strokeWidth={1.6} aria-hidden="true" />
		<span class="ctx-chip-label repo-name">{projectName}</span>
		{#if folderIds.length}<span class="folder-count">+{folderIds.length}</span>{/if}
		<ChevronDown size={11} aria-hidden="true" />
	</button>
	{#if open}
		<div class="workspace-dropdown" role="dialog" aria-label="Workspace folders" aria-busy={saving}>
			<div class="menu-heading">Workspace folders</div>
			<div class="folder-list">
				<div class="folder-row" title={mainProject?.path ?? projectName}>
					<FolderClosed size={15} aria-hidden="true" />
					<div class="folder-details"><span>{projectName}</span>{#if mainProject?.path}<small>{mainProject.path}</small>{/if}</div>
					<span class="main-label">Main</span>
				</div>
				{#each folderIds as id (id)}
					{@const folder = $projects.find(p => p.id === id)}
					<div class="folder-row" title={folder?.path ?? id}>
						<FolderClosed size={15} aria-hidden="true" />
						<div class="folder-details"><span>{folder?.name ?? id}</span><small>{folder?.path ?? 'Project unavailable'}</small></div>
						<button class="remove-folder" type="button" aria-label="Remove {folder?.name ?? id} from workspace" title="Remove from this chat" disabled={saving} onclick={() => save(folderIds.filter(item => item !== id))}><X size={14} /></button>
					</div>
				{/each}
			</div>
			{#if error}<p class="menu-error" role="alert">{error}</p>{/if}
			{#if adding}
				<div class="project-search"><Search size={14} aria-hidden="true" /><input bind:this={searchInput} bind:value={search} placeholder="Search projects…" aria-label="Search projects" /></div>
				<div class="project-options">
					{#each available as project (project.id)}
						<button type="button" class="project-option" disabled={saving} onclick={() => save([...folderIds, project.id])}>
							<FolderClosed size={15} aria-hidden="true" /><div class="folder-details"><span>{project.name}</span><small>{project.path}</small></div><Plus size={14} aria-hidden="true" />
						</button>
					{:else}<p class="empty-projects">{search ? 'No matching projects.' : 'No more projects to add.'}</p>{/each}
				</div>
				<button type="button" class="add-project" disabled={saving} onclick={createProject}><Plus size={14} />Browse for a new project…</button>
			{:else}
				<button type="button" class="add-project" disabled={saving} onclick={startAdding}><Plus size={14} />Add project</button>
			{/if}
			<p class="menu-hint">Folders apply to this chat. Changes take effect on the next turn.</p>
		</div>
	{/if}
</div>

{#if showCreateProject}
	<AddProjectModal open onClose={() => showCreateProject = false} onCreated={projectCreated} />
{/if}

<style>
	.workspace-selector { position: relative; min-width: 0; }
	.repo-name { max-width: 140px; }
	.folder-count { color: var(--muted-foreground); font-size: 0.6875rem; }
	.workspace-dropdown { position: absolute; bottom: calc(100% + 6px); left: 0; width: 320px; max-width: calc(100vw - 32px); max-height: min(440px, 65dvh); overflow-y: auto; padding: 6px; background: var(--popover); border: 1px solid var(--border); border-radius: calc(var(--radius, 0.75rem) * 0.83); box-shadow: var(--shadow-l); z-index: 100; color: var(--foreground); }
	.menu-heading { padding: 7px 9px; font-size: 0.75rem; font-weight: 600; }
	.folder-list, .project-options { max-height: 180px; overflow-y: auto; }
	.folder-row, .project-option { display: flex; align-items: center; gap: 9px; padding: 8px 9px; min-width: 0; }
	.folder-row > :global(svg), .project-option > :global(svg) { flex-shrink: 0; color: var(--muted-foreground); }
	.folder-details { display: flex; flex-direction: column; gap: 2px; flex: 1; min-width: 0; font-size: 0.8125rem; text-align: left; }
	.folder-details span, .folder-details small { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.folder-details small { font-size: 0.6875rem; color: var(--muted-foreground); }
	.main-label { font-size: 0.625rem; color: var(--muted-foreground); }
	.remove-folder { display: grid; place-items: center; width: 28px; height: 28px; padding: 0; flex-shrink: 0; }
	button { cursor: pointer; }
	.remove-folder, .project-option, .add-project { background: transparent; border: none; color: var(--foreground); border-radius: 6px; }
	.remove-folder:hover:not(:disabled), .project-option:hover:not(:disabled), .add-project:hover:not(:disabled) { background: var(--hover-overlay); }
	button:disabled { opacity: 0.5; cursor: default; }
	button:focus-visible, input:focus-visible { outline: 2px solid var(--primary); outline-offset: 1px; }
	.project-option { width: 100%; }
	.add-project { display: flex; align-items: center; gap: 8px; width: 100%; padding: 9px; margin-top: 4px; font-size: 0.8125rem; text-align: left; }
	.project-search { display: flex; align-items: center; gap: 8px; padding: 8px; margin: 4px 0; border-top: 1px solid var(--border-subtle); color: var(--muted-foreground); }
	.project-search input { width: 100%; min-width: 0; background: transparent; color: var(--foreground); border: 0; font-size: 0.8125rem; padding: 4px; }
	.empty-projects, .menu-hint, .menu-error { margin: 0; padding: 7px 9px; font-size: 0.6875rem; color: var(--muted-foreground); }
	.menu-error { color: var(--destructive); }
	.sheet-mode { grid-column: 1 / -1; width: 100%; }
	.sheet-mode .ctx-chip { width: 100%; height: 44px; padding: 0 12px; gap: 8px; border: 1px solid var(--border-subtle); border-radius: var(--composer-control-radius, 12px); background: var(--composer-control-bg, color-mix(in oklch, var(--muted) 55%, var(--card))); font-size: 0.8rem; }
	.sheet-mode .repo-name { max-width: none; }
	.sheet-mode .workspace-dropdown { position: static; width: 100%; max-width: none; margin-top: 6px; box-sizing: border-box; }
	.sheet-mode .remove-folder { width: 36px; height: 36px; }
</style>
