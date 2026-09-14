<script lang="ts">
	import { onMount } from 'svelte';
	import { api, getErrorMessage } from '$lib/api/client';

	type Policy = { allow_updates: boolean; update_all_workers: boolean };
	let policy = $state<Policy | null>(null);
	let saving = $state(false);
	let error = $state('');
	let alive = false;
	async function refresh() {
		try {
			const next = await api.get<Policy>('/settings/application-updates');
			if (alive && !saving) policy = next;
		} catch (cause) { if (alive) error = getErrorMessage(cause, 'Could not load update settings'); }
	}
	onMount(() => {
		alive = true; void refresh();
		const timer = setInterval(refresh, 5000);
		return () => { alive = false; clearInterval(timer); };
	});
	async function toggle(key: keyof Policy) {
		if (!policy || saving) return;
		saving = true; error = '';
		try {
			policy = await api.put<Policy>('/settings/application-updates', { [key]: !policy[key] });
			window.dispatchEvent(new Event('application-update-policy'));
		} catch (cause) { error = getErrorMessage(cause, 'Could not save update setting'); }
		finally { saving = false; }
	}
</script>

<section class="updates" aria-label="Application updates">
	<label class="policy">
		<span><strong>Allow updates</strong><span class="hint" id="update-policy-hint">Receive signed application updates on desktop and Docker without installing a new release. Enabled by default and shared across your account's workers.</span></span>
		<input type="checkbox" role="switch" aria-label="Allow updates" aria-describedby="update-policy-hint" checked={policy?.allow_updates ?? true} disabled={!policy || saving} onchange={(event) => { event.currentTarget.checked = policy?.allow_updates ?? true; void toggle('allow_updates'); }} />
	</label>
	<label class="policy scope">
		<span><strong>Update all workers</strong><span class="hint" id="update-scope-hint">Use the existing update button for all online workers. When off, it updates only the worker you are connected to. Off by default.</span></span>
		<input type="checkbox" role="switch" aria-label="Update all workers" aria-describedby="update-scope-hint" checked={policy?.update_all_workers ?? false} disabled={!policy || saving || !policy.allow_updates} onchange={(event) => { event.currentTarget.checked = policy?.update_all_workers ?? false; void toggle('update_all_workers'); }} />
	</label>
	<p class="hint">Download and restart using the update button in the sidebar. Offline, busy, or unsupported workers are reported, not queued for a later restart. Runtime and system security changes still require a new release; native Android changes still need a Play update.</p>
	{#if policy && !policy.allow_updates}<p role="status">Application updates are paused. Your current version will keep running.</p>{/if}
	{#if error}<p class="error" role="alert">{error}</p>{/if}
</section>

<style>
	.updates { padding: 14px; }
	.policy { display: flex; align-items: center; justify-content: space-between; gap: 20px; cursor: pointer; }
	.scope { margin-top: 14px; }
	.policy strong { display: block; font-size: 13px; }
	.hint { display: block; color: var(--muted-foreground); font-size: 12px; line-height: 1.5; margin: 6px 0; }
	input { accent-color: var(--primary); width: 32px; height: 20px; flex-shrink: 0; }
	.error { color: var(--destructive); font-size: 12px; }
</style>
