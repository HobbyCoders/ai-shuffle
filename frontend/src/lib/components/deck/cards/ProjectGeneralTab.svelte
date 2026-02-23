<script lang="ts">
	/**
	 * ProjectGeneralTab - General settings tab for project detail view
	 *
	 * Matches ProfileCard general tab UI pattern:
	 * - Editable project name
	 * - Read-only project ID and path
	 * - Group assignment with select + plus/delete buttons + new group input
	 */
	import { appData, projects } from '$lib/stores/app-data';
	import { groups as groupsStore } from '$lib/stores/groups';
	import { Check, Plus, Trash2, Loader2 } from 'lucide-svelte';
	import './card-design-system.css';

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

	// Group creation state
	let creatingGroup = $state(false);
	let newGroupName = $state('');

	// Sync editName when project changes
	$effect(() => {
		if (project) {
			editName = project.name;
		}
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

{#if project}
	<div class="general-tab">
		<div class="pf-section">
			<div class="pf-field-grid">
				<!-- Project Name -->
				<div class="pf-field">
					<label for="project-name" class="pf-label">Name</label>
					<div class="name-input-row">
						<input
							id="project-name"
							type="text"
							bind:value={editName}
							class="pf-input"
							placeholder="Project name"
							onkeydown={(e) => { if (e.key === 'Enter') handleSaveName(); }}
						/>
						{#if nameDirty}
							<button
								class="pf-btn-new-group"
								onclick={handleSaveName}
								disabled={nameLoading || !editName.trim()}
								title="Save name"
							>
								{#if nameLoading}
									<Loader2 size={14} class="animate-spin" />
								{:else}
									<Check size={14} />
								{/if}
							</button>
						{/if}
					</div>
					{#if nameError}
						<p class="field-error">{nameError}</p>
					{/if}
				</div>

				<!-- Project ID -->
				<div class="pf-field">
					<span class="pf-label">Project ID</span>
					<input type="text" class="pf-input pf-input--mono" value={project.id} readonly />
				</div>

				<!-- Project Path -->
				<div class="pf-field">
					<span class="pf-label">Path</span>
					<input type="text" class="pf-input pf-input--mono" value={project.path} readonly />
				</div>

				<!-- Group Assignment -->
				<div class="pf-field">
					<label class="pf-label">Group</label>
					<div class="pf-group-wrapper">
						<div class="pf-group-row">
							<select
								class="pf-select"
								value={groups?.projects?.assignments?.[projectId] ?? ''}
								onchange={handleGroupChange}
							>
								<option value="">No group</option>
								{#each groups?.projects?.groups ?? [] as group}
									<option value={group.name}>{group.name}</option>
								{/each}
							</select>
							{#if creatingGroup}
								<button type="button" class="pf-btn-new-group" onclick={confirmGroupCreate} title="Create">
									<Check size={14} />
								</button>
							{:else}
								<button type="button" class="pf-btn-new-group" onclick={startGroupCreate} title="New group">
									<Plus size={14} />
								</button>
								{#if (groups?.projects?.assignments?.[projectId] ?? "")}
									<button type="button" class="pf-btn-new-group pf-btn-new-group--delete" onclick={deleteCurrentGroup} title="Delete group">
										<Trash2 size={14} />
									</button>
								{/if}
							{/if}
						</div>
						{#if creatingGroup}
							<input
								type="text"
								class="pf-input pf-group-input"
								placeholder="Group name..."
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
	</div>
{/if}

<style>
	.general-tab {
		padding: 20px;
	}

	/* ─── Reuse ProfileCard pf- styles ─── */

	.pf-section {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.pf-field-grid {
		display: grid;
		grid-template-columns: repeat(2, 1fr);
		gap: 16px;
	}

	@media (max-width: 480px) {
		.pf-field-grid {
			grid-template-columns: 1fr;
		}
	}

	.pf-field {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.pf-label {
		font-size: var(--text-sm, 0.75rem);
		font-weight: 500;
		color: var(--text-secondary);
	}

	.pf-input {
		width: 100%;
		padding: 10px 14px;
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		color: var(--text-primary);
		font-size: var(--text-md, 0.875rem);
		font-family: inherit;
		transition: border-color 200ms, box-shadow 200ms;
		outline: none;
	}

	.pf-input:focus {
		border-color: var(--primary);
		box-shadow: 0 0 0 3px color-mix(in oklch, var(--primary) 15%, transparent);
	}

	.pf-input::placeholder {
		color: var(--text-tertiary);
	}

	.pf-input--mono {
		font-family: var(--card-font-mono, 'JetBrains Mono', monospace);
		font-size: var(--text-sm, 0.75rem);
	}

	.pf-select {
		width: 100%;
		padding: 10px 14px;
		padding-right: 36px;
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		color: var(--text-primary);
		font-size: var(--text-md, 0.875rem);
		font-family: inherit;
		cursor: pointer;
		outline: none;
		appearance: none;
		background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='16' viewBox='0 0 24 24' fill='none' stroke='%239090A8' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E");
		background-repeat: no-repeat;
		background-position: right 12px center;
		background-size: 16px;
		transition: border-color 200ms, box-shadow 200ms;
	}

	.pf-select:focus {
		border-color: var(--primary);
		box-shadow: 0 0 0 3px color-mix(in oklch, var(--primary) 15%, transparent);
	}

	/* ─── Name input row ─── */

	.name-input-row {
		display: flex;
		gap: 6px;
		align-items: center;
	}

	.name-input-row .pf-input {
		flex: 1;
	}

	/* ─── Group UI ─── */

	.pf-group-wrapper {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.pf-group-row {
		display: flex;
		gap: 6px;
		align-items: center;
	}

	.pf-group-row .pf-select {
		flex: 1;
	}

	.pf-btn-new-group {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 36px;
		height: 36px;
		flex-shrink: 0;
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		color: var(--text-secondary);
		cursor: pointer;
		transition: all 200ms;
	}

	.pf-btn-new-group:hover {
		background: var(--surface-hover);
		color: var(--text-primary);
		border-color: var(--border-default);
	}

	.pf-btn-new-group:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.pf-btn-new-group--delete {
		color: var(--destructive);
	}

	.pf-btn-new-group--delete:hover {
		background: color-mix(in oklch, var(--destructive) 15%, transparent);
		color: var(--destructive);
		border-color: color-mix(in oklch, var(--destructive) 30%, transparent);
	}

	.pf-group-input {
		width: 100%;
	}

	/* ─── Error ─── */

	.field-error {
		font-size: var(--text-xs, 0.6875rem);
		color: var(--destructive);
		margin: 0;
	}

	:global(.animate-spin) {
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		from { transform: rotate(0deg); }
		to { transform: rotate(360deg); }
	}
</style>
