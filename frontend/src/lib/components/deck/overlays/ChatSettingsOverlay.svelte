<script lang="ts">
	/**
	 * ChatSettingsOverlay - Full chat settings with background agent configuration
	 *
	 * Settings:
	 * - Profile (locked if inherited)
	 * - Project (locked if inherited)
	 * - Model (with reset to profile)
	 * - Permission Mode (with reset to profile)
	 * - Context Usage
	 * - Background Mode (toggle + full config when enabled)
	 *   - Task Name
	 *   - Branch Selector
	 *   - Auto-PR
	 *   - Auto-Merge
	 *   - Max Duration
	 */

	import type { Project } from '$lib/stores/tabs';
	import type { Profile } from '$lib/api/client';
	import { GitBranch, Plus, Loader2 } from 'lucide-svelte';

	interface ContextUsage {
		used: number;
		total: number;
		percentage: number;
	}

	interface BackgroundAgentConfig {
		taskName: string;
		branch: string;
		createNewBranch: boolean;
		autoPR: boolean;
		autoMerge: boolean;
		maxDurationMinutes: number;
	}

	interface Props {
		// Current values
		selectedProfile?: string | null;
		selectedProject?: string | null;
		selectedModel?: string | null;
		selectedMode?: string | null;
		effectiveModel?: string;
		effectiveMode?: string;
		contextUsage?: ContextUsage;
		isBackgroundMode?: boolean;

		// Locked states
		isProfileLocked?: boolean;
		isProjectLocked?: boolean;

		// Options
		profiles?: Profile[];
		projects?: Project[];
		models?: { value: string; label: string }[];
		modes?: { value: string; label: string }[];

		// Branch data
		branches?: string[];
		currentBranch?: string;
		defaultBranch?: string;
		loadingBranches?: boolean;

		// Background config
		backgroundConfig?: BackgroundAgentConfig;

		// Callbacks
		onProfileChange?: (profileId: string) => void;
		onProjectChange?: (projectId: string) => void;
		onModelChange?: (model: string | null) => void;
		onModeChange?: (mode: string | null) => void;
		onBackgroundModeChange?: (enabled: boolean) => void;
		onBackgroundConfigChange?: (config: Partial<BackgroundAgentConfig>) => void;
		onClose?: () => void;
	}

	let {
		selectedProfile = null,
		selectedProject = null,
		selectedModel = null,
		selectedMode = null,
		effectiveModel = 'sonnet',
		effectiveMode = 'ask',
		contextUsage = { used: 0, total: 200000, percentage: 0 },
		isBackgroundMode = false,
		isProfileLocked = false,
		isProjectLocked = false,
		profiles = [],
		projects = [],
		models = [],
		modes = [],
		branches = ['main'],
		currentBranch = 'main',
		defaultBranch = 'main',
		loadingBranches = false,
		backgroundConfig = {
			taskName: '',
			branch: 'main',
			createNewBranch: false,
			autoPR: true,  // Default to true - auto-create PR when background agent completes
			autoMerge: false,
			maxDurationMinutes: 30
		},
		onProfileChange,
		onProjectChange,
		onModelChange,
		onModeChange,
		onBackgroundModeChange,
		onBackgroundConfigChange,
		onClose
	}: Props = $props();

	// Local state for new branch creation
	let showNewBranchInput = $state(false);
	let newBranchName = $state('');

	// Derived values
	const isModelOverridden = $derived(selectedModel !== null);
	const isModeOverridden = $derived(selectedMode !== null);

	const effectiveModelLabel = $derived(
		models.find(m => m.value === effectiveModel)?.label || effectiveModel || 'Sonnet'
	);
	const effectiveModeLabel = $derived(
		modes.find(m => m.value === effectiveMode)?.label || effectiveMode || 'Ask'
	);

	const contextColor = $derived(
		contextUsage.percentage > 80 ? 'var(--destructive)' :
		contextUsage.percentage > 60 ? 'var(--warning)' :
		'var(--success)'
	);

	const durationOptions = [
		{ value: 15, label: '15 minutes' },
		{ value: 30, label: '30 minutes' },
		{ value: 60, label: '1 hour' },
		{ value: 120, label: '2 hours' },
		{ value: 0, label: 'Unlimited' }
	];

	function formatTokenCount(count: number): string {
		if (count >= 1000000) return `${(count / 1000000).toFixed(1)}M`;
		if (count >= 1000) return `${(count / 1000).toFixed(1)}k`;
		return count.toString();
	}

	function handleBranchChange(value: string) {
		if (value === '__new__') {
			showNewBranchInput = true;
		} else {
			onBackgroundConfigChange?.({ branch: value, createNewBranch: false });
		}
	}

	function handleNewBranchCreate() {
		if (newBranchName.trim()) {
			onBackgroundConfigChange?.({
				branch: newBranchName.trim(),
				createNewBranch: true
			});
			showNewBranchInput = false;
			newBranchName = '';
		}
	}

	function handleNewBranchCancel() {
		showNewBranchInput = false;
		newBranchName = '';
	}
</script>

<div class="settings-content">
	<!-- Context Section -->
	<div class="settings-section">
		<div class="section-title">Context</div>

		<!-- Profile -->
		<div class="setting-group">
			<div class="setting-label-row">
				<label for="overlay-profile" class="setting-label">Profile</label>
				{#if isProfileLocked}
					<span class="locked-badge">Locked</span>
				{/if}
			</div>
			<select
				id="overlay-profile"
				value={selectedProfile || ''}
				onchange={(e) => onProfileChange?.((e.target as HTMLSelectElement).value)}
				class="setting-select"
				disabled={isProfileLocked}
			>
				<option value="" disabled>Select Profile</option>
				{#each profiles as profile}
					<option value={profile.id}>{profile.name}</option>
				{/each}
			</select>
		</div>

		<!-- Project -->
		<div class="setting-group">
			<div class="setting-label-row">
				<label for="overlay-project" class="setting-label">Project</label>
				{#if isProjectLocked}
					<span class="locked-badge">Locked</span>
				{/if}
			</div>
			<select
				id="overlay-project"
				value={selectedProject || ''}
				onchange={(e) => onProjectChange?.((e.target as HTMLSelectElement).value)}
				class="setting-select"
				disabled={isProjectLocked}
			>
				<option value="" disabled>Select Project</option>
				{#each projects as project}
					<option value={project.id}>{project.name}</option>
				{/each}
			</select>
		</div>
	</div>

	<!-- Model Section -->
	<div class="settings-section">
		<div class="section-title">Model</div>

		<!-- Model Selector -->
		<div class="setting-group">
			<div class="setting-label-row">
				<label for="overlay-model" class="setting-label">Model</label>
				{#if isModelOverridden}
					<button
						type="button"
						class="reset-btn"
						onclick={() => onModelChange?.(null)}
					>
						Reset
					</button>
				{:else}
					<span class="inherited-badge">From Profile</span>
				{/if}
			</div>
			<select
				id="overlay-model"
				value={effectiveModel}
				onchange={(e) => onModelChange?.((e.target as HTMLSelectElement).value)}
				class="setting-select"
				class:inherited={!isModelOverridden}
			>
				{#each models as model}
					<option value={model.value}>{model.label}</option>
				{/each}
			</select>
		</div>

		<!-- Mode Selector -->
		<div class="setting-group">
			<div class="setting-label-row">
				<label for="overlay-mode" class="setting-label">Permission Mode</label>
				{#if isModeOverridden}
					<button
						type="button"
						class="reset-btn"
						onclick={() => onModeChange?.(null)}
					>
						Reset
					</button>
				{:else}
					<span class="inherited-badge">From Profile</span>
				{/if}
			</div>
			<select
				id="overlay-mode"
				value={effectiveMode}
				onchange={(e) => onModeChange?.((e.target as HTMLSelectElement).value)}
				class="setting-select"
				class:inherited={!isModeOverridden}
			>
				{#each modes as mode}
					<option value={mode.value}>{mode.label}</option>
				{/each}
			</select>
		</div>
	</div>

	<!-- Context Usage -->
	<div class="settings-section">
		<div class="section-title">Context Usage</div>
		<div class="context-display">
			<div class="context-bar">
				<div
					class="context-fill"
					style:width="{contextUsage.percentage}%"
					style:background={contextColor}
				></div>
			</div>
			<div class="context-text">
				<span class="context-percentage" style:color={contextColor}>
					{Math.round(contextUsage.percentage)}%
				</span>
				<span class="context-tokens">
					{formatTokenCount(contextUsage.used)} / {formatTokenCount(contextUsage.total)}
				</span>
			</div>
		</div>
	</div>

	<!-- Background Mode Section -->
	<div class="settings-section">
		<label class="toggle-label">
			<input
				type="checkbox"
				checked={isBackgroundMode}
				onchange={(e) => onBackgroundModeChange?.((e.target as HTMLInputElement).checked)}
				class="toggle-checkbox"
			/>
			<span class="toggle-indicator"></span>
			<div class="toggle-text">
				<span class="toggle-title">Run in Background</span>
				<span class="toggle-description">
					Launch as autonomous background agent
				</span>
			</div>
		</label>

		<!-- Background Agent Config (shown when enabled) -->
		{#if isBackgroundMode}
			<div class="background-config">
				<!-- Task Name -->
				<div class="setting-group">
					<label for="task-name" class="setting-label">Task Name</label>
					<input
						id="task-name"
						type="text"
						value={backgroundConfig.taskName}
						oninput={(e) => onBackgroundConfigChange?.({ taskName: (e.target as HTMLInputElement).value })}
						placeholder="Auto-generated from prompt"
						class="setting-input"
					/>
				</div>

				<!-- Branch Selector -->
				<div class="setting-group">
					<label for="branch-select" class="setting-label">
						<GitBranch size={14} strokeWidth={1.5} />
						Branch
					</label>
					{#if showNewBranchInput}
						<div class="new-branch-input">
							<input
								type="text"
								bind:value={newBranchName}
								placeholder="feature/my-branch"
								class="setting-input"
								autofocus
							/>
							<button
								type="button"
								class="btn-sm btn-primary"
								onclick={handleNewBranchCreate}
								disabled={!newBranchName.trim()}
							>
								Create
							</button>
							<button
								type="button"
								class="btn-sm btn-ghost"
								onclick={handleNewBranchCancel}
							>
								Cancel
							</button>
						</div>
					{:else}
						<div class="branch-select-row">
							<select
								id="branch-select"
								value={backgroundConfig.branch}
								onchange={(e) => handleBranchChange((e.target as HTMLSelectElement).value)}
								class="setting-select"
								disabled={loadingBranches}
							>
								{#if loadingBranches}
									<option value="">Loading...</option>
								{:else}
									{#each branches as branch}
										<option value={branch}>{branch}</option>
									{/each}
									<option value="__new__">+ New Branch</option>
								{/if}
							</select>
							{#if loadingBranches}
								<Loader2 size={16} class="loading-spinner" />
							{/if}
						</div>
					{/if}
					{#if backgroundConfig.createNewBranch}
						<span class="branch-note">Will create from {defaultBranch}</span>
					{/if}
				</div>

				<!-- Auto-PR -->
				<label class="checkbox-label">
					<input
						type="checkbox"
						checked={backgroundConfig.autoPR}
						onchange={(e) => onBackgroundConfigChange?.({ autoPR: (e.target as HTMLInputElement).checked })}
						class="checkbox-input"
					/>
					<span class="checkbox-box"></span>
					<span class="checkbox-text">Auto-create PR when done</span>
				</label>

				<!-- Auto-Merge (dangerous) -->
				<label class="checkbox-label" class:dangerous={backgroundConfig.autoMerge}>
					<input
						type="checkbox"
						checked={backgroundConfig.autoMerge}
						onchange={(e) => onBackgroundConfigChange?.({ autoMerge: (e.target as HTMLInputElement).checked })}
						class="checkbox-input"
					/>
					<span class="checkbox-box"></span>
					<span class="checkbox-text">
						Auto-merge to {defaultBranch}
						{#if backgroundConfig.autoMerge}
							<span class="warning-badge">Dangerous</span>
						{/if}
					</span>
				</label>

				<!-- Max Duration -->
				<div class="setting-group">
					<label for="max-duration" class="setting-label">Max Duration</label>
					<select
						id="max-duration"
						value={backgroundConfig.maxDurationMinutes}
						onchange={(e) => onBackgroundConfigChange?.({ maxDurationMinutes: parseInt((e.target as HTMLSelectElement).value) })}
						class="setting-select"
					>
						{#each durationOptions as option}
							<option value={option.value}>{option.label}</option>
						{/each}
					</select>
				</div>

			</div>
		{/if}
	</div>
</div>

<style>
	.settings-content {
		padding: 16px;
		display: flex;
		flex-direction: column;
		gap: 16px;
	}

	.settings-section {
		display: flex;
		flex-direction: column;
		gap: 12px;
		padding-bottom: 16px;
		border-bottom: 1px solid var(--border);
	}

	.settings-section:last-child {
		border-bottom: none;
		padding-bottom: 0;
	}

	.section-title {
		font-size: 0.6875rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--muted-foreground);
	}

	.setting-group {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.setting-label-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
	}

	.setting-label {
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--foreground);
		display: flex;
		align-items: center;
		gap: 6px;
	}

	.locked-badge {
		font-size: 0.625rem;
		font-weight: 500;
		color: var(--muted-foreground);
		background: var(--muted);
		padding: 2px 6px;
		border-radius: 9999px;
		text-transform: uppercase;
	}

	.inherited-badge {
		font-size: 0.625rem;
		font-weight: 500;
		color: var(--primary);
		opacity: 0.8;
	}

	.reset-btn {
		font-size: 0.625rem;
		font-weight: 500;
		color: var(--primary);
		background: transparent;
		border: none;
		padding: 2px 6px;
		border-radius: 4px;
		cursor: pointer;
		transition: background-color 0.15s;
	}

	.reset-btn:hover {
		background: color-mix(in srgb, var(--primary) 15%, transparent);
	}

	.setting-select,
	.setting-input {
		width: 100%;
		padding: 8px 12px;
		font-size: 0.875rem;
		color: var(--foreground);
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 6px;
		transition: border-color 0.15s, box-shadow 0.15s;
	}

	.setting-select:hover,
	.setting-input:hover {
		border-color: color-mix(in srgb, var(--border) 70%, var(--foreground));
	}

	.setting-select:focus,
	.setting-input:focus {
		outline: none;
		border-color: var(--primary);
		box-shadow: 0 0 0 2px color-mix(in srgb, var(--primary) 15%, transparent);
	}

	.setting-select:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	.setting-select.inherited {
		border-style: dashed;
		opacity: 0.85;
	}

	/* Context Display */
	.context-display {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.context-bar {
		width: 100%;
		height: 6px;
		background: var(--muted);
		border-radius: 9999px;
		overflow: hidden;
	}

	.context-fill {
		height: 100%;
		border-radius: 9999px;
		transition: width 0.3s ease;
	}

	.context-text {
		display: flex;
		align-items: center;
		justify-content: space-between;
		font-size: 0.75rem;
	}

	.context-percentage {
		font-weight: 600;
	}

	.context-tokens {
		color: var(--muted-foreground);
	}

	/* Toggle */
	.toggle-label {
		display: flex;
		align-items: flex-start;
		gap: 10px;
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
		width: 40px;
		height: 22px;
		background: var(--muted);
		border-radius: 9999px;
		position: relative;
		transition: background-color 0.2s;
	}

	.toggle-indicator::before {
		content: '';
		position: absolute;
		top: 2px;
		left: 2px;
		width: 18px;
		height: 18px;
		background: white;
		border-radius: 50%;
		transition: transform 0.2s;
		box-shadow: 0 1px 3px rgba(0, 0, 0, 0.2);
	}

	.toggle-checkbox:checked + .toggle-indicator {
		background: var(--primary);
	}

	.toggle-checkbox:checked + .toggle-indicator::before {
		transform: translateX(18px);
	}

	.toggle-text {
		flex: 1;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.toggle-title {
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--foreground);
	}

	.toggle-description {
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	/* Background Config */
	.background-config {
		display: flex;
		flex-direction: column;
		gap: 12px;
		padding: 12px;
		background: var(--muted);
		border: 1px solid var(--border);
		border-radius: 8px;
		margin-top: 8px;
	}

	.branch-select-row {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.branch-select-row .setting-select {
		flex: 1;
	}

	.branch-select-row :global(.loading-spinner) {
		animation: spin 1s linear infinite;
		color: var(--muted-foreground);
	}

	@keyframes spin {
		from { transform: rotate(0deg); }
		to { transform: rotate(360deg); }
	}

	.new-branch-input {
		display: flex;
		gap: 8px;
	}

	.new-branch-input .setting-input {
		flex: 1;
	}

	.btn-sm {
		padding: 6px 12px;
		font-size: 0.75rem;
		font-weight: 500;
		border-radius: 4px;
		cursor: pointer;
		transition: all 0.15s;
	}

	.btn-primary {
		background: var(--primary);
		color: var(--primary-foreground);
		border: none;
	}

	.btn-primary:hover:not(:disabled) {
		opacity: 0.9;
	}

	.btn-primary:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.btn-ghost {
		background: transparent;
		color: var(--muted-foreground);
		border: 1px solid var(--border);
	}

	.btn-ghost:hover {
		background: var(--accent);
		color: var(--foreground);
	}

	.branch-note {
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		font-style: italic;
	}

	/* Checkbox */
	.checkbox-label {
		display: flex;
		align-items: center;
		gap: 8px;
		cursor: pointer;
		user-select: none;
		font-size: 0.8125rem;
		color: var(--foreground);
	}

	.checkbox-label.dangerous {
		color: var(--warning);
	}

	.checkbox-input {
		position: absolute;
		opacity: 0;
		pointer-events: none;
	}

	.checkbox-box {
		width: 16px;
		height: 16px;
		border: 1.5px solid var(--border);
		border-radius: 4px;
		background: var(--background);
		display: flex;
		align-items: center;
		justify-content: center;
		transition: all 0.15s;
		flex-shrink: 0;
	}

	.checkbox-input:checked + .checkbox-box {
		background: var(--primary);
		border-color: var(--primary);
	}

	.checkbox-input:checked + .checkbox-box::after {
		content: '';
		width: 4px;
		height: 8px;
		border: solid white;
		border-width: 0 2px 2px 0;
		transform: rotate(45deg);
		margin-bottom: 2px;
	}

	.checkbox-text {
		flex: 1;
	}

	.warning-badge {
		font-size: 0.625rem;
		font-weight: 600;
		color: var(--warning);
		background: color-mix(in oklch, var(--warning) 15%, transparent);
		padding: 2px 6px;
		border-radius: 4px;
		margin-left: 6px;
	}
</style>
