<script lang="ts" module>
	/** The one tooltip every estimate marker shows. Exported for tests. */
	export const USAGE_ESTIMATED_TITLE = 'Token counts estimated: the provider did not report usage';
</script>

<script lang="ts">
	/**
	 * Marks a usage figure that includes harness-estimated token counts: the
	 * provider reported no usage for a call, or the reply was interrupted
	 * before its usage arrived, so the harness counted the text itself. The
	 * amount still counts toward budgets; only its precision is in question.
	 *
	 * `label` is "≈" (default, inline next to a cost) or "est." (tile labels).
	 */
	let { label = '≈' }: { label?: '≈' | 'est.' } = $props();
</script>

<span class="usage-est" title={USAGE_ESTIMATED_TITLE} aria-label={USAGE_ESTIMATED_TITLE} data-testid="usage-estimated">{label}</span>

<style>
	.usage-est {
		display: inline-flex;
		align-items: center;
		padding: 0 5px;
		border-radius: 4px;
		border: 1px solid color-mix(in srgb, var(--muted-foreground) 45%, transparent);
		color: var(--muted-foreground);
		font-size: 0.85em;
		font-weight: 600;
		line-height: 1.5;
		letter-spacing: 0;
		text-transform: none;
		cursor: help;
		user-select: none;
	}
</style>
