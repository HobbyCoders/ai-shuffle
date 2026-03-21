<script lang="ts">
	/**
	 * ContextUsageBar - Shows context window usage as a progress bar with percentage.
	 * Resolves the effective model (override → profile → default) to determine whether
	 * the context window is 200k (standard) or 1M (extended).
	 *
	 * IMPORTANT: Context usage comes from $activeSession.contextUsed which is set by the
	 * onPollApplied callback (session-poller → active-session). Do NOT import from
	 * session-state.svelte.ts directly — that .svelte.ts file suffers from dual module
	 * evaluation (see KNOWN_ISSUES Root Cause 10), causing reads to hit a different
	 * instance than the one the poller writes to.
	 */
	import { activeSession } from '$lib/stores/active-session';
	import { profiles } from '$lib/stores/app-data';
	import { AUTO_COMPACTION_BASE, getContextMaxForModel } from '$lib/constants/chat';

	function formatTokenCount(count: number): string {
		if (count >= 1000000) {
			return `${(count / 1000000).toFixed(1)}M`;
		}
		if (count >= 1000) {
			return `${(count / 1000).toFixed(1)}k`;
		}
		return count.toString();
	}

	// Resolve effective model: override → profile config → default
	const effectiveModel = $derived.by(() => {
		if ($activeSession.modelOverride) return $activeSession.modelOverride;
		const profile = $profiles.find(p => p.id === $activeSession.profile);
		return profile?.config?.model || 'sonnet';
	});

	const contextMax = $derived(getContextMaxForModel(effectiveModel));

	// Context usage from activeSession — set by onPollApplied callback after each
	// successful poll. This is the sole reliable path: poller → activeSession._update().
	// Uses per-turn context window tokens (input + cache_creation + cache_read),
	// NOT cumulative totalTokensIn which would massively overcount.
	//
	// AUTO_COMPACTION_BASE is a floor (not additive) because the SDK's
	// input_tokens + cache_creation + cache_read already includes everything
	// in the context window (system prompt, message history, tool results).
	// Adding it on top would double-count the ~33k system prompt.
	const baseContextUsed = $derived($activeSession.contextUsed ?? 0);
	const contextUsed = $derived(Math.max(baseContextUsed, AUTO_COMPACTION_BASE));
	const contextPercent = $derived(Math.min((contextUsed / contextMax) * 100, 100));

	// Context color - always themed (no usage-based color changes)
	const contextColor = $derived('');
</script>

<div class="usage-indicator {contextColor}" title="{formatTokenCount(contextUsed)} / {formatTokenCount(contextMax)} tokens">
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
