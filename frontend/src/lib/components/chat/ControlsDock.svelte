<script lang="ts">
	/**
	 * ControlsDock - Bottom bar of the chat input island containing
	 * context usage, profile selector, override dropdowns,
	 * and file attach button.
	 */
	import { activeSession } from '$lib/stores/active-session';
	import { claudeAuthenticated } from '$lib/stores/auth';
	import { profiles } from '$lib/stores/app-data';
	import { DEFAULT_MODEL, getModelFamily } from '$lib/constants/models';
	import ContextUsageBar from './ContextUsageBar.svelte';
	import ProfileSelector from './ProfileSelector.svelte';
	import OverrideDropdown from './OverrideDropdown.svelte';

	interface Props {
		isUploading: boolean;
		onAttachClick: () => void;
	}

	let { isUploading, onAttachClick }: Props = $props();

	// Resolve the effective model to determine if fast mode is applicable
	const effectiveModel = $derived.by(() => {
		if ($activeSession.modelOverride) return $activeSession.modelOverride;
		const profile = $profiles.find(p => p.id === $activeSession.profile);
		return profile?.config?.model || DEFAULT_MODEL;
	});

	// Fast mode is only meaningful with Opus models (per-session state)
	const isOpusModel = $derived(getModelFamily(effectiveModel) === 'opus');
	const fastModeActive = $derived(!!($activeSession.fastMode && isOpusModel));

	const permissionModes = [
		{ value: null, label: 'Default', description: 'Use profile setting' },
		{ value: 'default', label: 'Ask', description: 'Confirm before tools' },
		{ value: 'auto', label: 'Auto', description: 'SDK decides permissions' },
		{ value: 'acceptEdits', label: 'Accept Edits', description: 'Auto-approve edits' },
		{ value: 'plan', label: 'Plan Only', description: 'Plan without executing' },
		{ value: 'bypassPermissions', label: 'Bypass', description: 'Execute all tools' }
	];

	const effortLevels = [
		{ value: null, label: 'Default', description: 'Use profile setting' },
		{ value: 'low', label: 'Low', description: 'Quick, minimal reasoning' },
		{ value: 'medium', label: 'Medium', description: 'Balanced reasoning' },
		{ value: 'high', label: 'High', description: 'Thorough reasoning' },
		{ value: 'max', label: 'Max', description: 'Maximum reasoning depth' },
		{ value: 'xhigh', label: 'XHigh', description: 'Opus 4.7+ extreme reasoning' }
	];
</script>

<div class="controls-dock">
	<!-- Left: Usage Indicator + Fast Mode Badge -->
	<div class="controls-left">
		<ContextUsageBar />
		{#if $activeSession.fastMode}
			<button
				type="button"
				class="fast-mode-badge"
				class:active={fastModeActive}
				class:inactive={!isOpusModel}
				title={isOpusModel ? 'Fast mode active' : 'Fast mode enabled but requires Opus model'}
				onclick={() => activeSession.setFastMode(false)}
			>
				<span class="fast-icon">↯</span>
				<span class="fast-label">FAST</span>
			</button>
		{/if}
	</div>

	<!-- Center: Profile selector -->
	<div class="controls-center">
		<ProfileSelector />
	</div>

	<!-- Right: Override buttons, Attach & Mic buttons -->
	<div class="controls-right">
		<!-- Effort Level Override -->
		<OverrideDropdown
			modes={effortLevels}
			currentValue={$activeSession.effortLevelOverride}
			onSelect={(v) => activeSession.setEffortLevelOverride(v)}
			title="Effort Level"
		>
			{#snippet icon()}
				<svg class="w-[16px] h-[16px]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.75" d="M13 10V3L4 14h7v7l9-11h-7z" />
				</svg>
			{/snippet}
		</OverrideDropdown>

		<!-- Permission Mode Override -->
		<OverrideDropdown
			modes={permissionModes}
			currentValue={$activeSession.permissionModeOverride}
			onSelect={(v) => activeSession.setPermissionModeOverride(v)}
			title="Permission Mode"
		>
			{#snippet icon()}
				<svg class="w-[16px] h-[16px]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.75" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
				</svg>
			{/snippet}
		</OverrideDropdown>

		<!-- Attach File Button -->
		<button
			type="button"
			onclick={onAttachClick}
			disabled={$activeSession.isStreaming || !$claudeAuthenticated || isUploading || $activeSession.isReopeningWorktree}
			class="dock-action-btn"
			title={isUploading ? 'Uploading...' : $activeSession.project ? 'Attach file' : 'Select a project to attach files'}
		>
			{#if isUploading}
				<svg class="w-[18px] h-[18px] animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
					<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
				</svg>
			{:else}
				<svg class="w-[18px] h-[18px]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.75" d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13" />
				</svg>
			{/if}
		</button>
	</div>
</div>

<style>
	/* ===== CONTROLS DOCK ===== */
	.controls-dock {
		display: flex;
		align-items: center;
		justify-content: space-between;
		background: color-mix(in oklch, var(--card) 60%, transparent);
		backdrop-filter: blur(20px);
		-webkit-backdrop-filter: blur(20px);
		border: 1px solid var(--border-subtle);
		border-top: none;
		border-radius: 0 0 calc(var(--radius, 0.75rem) * 1.33) calc(var(--radius, 0.75rem) * 1.33);
		padding: 8px 12px;
		overflow: visible;
	}

	.controls-left {
		display: flex;
		align-items: center;
		gap: 8px;
		flex: 1;
	}

	.controls-center {
		display: flex;
		align-items: center;
		gap: 4px;
		justify-content: center;
		flex: 2;
	}

	.controls-right {
		display: flex;
		align-items: center;
		gap: 4px;
		justify-content: flex-end;
		flex: 1;
	}

	/* ===== ACTION BUTTONS ===== */
	.dock-action-btn {
		position: relative;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 32px;
		height: 32px;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius, 0.75rem) * 0.67);
		cursor: pointer;
		color: var(--muted-foreground);
		transition: all 0.15s ease;
	}

	.dock-action-btn:hover:not(:disabled) {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.dock-action-btn:active:not(:disabled) {
		transform: scale(0.94);
	}

	.dock-action-btn:disabled {
		opacity: 0.3;
		cursor: not-allowed;
	}

	/* ===== FAST MODE BADGE ===== */
	.fast-mode-badge {
		display: flex;
		align-items: center;
		gap: 4px;
		padding: 3px 8px;
		border: 1px solid var(--border-subtle);
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		cursor: pointer;
		font-family: var(--font-mono);
		font-size: 10px;
		font-weight: 600;
		letter-spacing: 0.05em;
		transition: all 0.15s ease;
		background: transparent;
		line-height: 1;
	}

	.fast-mode-badge.active {
		background: color-mix(in oklch, var(--warning) 15%, transparent);
		border-color: color-mix(in oklch, var(--warning) 40%, transparent);
		color: var(--warning);
	}

	.fast-mode-badge.inactive {
		background: color-mix(in oklch, var(--muted) 30%, transparent);
		border-color: var(--border-subtle);
		color: var(--muted-foreground);
		opacity: 0.6;
	}

	.fast-mode-badge:hover {
		opacity: 0.8;
	}

	.fast-icon {
		font-size: 12px;
		line-height: 1;
	}

	.fast-label {
		line-height: 1;
	}

	/* Responsive: mobile tighter center gap */
	@media (max-width: 639px) {
		.controls-center {
			gap: 4px;
		}

		.fast-label {
			display: none;
		}

		.fast-mode-badge {
			padding: 3px 5px;
		}
	}
</style>
