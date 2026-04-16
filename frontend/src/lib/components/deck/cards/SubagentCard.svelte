<script lang="ts">
	/**
	 * SubagentCard - Subagent management card for The Deck
	 *
	 * Features:
	 * - Subagent list with search filtering
	 * - Model badge (color-coded)
	 * - Tool count badge
	 * - Group assignment display
	 * - CRUD operations via API
	 * - 3-tab inline editor (Identity, Config, Prompt)
	 * - Tab validation indicators
	 * - Inline delete confirmation banner
	 * - Import/export JSON
	 */
	import { api, type SubagentExport } from '$lib/api/client';
	import { groups } from '$lib/stores/groups';
	import './card-design-system.css';

	import SubagentList from './subagents/SubagentList.svelte';
	import SubagentEditor from './subagents/SubagentEditor.svelte';

	interface Subagent {
		id: string;
		name: string;
		description: string;
		prompt: string;
		tools?: string[];
		model?: string;
		is_builtin: boolean;
		is_modified: boolean;
		created_at: string;
		updated_at: string;
	}

	interface ToolInfo {
		name: string;
		category: string;
		description: string;
		mcp_server?: string;
	}

	interface ToolCategory {
		id: string;
		name: string;
		description: string;
		tools: ToolInfo[];
	}

	interface ToolsResponse {
		categories: ToolCategory[];
		all_tools: ToolInfo[];
	}

	interface Props {
		mobile?: boolean;
	}

	let {
		mobile = false
	}: Props = $props();

	// View mode state
	type ViewMode = 'list' | 'editor';
	let viewMode = $state<ViewMode>('list');

	// List state
	let subagents = $state<Subagent[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let searchQuery = $state('');

	// Delete state
	let deletingId = $state<string | null>(null);
	let deleteLoading = $state(false);

	// Import state
	let importInput: HTMLInputElement;
	let importing = $state(false);
	let showGenerateDialog = $state(false);

	// Editor state
	let editingSubagent = $state<Subagent | null>(null);
	let isEditMode = $derived(!!editingSubagent);

	// Editor form state
	let formId = $state('');
	let formName = $state('');
	let formDescription = $state('');
	let formPrompt = $state('');
	let formTools = $state<string[]>([]);
	let formModel = $state('');
	let customModelMode = $state(false);
	let saving = $state(false);

	// Tools state
	let availableTools = $state<ToolsResponse>({ categories: [], all_tools: [] });
	let toolSelectionMode = $state<'all' | 'select'>('all');
	let expandedSections = $state<Record<string, boolean>>({ sdk: false });

	// Revert state
	let revertingId = $state<string | null>(null);
	let revertLoading = $state(false);

	// Group state
	let creatingGroup = $state(false);
	let newGroupName = $state('');

	// Model options
	const MODEL_OPTIONS = [
		{ value: '', label: 'Inherit' },
		{ value: 'haiku', label: 'Haiku' },
		{ value: 'sonnet', label: 'Sonnet' },
		{ value: 'opus', label: 'Opus' },
		{ value: 'opus[1m]', label: 'Opus 1M' }
	];

	// Filtered subagents
	const filteredSubagents = $derived(() => {
		if (!searchQuery.trim()) return subagents;
		const q = searchQuery.toLowerCase();
		return subagents.filter(
			s =>
				s.name.toLowerCase().includes(q) ||
				s.id.toLowerCase().includes(q) ||
				s.description.toLowerCase().includes(q)
		);
	});

	// Validation per tab
	const identityValid = $derived(() => {
		if (!isEditMode && !formId.match(/^[a-z0-9-]+$/)) return false;
		if (!isEditMode && formId.length === 0) return false;
		if (formName.length === 0) return false;
		if (formDescription.length === 0) return false;
		return true;
	});

	const configValid = $derived(() => true);
	const promptValid = $derived(() => formPrompt.length > 0);
	const isValid = $derived(() => identityValid() && configValid() && promptValid());

	// Load subagents
	async function loadSubagents() {
		loading = true;
		error = null;
		try {
			subagents = await api.get<Subagent[]>('/subagents');
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			error = err.detail || err.message || 'Failed to load subagents';
		} finally {
			loading = false;
		}
	}

	// Load tools
	async function loadTools() {
		try {
			availableTools = await api.get<ToolsResponse>('/tools');
		} catch (e) {
			console.error('Failed to load tools:', e);
		}
	}

	// Initialize
	$effect(() => {
		loadSubagents();
		loadTools();
	});

	// Get model display info
	function getModelDisplay(model?: string): string {
		const opt = MODEL_OPTIONS.find(o => o.value === model);
		return opt?.label || 'Inherit';
	}

	function getModelBadgeClass(model?: string): string {
		if (!model || model === '') return 'card-badge--inherit';
		if (model.startsWith('opus')) return 'card-badge--opus';
		return `card-badge--${model}`;
	}

	// Detect whether a model value is a preset (including Inherit) or a custom string
	function isCustomModel(model?: string): boolean {
		if (!model) return false;
		const presets = MODEL_OPTIONS.map(o => o.value);
		return !presets.includes(model);
	}

	// Open editor for new subagent
	function handleCreate() {
		editingSubagent = null;
		formId = '';
		formName = '';
		formDescription = '';
		formPrompt = '';
		formTools = [];
		formModel = '';
		customModelMode = false;
		toolSelectionMode = 'all';
		viewMode = 'editor';
	}

	// Open editor for existing subagent
	function handleEdit(agent: Subagent) {
		editingSubagent = agent;
		formId = agent.id;
		formName = agent.name;
		formDescription = agent.description;
		formPrompt = agent.prompt;
		formTools = agent.tools || [];
		formModel = agent.model || '';
		customModelMode = isCustomModel(agent.model);
		toolSelectionMode = Array.isArray(agent.tools) ? 'select' : 'all';
		viewMode = 'editor';
	}

	// Back to list
	function handleBack() {
		viewMode = 'list';
		editingSubagent = null;
		error = null;
	}

	// Save subagent
	async function handleSave() {
		if (!isValid()) return;

		saving = true;
		error = null;

		try {
			const body: Record<string, unknown> = {
				name: formName,
				description: formDescription,
				prompt: formPrompt,
			};

			// tools: null = all tools (inherit), [] = no tools, [...] = specific tools
			body.tools = toolSelectionMode === 'select' ? formTools : null;
			// Always include model — null means "inherit from session"
			body.model = formModel || null;

			if (isEditMode && editingSubagent) {
				await api.put(`/subagents/${editingSubagent.id}`, body);
			} else {
				body.id = formId;
				await api.post('/subagents', body);
			}

			await loadSubagents();
			handleBack();
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			error = err.detail || err.message || 'Failed to save subagent';
		} finally {
			saving = false;
		}
	}

	// Delete subagent
	function handleDeleteClick(id: string, e: Event) {
		e.stopPropagation();
		const agent = subagents.find(s => s.id === id);
		if (agent?.is_builtin) {
			error = 'Built-in subagents cannot be deleted. You can disable them in profile settings.';
			return;
		}
		deletingId = id;
	}

	async function confirmDelete() {
		if (!deletingId) return;
		deleteLoading = true;
		try {
			await api.delete(`/subagents/${deletingId}`);
			deletingId = null;
			await loadSubagents();
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			error = err.detail || err.message || 'Failed to delete subagent';
		} finally {
			deleteLoading = false;
		}
	}

	// Revert built-in subagent to defaults
	async function handleRevert(id: string, e: Event) {
		e.stopPropagation();
		revertingId = id;
	}

	async function confirmRevert() {
		if (!revertingId) return;
		revertLoading = true;
		try {
			await api.post(`/subagents/${revertingId}/revert`);
			revertingId = null;
			await loadSubagents();
			// If we're in the editor for this agent, reload the form
			if (editingSubagent?.id === revertingId) {
				const updated = subagents.find(s => s.id === revertingId);
				if (updated) {
					handleEdit(updated);
				}
			}
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			error = err.detail || err.message || 'Failed to revert subagent';
		} finally {
			revertLoading = false;
		}
	}

	// Export subagent
	async function exportSubagent(agentId: string, e: Event) {
		e.stopPropagation();
		try {
			const data = await api.get<unknown>(`/export-import/subagents/${agentId}`);
			const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
			const url = URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = url;
			a.download = `subagent-${agentId}-${new Date().toISOString().split('T')[0]}.json`;
			document.body.appendChild(a);
			a.click();
			document.body.removeChild(a);
			URL.revokeObjectURL(url);
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			error = 'Export failed: ' + (err.detail || err.message || 'Unknown error');
		}
	}

	// Import
	function triggerImport() {
		importInput?.click();
	}

	async function handleImport(event: Event) {
		const input = event.target as HTMLInputElement;
		const file = input.files?.[0];
		if (!file) return;
		input.value = '';

		importing = true;
		error = null;

		try {
			const content = await file.text();
			const data = JSON.parse(content);

			if (!data.subagents || data.subagents.length === 0) {
				error = 'No subagents found in file';
				importing = false;
				return;
			}

			const result = await api.post<{ subagents_imported: number; subagents_skipped: number; errors?: string[] }>('/export-import/import/json?overwrite_existing=false', data);

			if (result.subagents_imported > 0) {
				await loadSubagents();
			} else if (result.subagents_skipped > 0) {
				error = 'Subagent already exists. Use a different ID or delete the existing one first.';
			} else if (result.errors?.length) {
				error = 'Import failed: ' + result.errors.join(', ');
			}
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			error = 'Import failed: ' + (err.detail || err.message || 'Invalid JSON file');
		}

		importing = false;
	}

	// Group assignment
	function assignToGroup(agentId: string, groupName: string) {
		groups.assignToGroup('subagents', agentId, groupName);
	}

	function removeFromGroup(agentId: string) {
		groups.removeFromGroup('subagents', agentId);
	}

	function startGroupCreate() {
		creatingGroup = true;
		newGroupName = '';
	}

	function confirmGroupCreate(agentId: string) {
		const name = newGroupName.trim();
		if (name) {
			groups.createGroup('subagents', name);
			groups.assignToGroup('subagents', agentId, name);
		}
		creatingGroup = false;
		newGroupName = '';
	}

	function cancelGroupCreate() {
		creatingGroup = false;
		newGroupName = '';
	}

	/**
	 * Opens the editor view populated with AI-generated subagent data.
	 * User can review/edit the values before saving.
	 */
	function openEditorFromGenerated(data: SubagentExport) {
		editingSubagent = null; // Creating new, not editing existing

		const sub = data.subagent;
		formId = sub.id;
		formName = sub.name;
		formDescription = sub.description;
		formPrompt = sub.prompt;
		formTools = sub.tools || [];
		formModel = sub.model || '';
		customModelMode = isCustomModel(sub.model);
		toolSelectionMode = Array.isArray(sub.tools) ? 'select' : 'all';

		viewMode = 'editor';
	}

	// Handle AI-generated subagent
	function handleGeneratedSubagent(data: SubagentExport | unknown) {
		showGenerateDialog = false;
		openEditorFromGenerated(data as SubagentExport);
	}

	// Tool helpers
	function toggleSection(section: string) {
		expandedSections = { ...expandedSections, [section]: !expandedSections[section] };
	}
</script>

<!-- Hidden file input for import -->
<input type="file" accept=".json" bind:this={importInput} onchange={handleImport} class="hidden" />

{#if viewMode === 'list'}
	<SubagentList
		{subagents}
		{loading}
		{error}
		{searchQuery}
		onSearchQueryChange={(v) => searchQuery = v}
		{deletingId}
		{deleteLoading}
		{revertingId}
		{revertLoading}
		{importing}
		{showGenerateDialog}
		onShowGenerateDialogChange={(v) => showGenerateDialog = v}
		onEdit={handleEdit}
		onCreate={handleCreate}
		onDeleteClick={handleDeleteClick}
		onConfirmDelete={confirmDelete}
		onCancelDelete={() => deletingId = null}
		onRevert={handleRevert}
		onConfirmRevert={confirmRevert}
		onCancelRevert={() => revertingId = null}
		onExport={exportSubagent}
		onTriggerImport={triggerImport}
		onClearError={() => error = null}
		onGeneratedSubagent={handleGeneratedSubagent}
		{getModelDisplay}
		{getModelBadgeClass}
		{filteredSubagents}
	/>
{:else}
	<SubagentEditor
		{editingSubagent}
		{isEditMode}
		{error}
		{saving}
		{formId}
		onFormIdChange={(v) => formId = v}
		{formName}
		onFormNameChange={(v) => formName = v}
		{formDescription}
		onFormDescriptionChange={(v) => formDescription = v}
		{formPrompt}
		onFormPromptChange={(v) => formPrompt = v}
		{formTools}
		onFormToolsChange={(v) => formTools = v}
		{formModel}
		onFormModelChange={(v) => formModel = v}
		{customModelMode}
		onCustomModelModeChange={(custom, model) => { customModelMode = custom; formModel = model; }}
		{toolSelectionMode}
		onToolSelectionModeChange={(v) => toolSelectionMode = v}
		{availableTools}
		{expandedSections}
		onToggleSection={toggleSection}
		{creatingGroup}
		{newGroupName}
		onNewGroupNameChange={(v) => newGroupName = v}
		onStartGroupCreate={startGroupCreate}
		onConfirmGroupCreate={confirmGroupCreate}
		onCancelGroupCreate={cancelGroupCreate}
		onAssignToGroup={assignToGroup}
		onRemoveFromGroup={removeFromGroup}
		onBack={handleBack}
		onSave={handleSave}
		onClearError={() => error = null}
		{isValid}
		{showGenerateDialog}
		onShowGenerateDialogChange={(v) => showGenerateDialog = v}
		onGeneratedSubagent={handleGeneratedSubagent}
	/>
{/if}

<style>
	.hidden {
		display: none;
	}
</style>
