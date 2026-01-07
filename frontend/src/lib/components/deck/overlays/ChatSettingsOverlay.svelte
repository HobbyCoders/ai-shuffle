<script lang="ts">
	/**
	 * ChatSettingsOverlay - Chat settings for interactive conversations
	 *
	 * Settings:
	 * - Profile (locked if inherited)
	 * - Project (locked if inherited)
	 * - Model (with reset to profile)
	 * - Permission Mode (with reset to profile)
	 * - Context Usage
	 *
	 * Note: Background agent launching has been moved to AgentCard for separation
	 * of concerns - chat cards are for interactive conversations, agent cards are
	 * for background autonomous tasks.
	 */

	import type { Project } from '$lib/stores/tabs';
	import type { Profile } from '$lib/api/client';

	interface ContextUsage {
		used: number;
		total: number;
		percentage: number;
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

		// Locked states
		isProfileLocked?: boolean;
		isProjectLocked?: boolean;

		// Options
		profiles?: Profile[];
		projects?: Project[];
		models?: { value: string; label: string }[];
		modes?: { value: string; label: string }[];

		// Callbacks
		onProfileChange?: (profileId: string) => void;
		onProjectChange?: (projectId: string) => void;
		onModelChange?: (model: string | null) => void;
		onModeChange?: (mode: string | null) => void;
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
		isProfileLocked = false,
		isProjectLocked = false,
		profiles = [],
		projects = [],
		models = [],
		modes = [],
		onProfileChange,
		onProjectChange,
		onModelChange,
		onModeChange,
		onClose
	}: Props = $props();

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

	function formatTokenCount(count: number): string {
		if (count >= 1000000) return `${(count / 1000000).toFixed(1)}M`;
		if (count >= 1000) return `${(count / 1000).toFixed(1)}k`;
		return count.toString();
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

	.setting-select {
		width: 100%;
		padding: 8px 12px;
		font-size: 0.875rem;
		color: var(--foreground);
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 6px;
		transition: border-color 0.15s, box-shadow 0.15s;
	}

	.setting-select:hover {
		border-color: color-mix(in srgb, var(--border) 70%, var(--foreground));
	}

	.setting-select:focus {
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
</style>
