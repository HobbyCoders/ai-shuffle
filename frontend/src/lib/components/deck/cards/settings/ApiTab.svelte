<script lang="ts">
	/**
	 * ApiTab — programmatic-access settings.
	 *
	 * Manage API users and keys for programmatic access.
	 */
	import { Users } from 'lucide-svelte';
	import ApiUserManager from '$lib/components/ApiUserManager.svelte';
</script>

<!-- API users -->
<div class="set-group">
	<div class="set-grouphead">
		<h3><Users />API users</h3>
	</div>
	<div class="sg-body"><ApiUserManager /></div>
</div>

<style>
	.sg-body {
		padding: 10px 14px;
	}
</style>
