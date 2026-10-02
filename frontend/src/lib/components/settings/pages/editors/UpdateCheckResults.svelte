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
</script>

{#if rows.length === 0 && running}
	<div class="st-wait" role="status"><span class="st-spin"></span>Checking for updates</div>
{:else if rows.length > 0}
	<div class="st-xlist" aria-busy={running}>
		{#each rows as row (row.id)}
			<SettingRow label={row.name}>
				<span class="st-status">
					{#if row.tone === 'busy'}<span class="st-spin"></span>{:else}<StatusDot tone={row.tone} />{/if}{row.text}
				</span>
			</SettingRow>
		{/each}
	</div>
{/if}
{#if error}<p class="st-msg err" role="alert">{error}</p>{/if}
