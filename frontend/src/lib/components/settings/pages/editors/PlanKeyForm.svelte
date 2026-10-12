<script lang="ts">
	/**
	 * PlanKeyForm: the paid-plan key entry, opened under Cloud > Plan key. A
	 * secret, so Activate is explicit (Enter submits). The key only adds
	 * remote workers; local use never needs one.
	 */
	import SettingRow from '../../rows/SettingRow.svelte';

	interface Props {
		/** A paid lease is held here: the key replaces it. */
		replacing: boolean;
		busy: boolean;
		key: string;
		onActivate: () => void;
	}

	let { replacing, busy, key = $bindable(''), onActivate }: Props = $props();
</script>

<form
	class="st-xlist"
	aria-label="Plan key"
	onsubmit={(e) => {
		e.preventDefault();
		if (key.trim()) onActivate();
	}}
>
	<SettingRow label={replacing ? 'Use a different key' : 'Key'}>
		<input
			class="st-txt key st-mono"
			aria-label="Plan key"
			placeholder="ASH-XXXXX-XXXXX-XXXXX-XXXXX"
			autocomplete="off"
			spellcheck="false"
			maxlength="64"
			bind:value={key}
			disabled={busy}
		/>
		<button type="submit" class="st-btn primary" disabled={busy || !key.trim()}>{busy ? 'Working…' : 'Activate'}</button>
	</SettingRow>
</form>
<p class="st-note">The key is in the email from your paid plan. Keep it private.</p>

<style>
	.key {
		height: 32px;
		width: 250px;
		max-width: 100%;
		min-width: 0;
		border-radius: var(--st-radius-md);
		border: 1px solid var(--st-input);
		background: var(--st-muted);
		color: var(--st-fg);
		padding: 0 10px;
	}
	.key::placeholder {
		color: var(--st-subtle);
	}
	.key:focus {
		outline: none;
		border-color: var(--st-primary);
		box-shadow: 0 0 0 3px var(--st-primary-soft);
	}
	.key:disabled {
		opacity: 0.6;
	}
</style>
