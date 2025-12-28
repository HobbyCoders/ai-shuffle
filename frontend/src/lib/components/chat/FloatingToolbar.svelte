<script lang="ts">
	/**
	 * FloatingToolbar - Compact settings toolbar above chat input
	 *
	 * Displays:
	 * - Profile name (clickable to change)
	 * - Project name (clickable to change)
	 * - Model selector (Haiku/Sonnet/Opus)
	 * - Permission Mode selector (Plan/Ask/Auto)
	 * - Context usage meter
	 *
	 * Replaces the ChatSettingsOverlay for quick inline access
	 */

	import type { Profile } from '$lib/api/client';
	import type { Project } from '$lib/stores/tabs';

	interface ContextUsage {
		used: number;
		total: number;
		percentage: number;
	}

	interface Props {
		// Current values
		selectedProfile?: string | null;
		selectedProject?: string | null;
		effectiveModel?: string;
		effectiveMode?: string;
		contextUsage?: ContextUsage;

		// Override states
		isModelOverridden?: boolean;
		isModeOverridden?: boolean;

		// Options
		profiles?: Profile[];
		projects?: Project[];

		// Callbacks
		onProfileChange?: (profileId: string) => void;
		onProjectChange?: (projectId: string) => void;
		onModelChange?: (model: string | null) => void;
		onModeChange?: (mode: string | null) => void;
	}

	let {
		selectedProfile = null,
		selectedProject = null,
		effectiveModel = 'sonnet',
		effectiveMode = 'ask',
		contextUsage = { used: 0, total: 200000, percentage: 0 },
		isModelOverridden = false,
		isModeOverridden = false,
		profiles = [],
		projects = [],
		onProfileChange,
		onProjectChange,
		onModelChange,
		onModeChange
	}: Props = $props();

	// Dropdown states
	let showProfileDropdown = $state(false);
	let showProjectDropdown = $state(false);

	// Get display names
	const profileName = $derived(
		profiles.find((p) => p.id === selectedProfile)?.name || 'Profile'
	);
	const projectName = $derived(
		projects.find((p) => p.id === selectedProject)?.name || 'Project'
	);

	// Models and modes
	const models = [
		{ value: 'haiku', label: 'Haiku' },
		{ value: 'sonnet', label: 'Sonnet' },
		{ value: 'opus', label: 'Opus' }
	];

	const modes = [
		{ value: 'plan', label: 'Plan' },
		{ value: 'ask', label: 'Ask' },
		{ value: 'auto', label: 'Auto' }
	];

	// Context meter color
	const contextColor = $derived(
		contextUsage.percentage > 80
			? 'high'
			: contextUsage.percentage > 60
				? 'medium'
				: 'low'
	);

	// Handle clicks outside dropdowns
	function handleClickOutside(event: MouseEvent) {
		const target = event.target as HTMLElement;
		if (!target.closest('.dropdown-trigger')) {
			showProfileDropdown = false;
			showProjectDropdown = false;
		}
	}

	// Handle model selection
	function selectModel(model: string) {
		// If already the effective model and overridden, reset to profile default
		if (model === effectiveModel && isModelOverridden) {
			onModelChange?.(null);
		} else {
			onModelChange?.(model);
		}
	}

	// Handle mode selection
	function selectMode(mode: string) {
		// If already the effective mode and overridden, reset to profile default
		if (mode === effectiveMode && isModeOverridden) {
			onModeChange?.(null);
		} else {
			onModeChange?.(mode);
		}
	}
</script>

<svelte:window onclick={handleClickOutside} />

<div class="floating-toolbar-container">
	<div class="floating-toolbar">
		<!-- Profile & Project Segment -->
		<div class="toolbar-segment context-segment">
			<!-- Profile Dropdown -->
			<div class="dropdown-wrapper">
				<button
					type="button"
					class="toolbar-btn dropdown-trigger"
					class:active={showProfileDropdown}
					class:unset={!selectedProfile}
					onclick={() => {
						showProfileDropdown = !showProfileDropdown;
						showProjectDropdown = false;
					}}
				>
					<svg class="btn-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path
							stroke-linecap="round"
							stroke-linejoin="round"
							stroke-width="2"
							d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"
						/>
					</svg>
					<span class="btn-label">{profileName}</span>
					<svg class="chevron" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
					</svg>
				</button>

				{#if showProfileDropdown}
					<div class="dropdown-menu">
						{#each profiles as profile}
							<button
								type="button"
								class="dropdown-item"
								class:selected={profile.id === selectedProfile}
								onclick={() => {
									onProfileChange?.(profile.id);
									showProfileDropdown = false;
								}}
							>
								{profile.name}
								{#if profile.id === selectedProfile}
									<svg class="check-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
									</svg>
								{/if}
							</button>
						{/each}
					</div>
				{/if}
			</div>

			<!-- Project Dropdown -->
			<div class="dropdown-wrapper">
				<button
					type="button"
					class="toolbar-btn dropdown-trigger"
					class:active={showProjectDropdown}
					class:unset={!selectedProject}
					onclick={() => {
						showProjectDropdown = !showProjectDropdown;
						showProfileDropdown = false;
					}}
				>
					<svg class="btn-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path
							stroke-linecap="round"
							stroke-linejoin="round"
							stroke-width="2"
							d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"
						/>
					</svg>
					<span class="btn-label">{projectName}</span>
					<svg class="chevron" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
					</svg>
				</button>

				{#if showProjectDropdown}
					<div class="dropdown-menu">
						{#each projects as project}
							<button
								type="button"
								class="dropdown-item"
								class:selected={project.id === selectedProject}
								onclick={() => {
									onProjectChange?.(project.id);
									showProjectDropdown = false;
								}}
							>
								{project.name}
								{#if project.id === selectedProject}
									<svg class="check-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
									</svg>
								{/if}
							</button>
						{/each}
					</div>
				{/if}
			</div>
		</div>

		<div class="toolbar-divider"></div>

		<!-- Model Segment -->
		<div class="toolbar-segment model-segment">
			{#each models as model}
				<button
					type="button"
					class="toolbar-btn model-btn"
					class:active={effectiveModel === model.value}
					class:overridden={effectiveModel === model.value && isModelOverridden}
					onclick={() => selectModel(model.value)}
					title={effectiveModel === model.value && isModelOverridden
						? 'Click to reset to profile default'
						: `Select ${model.label}`}
				>
					{model.label}
				</button>
			{/each}
		</div>

		<div class="toolbar-divider"></div>

		<!-- Mode Segment -->
		<div class="toolbar-segment mode-segment">
			{#each modes as mode}
				<button
					type="button"
					class="toolbar-btn mode-btn"
					class:active={effectiveMode === mode.value}
					class:overridden={effectiveMode === mode.value && isModeOverridden}
					onclick={() => selectMode(mode.value)}
					title={effectiveMode === mode.value && isModeOverridden
						? 'Click to reset to profile default'
						: `Select ${mode.label} mode`}
				>
					{mode.label}
				</button>
			{/each}
		</div>

		<div class="toolbar-divider"></div>

		<!-- Context Meter -->
		<div class="toolbar-meter" title="{Math.round(contextUsage.used / 1000)}k / {Math.round(contextUsage.total / 1000)}k tokens">
			<div class="meter-bar">
				<div
					class="meter-fill {contextColor}"
					style:width="{Math.min(contextUsage.percentage, 100)}%"
				></div>
			</div>
			<span class="meter-text {contextColor}">{Math.round(contextUsage.percentage)}%</span>
		</div>
	</div>
</div>

<style>
	.floating-toolbar-container {
		display: flex;
		justify-content: center;
		padding: 0 0.75rem 0.5rem;
	}

	.floating-toolbar {
		display: flex;
		align-items: center;
		gap: 0.25rem;
		padding: 0.375rem;
		background: var(--glass-bg);
		backdrop-filter: blur(var(--glass-blur));
		-webkit-backdrop-filter: blur(var(--glass-blur));
		border: 1px solid var(--border);
		border-radius: 1.5rem;
		box-shadow: var(--shadow-m);
	}

	.toolbar-segment {
		display: flex;
		align-items: center;
		gap: 0.125rem;
		padding: 0 0.25rem;
	}

	.toolbar-divider {
		width: 1px;
		height: 1.25rem;
		background: var(--border);
		margin: 0 0.25rem;
		opacity: 0.6;
	}

	/* Base button style */
	.toolbar-btn {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		padding: 0.4rem 0.75rem;
		background: transparent;
		border: none;
		border-radius: 1rem;
		font-size: 0.75rem;
		font-weight: 500;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 0.15s ease;
		white-space: nowrap;
	}

	.toolbar-btn:hover {
		background: var(--accent);
		color: var(--foreground);
	}

	.toolbar-btn.active {
		background: var(--primary);
		color: var(--primary-foreground);
	}

	.toolbar-btn.unset {
		color: var(--warning);
	}

	/* Icon sizes */
	.btn-icon {
		width: 0.875rem;
		height: 0.875rem;
		flex-shrink: 0;
	}

	.chevron {
		width: 0.75rem;
		height: 0.75rem;
		opacity: 0.6;
		flex-shrink: 0;
	}

	.btn-label {
		max-width: 6rem;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	/* Model button with gold accent when active */
	.toolbar-btn.model-btn.active {
		background: linear-gradient(135deg, var(--gold) 0%, var(--gold-dim) 100%);
		color: oklch(0.15 0.02 85);
		font-weight: 600;
	}

	/* Overridden indicator - subtle dot */
	.toolbar-btn.overridden::after {
		content: '';
		width: 4px;
		height: 4px;
		background: currentColor;
		border-radius: 50%;
		opacity: 0.6;
		margin-left: 0.125rem;
	}

	/* Mode button */
	.toolbar-btn.mode-btn.active {
		background: var(--primary);
		color: var(--primary-foreground);
	}

	/* Context meter */
	.toolbar-meter {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.375rem 0.75rem;
	}

	.meter-bar {
		width: 3.5rem;
		height: 0.25rem;
		background: var(--muted);
		border-radius: 0.125rem;
		overflow: hidden;
	}

	.meter-fill {
		height: 100%;
		border-radius: 0.125rem;
		transition: width 0.3s ease;
	}

	.meter-fill.low {
		background: var(--success);
	}
	.meter-fill.medium {
		background: var(--warning);
	}
	.meter-fill.high {
		background: var(--destructive);
	}

	.meter-text {
		font-size: 0.6875rem;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
	}

	.meter-text.low {
		color: var(--success);
	}
	.meter-text.medium {
		color: var(--warning);
	}
	.meter-text.high {
		color: var(--destructive);
	}

	/* Dropdown wrapper */
	.dropdown-wrapper {
		position: relative;
	}

	.dropdown-menu {
		position: absolute;
		top: calc(100% + 0.5rem);
		left: 0;
		min-width: 12rem;
		max-height: 16rem;
		overflow-y: auto;
		background: var(--popover);
		border: 1px solid var(--border);
		border-radius: 0.75rem;
		box-shadow: var(--shadow-l);
		z-index: 100;
		padding: 0.375rem;
		animation: dropdownIn 0.15s ease;
	}

	@keyframes dropdownIn {
		from {
			opacity: 0;
			transform: translateY(-4px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	.dropdown-item {
		display: flex;
		align-items: center;
		justify-content: space-between;
		width: 100%;
		padding: 0.5rem 0.75rem;
		background: transparent;
		border: none;
		border-radius: 0.5rem;
		font-size: 0.8125rem;
		color: var(--foreground);
		cursor: pointer;
		text-align: left;
		transition: background-color 0.1s;
	}

	.dropdown-item:hover {
		background: var(--accent);
	}

	.dropdown-item.selected {
		background: color-mix(in oklch, var(--primary) 15%, transparent);
		color: var(--primary);
	}

	.check-icon {
		width: 1rem;
		height: 1rem;
		flex-shrink: 0;
	}

	/* Responsive: Stack on small screens */
	@media (max-width: 640px) {
		.floating-toolbar {
			flex-wrap: wrap;
			justify-content: center;
			border-radius: 1rem;
			gap: 0.125rem;
			padding: 0.375rem;
		}

		.toolbar-divider {
			display: none;
		}

		.toolbar-segment {
			padding: 0.125rem;
		}

		.toolbar-btn {
			padding: 0.35rem 0.5rem;
			font-size: 0.6875rem;
		}

		.btn-label {
			max-width: 4.5rem;
		}

		.context-segment {
			width: 100%;
			justify-content: center;
			padding-bottom: 0.25rem;
			border-bottom: 1px solid var(--border);
			margin-bottom: 0.125rem;
		}

		.meter-bar {
			width: 2.5rem;
		}
	}
</style>
