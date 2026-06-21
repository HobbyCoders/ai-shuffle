<script lang="ts">
	/**
	 * HooksCard - Hook management card for The Deck
	 *
	 * Features:
	 * - View and manage app-level hooks (no project)
	 * - View and manage project-scoped hooks
	 * - View plugin-contributed hooks (read-only)
	 * - Create, edit, delete, and test hooks
	 * - Enable/disable hooks
	 * - Page-based navigation with breadcrumbs (no modals)
	 */
	import StudioEditorShell from '$lib/components/layout/StudioEditorShell.svelte';
	import {
		hooks,
		projectHooks,
		pluginHooks,
		hooksLoading,
		hooksError,
		hookTesting,
		hookSaving
	} from '$lib/stores/hooks.svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { derived } from 'svelte/store';

	const selectedProject = derived(activeSession, $s => $s.project);
	import type { Hook, HookCreate, HookUpdate, HookTestResult } from '$lib/api/client';
	import HookForm from '$lib/components/hooks/HookForm.svelte';
	import BuildHookList from '$lib/components/layout/build/BuildHookList.svelte';
	import { Loader2, Check } from 'lucide-svelte';
	import './card-design-system.css';

	interface Props {
		mobile?: boolean;
		/** When embedded in the Build page, render the header-less rows list
		 *  instead of the FeaturePageLayout list. The editor is unchanged. */
		embedded?: boolean;
		/** Reports full-canvas detail/editor state so BuildPage can hide its chrome. */
		onDetailChange?: (detail: boolean) => void;
	}

	let {
		mobile = false,
		embedded = false,
		onDetailChange
	}: Props = $props();

	// Tell the Build page when we enter/leave the editor (form takes the canvas).
	$effect(() => {
		if (embedded) onDetailChange?.(currentView !== 'list');
	});

	// ── View state ─────────────────────────────────────────────────────
	type View = 'list' | 'create' | 'edit';
	let currentView = $state<View>('list');


	// Selected hook for editing
	let selectedHook = $state<Hook | null>(null);
	let formSaving = $state(false);

	// Form ref for calling save() from footer
	let formRef: HookForm | undefined = $state(undefined);

	// Test result display
	let testResult = $state<{ hookId: string; result: HookTestResult } | null>(null);


	// ── Data loading ───────────────────────────────────────────────────
	$effect(() => {
		if ($selectedProject) {
			hooks.loadAll($selectedProject);
		} else {
			// Still load app hooks even without a project
			hooks.loadAppHooks();
		}
	});



	// ── Navigation ─────────────────────────────────────────────────────
	function backToList() {
		currentView = 'list';
		selectedHook = null;
		formRef = undefined;
	}

	function openCreate() {
		selectedHook = null;
		formRef = undefined;
		currentView = 'create';
	}

	function openEdit(hook: Hook) {
		selectedHook = hook;
		formRef = undefined;
		currentView = 'edit';
	}

	// ── CRUD handlers ──────────────────────────────────────────────────
	async function handleSave() {
		formRef?.save();
	}

	async function handleFormSave(data: HookCreate | HookUpdate, projectId: string | null = null) {
		formSaving = true;
		try {
			if (selectedHook) {
				await hooks.updateHook(selectedHook.id, data as HookUpdate);
			} else if (projectId) {
				// Project-scoped hook
				await hooks.createHook(projectId, data as HookCreate);
			} else {
				// App-level hook — no project_id
				await hooks.createAppHook(data as HookCreate);
			}
			backToList();
		} catch (e) {
			console.error('Failed to save hook:', e);
		} finally {
			formSaving = false;
		}
	}

	async function handleTestHook(hookId: string) {
		try {
			const result = await hooks.testHook(hookId);
			testResult = { hookId, result };
			setTimeout(() => {
				if (testResult?.hookId === hookId) {
					testResult = null;
				}
			}, 5000);
		} catch (e) {
			console.error('Failed to test hook:', e);
		}
	}


	// Direct delete for the embedded Build list (it owns its own inline confirm).
	async function embeddedDelete(hookId: string) {
		await hooks.deleteHook(hookId);
	}

</script>

<!-- ═══════════════════ LIST (Build page) ═══════════════════ -->
{#if currentView === 'list'}
	<BuildHookList
		allHooks={$projectHooks}
		pluginHooks={$pluginHooks}
		hasProject={!!$selectedProject}
		loading={$hooksLoading}
		error={$hooksError}
		{testResult}
		testing={$hookTesting}
		saving={$hookSaving}
		onCreate={openCreate}
		onEdit={openEdit}
		onDelete={embeddedDelete}
		onToggle={(id) => hooks.toggleHook(id)}
		onTest={handleTestHook}
		onClearError={() => hooks.clearError()}
		onClearTest={() => (testResult = null)}
	/>

<!-- ═══════════════════ CREATE / EDIT VIEW ═══════════════════ -->
{:else if currentView === 'create' || currentView === 'edit'}
	<StudioEditorShell
		title={currentView === 'edit' && selectedHook ? selectedHook.name : 'New Hook'}
		subtitle={currentView === 'edit' ? 'Hook' : 'Create a new hook'}
		backLabel="Hooks"
		onBack={backToList}
	>
		{#snippet actions()}
			<button type="button" class="ed-btn primary" onclick={handleSave} disabled={formSaving}>
				{#if formSaving}
					<Loader2 size={14} class="animate-spin" />
					<span>Saving...</span>
				{:else}
					<Check size={14} />
					<span>{currentView === 'edit' ? 'Save' : 'Create'}</span>
				{/if}
			</button>
		{/snippet}
		<HookForm
			bind:this={formRef}
			hook={selectedHook}
			onSave={handleFormSave}
		/>
	</StudioEditorShell>

{/if}


<style>
	/* Spinner animation */
	:global(.animate-spin) {
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		from { transform: rotate(0deg); }
		to { transform: rotate(360deg); }
	}
</style>
