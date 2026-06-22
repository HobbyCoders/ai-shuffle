<script lang="ts">
	/**
	 * ControlsDock - Bottom bar of the chat input island containing
	 * context usage, profile selector, override dropdowns,
	 * and file attach button.
	 */
	import { activeSession } from '$lib/stores/active-session';
	import { claudeAuthenticated } from '$lib/stores/auth.svelte';
	import { profiles } from '$lib/stores/app-data.svelte';
	import ContextUsageBar from './ContextUsageBar.svelte';
	import UsageLimitIndicator from './UsageLimitIndicator.svelte';
	import OverrideDropdown from './OverrideDropdown.svelte';
	import { onMount } from 'svelte';
	import { Monitor } from 'lucide-svelte';
	import { computerUse } from '$lib/stores/computerUse.svelte';

	interface Props {
		isUploading: boolean;
		onAttachClick: () => void;
	}

	let { isUploading, onAttachClick }: Props = $props();

	// Computer-use toolbar toggle. A single monitor icon: lit when the agent is
	// allowed to control the screen, dimmed when paused (the process-global
	// emergency stop halts all synthetic input immediately). Polling starts only
	// while the active profile enables the capability.
	let cuBusy = $state(false);

	async function toggleComputerUse() {
		if (cuBusy) return;
		cuBusy = true;
		try {
			await computerUse.toggleStopped();
		} finally {
			cuBusy = false;
		}
	}

	// Only one override dropdown may be open at a time — opening one closes the other.
	let openDropdown = $state<'effort' | 'permission' | null>(null);

	// The effort/permission dropdowns reflect the *effective* value: the per-session
	// override if one is set, otherwise the active profile's setting. They list only
	// real SDK values (no synthetic "Default") — selecting the profile's own value
	// clears the override so the session keeps inheriting from the profile.
	const activeProfile = $derived($profiles.find((p) => p.id === $activeSession.profile));
	const profileEffort = $derived(activeProfile?.config?.effort ?? null);
	const profilePermissionMode = $derived(activeProfile?.config?.permission_mode ?? null);
	const effortValue = $derived($activeSession.effortLevelOverride ?? profileEffort);
	const permissionValue = $derived($activeSession.permissionModeOverride ?? profilePermissionMode);
	const computerUseEnabled = $derived(!!activeProfile?.config?.computer_use?.enabled);

	// Poll computer-use status (shared store) only while the active profile
	// enables the capability; stop when it doesn't, to avoid idle polling.
	$effect(() => {
		if (!computerUseEnabled) return;
		computerUse.start();
		return () => computerUse.stop();
	});

	const permissionModes = [
		{ value: 'default', label: 'Ask', description: 'Confirm before tools' },
		{ value: 'auto', label: 'Auto', description: 'Model classifier decides per tool call' },
		{ value: 'acceptEdits', label: 'Accept Edits', description: 'Auto-approve edits' },
		{ value: 'plan', label: 'Plan Only', description: 'Plan without executing' },
		{ value: 'bypassPermissions', label: 'Bypass', description: 'Execute all tools' }
	];

	const effortLevels = [
		{ value: 'low', label: 'Low', description: 'Quick, minimal reasoning' },
		{ value: 'medium', label: 'Medium', description: 'Balanced reasoning' },
		{ value: 'high', label: 'High', description: 'Thorough reasoning' },
		{ value: 'xhigh', label: 'XHigh', description: 'Extreme reasoning' },
		{ value: 'max', label: 'Max', description: 'Maximum reasoning depth' }
	];
</script>

<div class="controls-dock">
	<!-- Left: Usage Indicator -->
	<div class="controls-left">
		<ContextUsageBar />
		<UsageLimitIndicator />
	</div>

	<!-- Right: Override buttons, Attach & Mic buttons -->
	<div class="controls-right">
		<!-- Effort Level Override -->
		<OverrideDropdown
			modes={effortLevels}
			currentValue={effortValue}
			isOverridden={$activeSession.effortLevelOverride !== null}
			isOpen={openDropdown === 'effort'}
			onOpenChange={(open) => (openDropdown = open ? 'effort' : null)}
			onSelect={(v) => activeSession.setEffortLevelOverride(v === profileEffort ? null : v)}
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
			currentValue={permissionValue}
			isOverridden={$activeSession.permissionModeOverride !== null}
			isOpen={openDropdown === 'permission'}
			onOpenChange={(open) => (openDropdown = open ? 'permission' : null)}
			onSelect={(v) => activeSession.setPermissionModeOverride(v === profilePermissionMode ? null : v)}
			title="Permission Mode"
		>
			{#snippet icon()}
				<svg class="w-[16px] h-[16px]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.75" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
				</svg>
			{/snippet}
		</OverrideDropdown>

		<!-- Computer Use toggle (only when the profile enables it). A single
		     monitor icon: lit = the agent may control the screen; dimmed =
		     paused. Clicking toggles control on/off. -->
		{#if computerUseEnabled}
			<button
				type="button"
				onclick={toggleComputerUse}
				disabled={cuBusy}
				class="dock-action-btn dock-action-btn--cu"
				class:cu-on={!computerUse.stopped}
				class:cu-active={computerUse.active && !computerUse.stopped}
				role="switch"
				aria-checked={!computerUse.stopped}
				title={computerUse.stopped
					? 'Computer control paused — click to allow the agent to control the screen'
					: computerUse.active
						? `Agent is controlling ${computerUse.displayLabel ?? 'your screen'} — click to pause`
						: 'Computer control enabled — click to pause'}
				aria-label={computerUse.stopped ? 'Enable computer control' : 'Pause computer control'}
			>
				<Monitor size={18} strokeWidth={1.75} />
			</button>
		{/if}

		<!-- Attach File Button -->
		<button
			type="button"
			onclick={onAttachClick}
			disabled={!$claudeAuthenticated || isUploading || $activeSession.isReopeningWorktree}
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
		/* The Effort/Permission dropdowns inside this dock open UPWARD, so they
		   overlap the `.input-wrapper` (the message box) that sits directly above
		   the dock in ChatInput. That wrapper sets `z-index: 20` and establishes a
		   stacking context (backdrop-filter), so without an explicit z-index here
		   the whole dock — and its z-50 dropdowns — stacks below the wrapper and the
		   popups render BEHIND the message box. Lifting the dock above the wrapper
		   keeps the override popups fully visible on top. */
		position: relative;
		z-index: 30;
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

	/* Computer-use toggle: dimmed when paused, lit (primary) when control is
	   allowed, and gently pulsing while the agent is actively driving a screen. */
	.dock-action-btn--cu {
		color: var(--muted-foreground);
	}

	.dock-action-btn--cu.cu-on {
		color: var(--primary);
	}

	.dock-action-btn--cu:hover:not(:disabled) {
		background: var(--hover-overlay);
		color: var(--primary);
	}

	.dock-action-btn--cu.cu-active {
		color: var(--primary);
		animation: cu-pulse 1.4s ease-in-out infinite;
	}

	@keyframes cu-pulse {
		0%, 100% { opacity: 1; }
		50% { opacity: 0.45; }
	}
</style>
