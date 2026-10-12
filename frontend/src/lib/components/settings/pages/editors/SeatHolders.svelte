<script lang="ts">
	/**
	 * SeatHolders: the workers holding a paid plan's seats, opened under
	 * Cloud > Remote workers when activation found every seat taken. Freeing
	 * a seat uses the key that was just tried, then you activate again.
	 */
	import type { SeatHolder } from '$lib/api/license';
	import SettingRow from '../../rows/SettingRow.svelte';
	import InlineConfirm from '../../rows/InlineConfirm.svelte';

	interface Props {
		holders: SeatHolder[];
		busy: boolean;
		onFree: (holder: SeatHolder) => Promise<void> | void;
	}

	let { holders, busy, onFree }: Props = $props();

	function seen(iso: string): string {
		const date = new Date(iso);
		if (Number.isNaN(date.getTime())) return iso;
		return `Seen ${date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}`;
	}
</script>

<div class="st-xlist" aria-label="Workers using this plan" role="group">
	{#each holders as holder (holder.install_id)}
		<SettingRow
			label={holder.name || holder.install_id}
			hint={[seen(holder.last_seen), holder.app_version].filter(Boolean).join(', ')}
		>
			<InlineConfirm
				label="Free seat"
				question="Free this seat?"
				confirmLabel="Free seat"
				size="sm"
				disabled={busy}
				onConfirm={() => onFree(holder)}
			/>
		</SettingRow>
	{/each}
</div>
<p class="st-note">Free a seat you no longer use, then activate again. That worker keeps running locally; it only loses paid remote access.</p>
