<script lang="ts">
	/**
	 * UpdateCheckResults: per-worker results of "Check for updates", opened
	 * under System > Updates. Installing stays with the sidebar's update
	 * button.
	 */
	import type { UpdateResultRow } from '../updates-state.svelte';
	import StatusDot from '../../rows/StatusDot.svelte';
	import SettingRow from '../../rows/SettingRow.svelte';

	interface Props {
		rows: UpdateResultRow[];
		/** The check is still running. */
		running: boolean;
		error?: string;
	}

	let { rows, running, error = '' }: Props = $props();
	// One copy of each hint, however many workers share it.
	const hints = $derived([...new Set(rows.flatMap((row) => (row.hint ? [row.hint] : [])))]);
</script>

{#if rows.length === 0 && running}
	<div class="st-wait" role="status"><span class="st-spin"></span>Checking for updates</div>
{:else if rows.length > 0}
	<div class="st-xlist st-update-results" aria-busy={running}>
		{#each rows as row (row.id)}
			<SettingRow label={row.name}>
				<span class="st-status">
					{#if row.tone === 'busy'}<span class="st-spin"></span>{:else}<StatusDot tone={row.tone} />{/if}{row.text}
				</span>
			</SettingRow>
		{/each}
	</div>
	{#each hints as hint (hint)}<p class="st-msg">{hint}</p>{/each}
{/if}
{#if error}<p class="st-msg err" role="alert">{error}</p>{/if}

<style>
	/* These are explanations, not compact value chips. Keep the complete
	 * reason readable, including workflow names and long revision IDs. */
	.st-update-results :global(.st-row) {
		grid-template-columns: minmax(0, 1fr);
		gap: 4px;
	}
	.st-update-results :global(.st-rc) {
		justify-content: flex-start;
	}
	.st-update-results :global(.st-status) {
		white-space: normal;
		overflow-wrap: anywhere;
		align-items: baseline;
		line-height: 1.6;
	}
</style>
