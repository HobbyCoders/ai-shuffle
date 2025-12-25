<script lang="ts">
	/**
	 * SettingsDrawer - Side drawer for chat settings
	 * Contains profile, project, model, permission mode selectors
	 * Context usage display and background mode toggle
	 */
	import { tick, onMount, onDestroy } from 'svelte';
	import type { Project } from '$lib/stores/tabs';
	import type { Profile } from '$lib/api/client';

	interface ContextUsage {
		used: number;
		total: number;
		percentage: number;
	}

	interface Props {
		open: boolean;
		onClose: () => void;
		selectedProfile: string | null;
		selectedProject: string | null;
		selectedModel: string | null;  // Override value (null = use profile default)
		selectedMode: string | null;   // Override value (null = use profile default)
		effectiveModel: string;        // Actual model being used (override or profile default)
		effectiveMode: string;         // Actual mode being used (override or profile default)
		contextUsage: ContextUsage;
		isBackgroundMode: boolean;
		isProfileLocked?: boolean;
		isProjectLocked?: boolean;
		profiles: Profile[];
		projects: Project[];
		models: { value: string; label: string }[];
		modes: { value: string; label: string }[];
		onProfileChange: (profileId: string) => void;
		onProjectChange: (projectId: string) => void;
		onModelChange: (model: string | null) => void;
		onModeChange: (mode: string | null) => void;
		onBackgroundModeChange: (enabled: boolean) => void;
	}

	let {
		open = $bindable(),
		onClose,
		selectedProfile,
		selectedProject,
		selectedModel,
		selectedMode,
		effectiveModel,
		effectiveMode,
		contextUsage,
		isBackgroundMode,
		isProfileLocked = false,
		isProjectLocked = false,
		profiles,
		projects,
		models,
		modes,
		onProfileChange,
		onProjectChange,
		onModelChange,
		onModeChange,
		onBackgroundModeChange
	}: Props = $props();

	// Local state
	let drawerElement = $state<HTMLDivElement | null>(null);

	// Format token count for display
	function formatTokenCount(count: number): string {
		if (count >= 1000000) {
			return `${(count / 1000000).toFixed(1)}M`;
		}
		if (count >= 1000) {
			return `${(count / 1000).toFixed(1)}k`;
		}
		return count.toString();
	}

	// Get selected names for display
	const selectedProfileName = $derived(
		profiles.find(p => p.id === selectedProfile)?.name || 'Select Profile'
	);
	const selectedProjectName = $derived(
		projects.find(p => p.id === selectedProject)?.name || 'Select Project'
	);
	// For Model/Mode, show effective value with indicator if it's inherited from profile
	const effectiveModelLabel = $derived(
		models.find(m => m.value === effectiveModel)?.label || effectiveModel || 'Sonnet'
	);
	const effectiveModeLabel = $derived(
		modes.find(m => m.value === effectiveMode)?.label || effectiveMode || 'Ask'
	);
	const isModelOverridden = $derived(selectedModel !== null);
	const isModeOverridden = $derived(selectedMode !== null);

	// Context usage color
	const contextColor = $derived(
		contextUsage.percentage > 80 ? 'text-red-500' :
		contextUsage.percentage > 60 ? 'text-amber-500' :
		'text-emerald-500'
	);

	// Handle escape key
	function handleKeyDown(e: KeyboardEvent) {
		if (e.key === 'Escape' && open) {
			onClose();
		}
	}

	// Handle backdrop click
	function handleBackdropClick(e: MouseEvent) {
		if (e.target === e.currentTarget) {
			onClose();
		}
	}

	// Prevent body scroll when drawer is open
	$effect(() => {
		if (open) {
			document.body.style.overflow = 'hidden';
		} else {
			document.body.style.overflow = '';
		}

		return () => {
			document.body.style.overflow = '';
		};
	});

	onMount(() => {
		document.addEventListener('keydown', handleKeyDown);
		return () => document.removeEventListener('keydown', handleKeyDown);
	});
</script>

<!-- Backdrop -->
{#if open}
	<div
		class="drawer-backdrop"
		onclick={handleBackdropClick}
		role="presentation"
	>
		<!-- Drawer -->
		<div
			bind:this={drawerElement}
			class="settings-drawer"
			role="dialog"
			aria-modal="true"
			aria-labelledby="drawer-title"
		>
			<!-- Header -->
			<div class="drawer-header">
				<h2 id="drawer-title" class="drawer-title">Settings</h2>
				<button
					type="button"
					onclick={onClose}
					class="close-btn"
					aria-label="Close settings"
				>
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</button>
			</div>

			<!-- Content -->
			<div class="drawer-content">
				<!-- Profile Selector -->
				<div class="setting-group">
					<div class="setting-label-row">
						<label for="profile-select" class="setting-label">Profile</label>
						{#if isProfileLocked}
							<span class="locked-badge">Locked</span>
						{/if}
					</div>
					<select
						id="profile-select"
						value={selectedProfile || ''}
						onchange={(e) => onProfileChange((e.target as HTMLSelectElement).value)}
						class="setting-select"
						disabled={isProfileLocked}
					>
						<option value="" disabled>Select Profile</option>
						{#each profiles as profile}
							<option value={profile.id}>{profile.name}</option>
						{/each}
					</select>
				</div>

				<!-- Project Selector -->
				<div class="setting-group">
					<div class="setting-label-row">
						<label for="project-select" class="setting-label">Project</label>
						{#if isProjectLocked}
							<span class="locked-badge">Locked</span>
						{/if}
					</div>
					<select
						id="project-select"
						value={selectedProject || ''}
						onchange={(e) => onProjectChange((e.target as HTMLSelectElement).value)}
						class="setting-select"
						disabled={isProjectLocked}
					>
						<option value="" disabled>Select Project</option>
						{#each projects as project}
							<option value={project.id}>{project.name}</option>
						{/each}
					</select>
				</div>

				<!-- Divider -->
				<div class="divider"></div>

				<!-- Model Selector -->
				<div class="setting-group">
					<div class="setting-label-row">
						<label for="model-select" class="setting-label">Model</label>
						{#if isModelOverridden}
							<button
								type="button"
								class="reset-btn"
								onclick={() => onModelChange(null)}
								title="Reset to profile default"
							>
								Reset
							</button>
						{:else}
							<span class="inherited-badge">From Profile</span>
						{/if}
					</div>
					<select
						id="model-select"
						value={effectiveModel}
						onchange={(e) => {
							const value = (e.target as HTMLSelectElement).value;
							onModelChange(value);
						}}
						class="setting-select"
						class:inherited={!isModelOverridden}
					>
						{#each models as model}
							<option value={model.value}>{model.label}</option>
						{/each}
					</select>
				</div>

				<!-- Permission Mode Selector -->
				<div class="setting-group">
					<div class="setting-label-row">
						<label for="mode-select" class="setting-label">Permission Mode</label>
						{#if isModeOverridden}
							<button
								type="button"
								class="reset-btn"
								onclick={() => onModeChange(null)}
								title="Reset to profile default"
							>
								Reset
							</button>
						{:else}
							<span class="inherited-badge">From Profile</span>
						{/if}
					</div>
					<select
						id="mode-select"
						value={effectiveMode}
						onchange={(e) => {
							const value = (e.target as HTMLSelectElement).value;
							onModeChange(value);
						}}
						class="setting-select"
						class:inherited={!isModeOverridden}
					>
						{#each modes as mode}
							<option value={mode.value}>{mode.label}</option>
						{/each}
					</select>
				</div>

				<!-- Divider -->
				<div class="divider"></div>

				<!-- Context Usage -->
				<div class="setting-group">
					<div class="setting-label">Context Usage</div>
					<div class="context-display">
						<div class="context-bar">
							<div
								class="context-fill {contextColor}"
								style="width: {contextUsage.percentage}%"
							></div>
						</div>
						<div class="context-text">
							<span class="context-percentage {contextColor}">
								{Math.round(contextUsage.percentage)}%
							</span>
							<span class="context-tokens">
								({formatTokenCount(contextUsage.used)} / {formatTokenCount(contextUsage.total)})
							</span>
						</div>
					</div>
				</div>

				<!-- Divider -->
				<div class="divider"></div>

				<!-- Background Mode Toggle -->
				<div class="setting-group">
					<label class="toggle-label">
						<input
							type="checkbox"
							checked={isBackgroundMode}
							onchange={(e) => onBackgroundModeChange((e.target as HTMLInputElement).checked)}
							class="toggle-checkbox"
						/>
						<span class="toggle-indicator"></span>
						<div class="toggle-text">
							<span class="toggle-title">Run in Background</span>
							<span class="toggle-description">
								Creates a background agent for autonomous work
							</span>
						</div>
					</label>
				</div>
			</div>
		</div>
	</div>
{/if}

<style>
	/* Backdrop */
	.drawer-backdrop {
		position: fixed;
		inset: 0;
		z-index: 1000;
		background: rgba(0, 0, 0, 0.5);
		backdrop-filter: blur(4px);
		-webkit-backdrop-filter: blur(4px);
		animation: fadeIn 0.2s ease-out;
		display: flex;
		justify-content: flex-end;
	}

	@keyframes fadeIn {
		from {
			opacity: 0;
		}
		to {
			opacity: 1;
		}
	}

	/* Drawer */
	.settings-drawer {
		width: 100%;
		max-width: 400px;
		height: 100%;
		background: var(--card);
		border-left: 1px solid var(--border);
		box-shadow: -4px 0 24px -4px rgba(0, 0, 0, 0.2);
		animation: slideIn 0.3s ease-out;
		display: flex;
		flex-direction: column;
		overflow: hidden;
	}

	@media (max-width: 640px) {
		.settings-drawer {
			max-width: 100%;
		}
	}

	@keyframes slideIn {
		from {
			transform: translateX(100%);
		}
		to {
			transform: translateX(0);
		}
	}

	:global(.dark) .settings-drawer {
		background: oklch(0.16 0.01 260);
		border-color: oklch(0.28 0.01 260);
	}

	:global(.light) .settings-drawer {
		background: white;
		border-color: oklch(0.88 0.005 260);
	}

	/* Header */
	.drawer-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 1.25rem 1.5rem;
		border-bottom: 1px solid var(--border);
		flex-shrink: 0;
	}

	.drawer-title {
		font-size: 1.125rem;
		font-weight: 600;
		color: var(--foreground);
		margin: 0;
	}

	.close-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 36px;
		height: 36px;
		border-radius: 0.5rem;
		color: var(--muted-foreground);
		transition: background-color 0.15s, color 0.15s;
	}

	.close-btn:hover {
		background: var(--accent);
		color: var(--foreground);
	}

	/* Content */
	.drawer-content {
		flex: 1;
		overflow-y: auto;
		padding: 1.5rem;
		display: flex;
		flex-direction: column;
		gap: 1.25rem;
	}

	/* Setting Group */
	.setting-group {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}

	.setting-label {
		font-size: 0.875rem;
		font-weight: 500;
		color: var(--foreground);
		user-select: none;
	}

	.setting-label-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
	}

	.locked-badge {
		font-size: 0.6875rem;
		font-weight: 500;
		color: var(--muted-foreground);
		background: color-mix(in srgb, var(--muted) 50%, transparent);
		padding: 0.125rem 0.5rem;
		border-radius: 9999px;
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}

	.inherited-badge {
		font-size: 0.6875rem;
		font-weight: 500;
		color: var(--primary);
		opacity: 0.8;
	}

	.reset-btn {
		font-size: 0.6875rem;
		font-weight: 500;
		color: var(--primary);
		background: transparent;
		border: none;
		padding: 0.125rem 0.375rem;
		border-radius: 0.25rem;
		cursor: pointer;
		transition: background-color 0.15s;
	}

	.reset-btn:hover {
		background: color-mix(in srgb, var(--primary) 15%, transparent);
	}

	/* Select */
	.setting-select {
		width: 100%;
		padding: 0.625rem 0.875rem;
		font-size: 0.9375rem;
		color: var(--foreground);
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 0.5rem;
		transition: border-color 0.15s, box-shadow 0.15s;
		cursor: pointer;
	}

	.setting-select:hover {
		border-color: color-mix(in srgb, var(--border) 70%, var(--foreground));
	}

	.setting-select:focus {
		outline: none;
		border-color: var(--primary);
		box-shadow: 0 0 0 3px color-mix(in srgb, var(--primary) 10%, transparent);
	}

	:global(.dark) .setting-select {
		background: oklch(0.18 0.01 260);
		border-color: oklch(0.3 0.01 260);
	}

	.setting-select:disabled {
		opacity: 0.6;
		cursor: not-allowed;
		background: color-mix(in srgb, var(--muted) 20%, transparent);
	}

	.setting-select.inherited {
		border-style: dashed;
		opacity: 0.85;
	}

	/* Divider */
	.divider {
		height: 1px;
		background: var(--border);
		margin: 0.5rem 0;
	}

	/* Context Display */
	.context-display {
		display: flex;
		flex-direction: column;
		gap: 0.625rem;
	}

	.context-bar {
		width: 100%;
		height: 8px;
		background: color-mix(in srgb, var(--muted) 30%, transparent);
		border-radius: 9999px;
		overflow: hidden;
	}

	.context-fill {
		height: 100%;
		background: currentColor;
		border-radius: 9999px;
		transition: width 0.3s ease, color 0.3s ease;
	}

	.context-text {
		display: flex;
		align-items: center;
		justify-content: space-between;
		font-size: 0.875rem;
	}

	.context-percentage {
		font-weight: 600;
	}

	.context-tokens {
		color: var(--muted-foreground);
		font-size: 0.8125rem;
	}

	/* Toggle */
	.toggle-label {
		display: flex;
		align-items: flex-start;
		gap: 0.75rem;
		cursor: pointer;
		user-select: none;
	}

	.toggle-checkbox {
		position: absolute;
		opacity: 0;
		pointer-events: none;
	}

	.toggle-indicator {
		flex-shrink: 0;
		width: 44px;
		height: 24px;
		background: color-mix(in srgb, var(--muted) 50%, transparent);
		border-radius: 9999px;
		position: relative;
		transition: background-color 0.2s;
	}

	.toggle-indicator::before {
		content: '';
		position: absolute;
		top: 2px;
		left: 2px;
		width: 20px;
		height: 20px;
		background: white;
		border-radius: 50%;
		transition: transform 0.2s;
		box-shadow: 0 2px 4px rgba(0, 0, 0, 0.2);
	}

	.toggle-checkbox:checked + .toggle-indicator {
		background: var(--primary);
	}

	.toggle-checkbox:checked + .toggle-indicator::before {
		transform: translateX(20px);
	}

	.toggle-checkbox:focus + .toggle-indicator {
		box-shadow: 0 0 0 3px color-mix(in srgb, var(--primary) 20%, transparent);
	}

	.toggle-text {
		flex: 1;
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}

	.toggle-title {
		font-size: 0.875rem;
		font-weight: 500;
		color: var(--foreground);
	}

	.toggle-description {
		font-size: 0.8125rem;
		color: var(--muted-foreground);
		line-height: 1.4;
	}

	/* Scrollbar styling */
	.drawer-content::-webkit-scrollbar {
		width: 8px;
	}

	.drawer-content::-webkit-scrollbar-track {
		background: transparent;
	}

	.drawer-content::-webkit-scrollbar-thumb {
		background: color-mix(in srgb, var(--muted) 50%, transparent);
		border-radius: 4px;
	}

	.drawer-content::-webkit-scrollbar-thumb:hover {
		background: color-mix(in srgb, var(--muted) 70%, transparent);
	}
</style>
