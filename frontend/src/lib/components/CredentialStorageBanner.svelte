<script lang="ts">
	import { auth } from '$lib/stores/auth.svelte';
	import Banner from './Banner.svelte';
	const storage = $derived($auth.status?.credential_storage);
	const message = $derived(storage?.state === 'legacy_unlock_required'
		? 'Stop Shuffle and adopt the saved credentials with the original encryption password, then restart.'
		: storage?.state === 'recovery_required'
			? 'Restore the matching credential key from a backup or the Shuffle install that initialized this database, then restart Shuffle.'
			: 'Check credential storage on this machine with python -m scripts.credential_keys status.');
</script>

{#if $auth.status?.is_admin && storage?.ready === false}
	<Banner title="Saved credentials are locked on this install" {message} severity="error" />
{/if}
