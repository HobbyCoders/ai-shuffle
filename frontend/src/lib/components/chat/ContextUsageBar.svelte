<script lang="ts">
	/**
	 * ContextUsageBar - Shows context window usage as a progress bar with percentage.
	 * Reads directly from stores.
	 */
	import { activeSession } from '$lib/stores/active-session';
	import { profiles } from '$lib/stores/app-data';
	import { AUTO_COMPACTION_BASE, CONTEXT_MAX } from '$lib/constants/chat';
	import { sessionStateManager } from '$lib/stores/session/session-state.svelte';

	function formatTokenCount(count: number): string {
		if (count >= 1000000) {
			return `${(count / 1000000).toFixed(1)}M`;
		}
		if (count >= 1000) {
			return `${(count / 1000).toFixed(1)}k`;
		}
		return count.toString();
	}

	// Get current profile to access env vars for auto-compaction reserve calculation
	const currentProfile = $derived($profiles.find(p => p.id === $activeSession.profile));

	// Auto-compaction reserve: 13k base + CLAUDE_CODE_MAX_OUTPUT_TOKENS from profile env vars
	const maxOutputTokens = $derived(
		parseInt(currentProfile?.config?.env?.CLAUDE_CODE_MAX_OUTPUT_TOKENS || '0', 10) || 0
	);
	const autoCompactionReserve = $derived(AUTO_COMPACTION_BASE + maxOutputTokens);

	// Context usage: prefer real-time session state (updated by poller during streaming),
	// fall back to tab store values for idle/resumed sessions.
	const sessionUsage = $derived(
		$activeSession.sessionId
			? sessionStateManager.getSession($activeSession.sessionId)?.usage
			: undefined
	);
	// IMPORTANT: The fallback must use $activeSession.contextUsed (per-turn context window
	// tokens set by onStreamEnd/bridge), NOT totalTokensIn which is CUMULATIVE across all
	// turns and would massively overcount for multi-turn sessions (showing 100% always).
	const baseContextUsed = $derived(
		(sessionUsage && sessionUsage.contextUsed > 0)
			? sessionUsage.contextUsed
			: ($activeSession.contextUsed ?? 0)
	);
	const contextUsed = $derived(baseContextUsed + autoCompactionReserve);
	const contextPercent = $derived(Math.min((contextUsed / CONTEXT_MAX) * 100, 100));

	// Context color - always themed (no usage-based color changes)
	const contextColor = $derived('');
</script>

<div class="usage-indicator {contextColor}" title="{formatTokenCount(contextUsed)} / {formatTokenCount(CONTEXT_MAX)} tokens">
	<div class="usage-bar">
		<div class="usage-fill" style="width: {Math.min(contextPercent, 100)}%"></div>
	</div>
	<span class="usage-text">{Math.round(contextPercent)}%</span>
</div>

<style>
	/* ===== USAGE INDICATOR - Glass styling to match desktop ===== */
	.usage-indicator {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 4px 8px;
		background: var(--glass-bg);
		backdrop-filter: blur(var(--glass-blur, 12px));
		-webkit-backdrop-filter: blur(var(--glass-blur, 12px));
		border: 1px solid var(--glass-border);
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
	}

	.usage-bar {
		width: 40px;
		height: 4px;
		background: color-mix(in oklch, var(--muted) 50%, var(--background));
		border-radius: calc(var(--radius, 0.75rem) * 0.17);
		overflow: hidden;
	}

	.usage-fill {
		height: 100%;
		background: var(--primary);
		border-radius: calc(var(--radius, 0.75rem) * 0.17);
		transition: width 0.3s ease;
	}

	.usage-text {
		font-family: var(--font-mono);
		font-size: 11px;
		font-weight: 500;
		color: var(--muted-foreground);
		letter-spacing: 0.02em;
	}

	/* Responsive: mobile layout adjustments */
	@media (max-width: 639px) {
		/* Hide the usage bar on mobile, only show percentage */
		.usage-bar {
			display: none;
		}

		/* Make usage indicator more compact */
		.usage-indicator {
			padding: 4px 6px;
			gap: 0;
		}
	}
</style>
