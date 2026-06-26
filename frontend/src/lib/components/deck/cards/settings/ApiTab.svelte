<script lang="ts">
	/**
	 * ApiTab — programmatic-access settings.
	 *
	 * Groups the two API-facing admin panels that previously lived in the System
	 * tab: rate limiting (how often sessions may call the model) and API users
	 * (keys for programmatic access).
	 */
	import { Gauge, Users } from 'lucide-svelte';
	import RateLimitManager from '$lib/components/RateLimitManager.svelte';
	import ApiUserManager from '$lib/components/ApiUserManager.svelte';
</script>

<!-- Rate limits -->
<div class="set-group">
	<div class="set-grouphead">
		<h3><Gauge />Rate limits</h3>
		<p>Throttle how often sessions can call the model.</p>
	</div>
	<div class="sg-body"><RateLimitManager /></div>
</div>

<!-- API users -->
<div class="set-group">
	<div class="set-grouphead">
		<h3><Users />API users</h3>
		<p>Create and revoke API keys for programmatic access.</p>
	</div>
	<div class="sg-body"><ApiUserManager /></div>
</div>

<style>
	.sg-body {
		padding: 14px 18px 16px;
	}
</style>
