<script lang="ts">
	/**
	 * VaultDuration — how long an approved access lasts. Three choices that
	 * cover a quick look, a working session and a long job; the access ends
	 * earlier when it is revoked or the worker restarts.
	 */
	import './vault.css';
	let { minutes = $bindable(60), disabled = false }: { minutes?: number; disabled?: boolean } = $props();
	const uid = $props.id();
	const choices = [[15, '15 minutes'], [60, '1 hour'], [480, '8 hours']] as const;
</script>

<div class="pa-seg" role="radiogroup" aria-labelledby="{uid}-legend">
	<span class="pa-seg-legend" id="{uid}-legend">Access for</span>
	<div class="pa-seg-row">
		{#each choices as [value, text] (value)}
			<label class:selected={minutes === value}>
				<input type="radio" name={uid} {value} {disabled} bind:group={minutes} />
				<span>{text}</span>
			</label>
		{/each}
	</div>
</div>
