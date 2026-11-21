<script lang="ts">
	/**
	 * VaultSignInPicker — which saved sign-in a request uses, or a new one.
	 *
	 * A short list to pick from, not a dropdown: the name and the address of
	 * every choice are readable before choosing. It holds no secret; the value
	 * is the saved sign-in's id, or '' for a sign-in typed into the request.
	 */
	import './vault.css';
	type Option = { id: string; label: string; target: string; fingerprint?: string };
	let { options, value = $bindable(''), legend = 'Sign in with', pinLabel = 'Host key', disabled = false }: {
		options: Option[];
		value?: string;
		legend?: string;
		/** What a pinned fingerprint is: an SSH host key, or a service's certificate. */
		pinLabel?: string;
		disabled?: boolean;
	} = $props();
	const uid = $props.id();
</script>

<div class="pa-pick" role="radiogroup" aria-labelledby="{uid}-legend">
	<span class="pa-pick-legend" id="{uid}-legend">{legend}</span>
	{#each options as option (option.id)}
		<label class="pa-pick-row" class:selected={value === option.id}>
			<input type="radio" name={uid} value={option.id} {disabled} bind:group={value} />
			<span class="pa-pick-main">
				<strong>{option.label}</strong>
				<code>{option.target}</code>
				{#if value === option.id && option.fingerprint}
					<small>{pinLabel} <code>{option.fingerprint}</code></small>
				{/if}
			</span>
		</label>
	{/each}
	<label class="pa-pick-row" class:selected={!value}>
		<input type="radio" name={uid} value="" {disabled} bind:group={value} />
		<span class="pa-pick-main"><strong>Another sign-in…</strong></span>
	</label>
</div>
