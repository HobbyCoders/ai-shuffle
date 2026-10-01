<script lang="ts">
	import { onMount } from 'svelte';
	import { api, getErrorMessage } from '$lib/api/client';

	type Policy = { allow_updates: boolean; update_all_workers: boolean };
	type Worker = { device_id: string; phase: string; version?: string | null; running_version?: string | null; message?: string | null; error?: string | null; reason?: string | null; request_pending?: boolean };
	type Receipt = { results: { device_id?: string; device_name: string; accepted: boolean | null; reason: string }[] };
	// A desktop takes a request on its next 30 s report and a container within
	// about a second; the check itself is one signed manifest fetch.
	const CHECK_POLL_MS = 2000;
	const CHECK_WAIT_MS = 90_000;
	let policy = $state<Policy | null>(null);
	let saving = $state(false);
	let checking = $state(false);
	let outcome = $state<{ name: string; text: string }[]>([]);
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
	function describe(w: Worker | undefined): string {
		if (!w) return 'Status unavailable';
		if (w.request_pending || w.phase === 'checking') return 'Still checking';
		if (w.version && w.running_version && (w.running_version.startsWith(w.version) || w.version.startsWith(w.running_version))) return 'Up to date';
		switch (w.phase) {
			case 'available': return 'Update available';
			case 'downloading': return 'Downloading update';
			case 'ready': return 'Update downloaded; ready to install';
			case 'error': return w.error || 'Check failed';
			case 'image-required': return w.message || 'Needs a new Docker image';
			case 'unknown': case 'unsupported': return w.reason || 'Status unavailable';
			default: return 'Up to date';
		}
	}
	// With Update all workers on, the server sends the check to every online worker.
	async function checkForUpdates() {
		if (!policy?.allow_updates || checking) return;
		checking = true; error = ''; outcome = [];
		try {
			const receipt = await api.post<Receipt>('/settings/application-updates', { action: 'check' });
			window.dispatchEvent(new Event('application-update-policy'));
			const asked = new Set(receipt.results.filter(r => r.accepted).map(r => r.device_id));
			let workers: Worker[] = [];
			// Two quiet readings in a row: a worker takes the request a moment
			// before it reports "checking".
			for (let waited = 0, quiet = 0; alive && asked.size && quiet < 2 && waited < CHECK_WAIT_MS; waited += CHECK_POLL_MS) {
				await new Promise(resolve => setTimeout(resolve, CHECK_POLL_MS));
				workers = (await api.get<{ workers: Worker[] }>('/settings/application-updates/workers')).workers;
				quiet = workers.some(w => asked.has(w.device_id) && (w.request_pending || w.phase === 'checking')) ? 0 : quiet + 1;
			}
			if (!alive) return;
			outcome = receipt.results.map(r => ({ name: r.device_name, text: r.accepted ? describe(workers.find(w => w.device_id === r.device_id)) : r.reason }));
			window.dispatchEvent(new Event('application-update-policy'));
		} catch (cause) { if (alive) error = getErrorMessage(cause, 'Could not check for updates'); }
		finally { checking = false; }
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
	<div class="check">
		<button type="button" class="ed-btn" aria-busy={checking} disabled={!policy?.allow_updates || saving || checking} onclick={() => void checkForUpdates()}>{checking ? 'Checking…' : 'Check for updates'}</button>
		<span class="hint">{policy?.update_all_workers ? 'Checks every online worker.' : 'Checks the worker you are connected to.'} Docker workers otherwise check once a day.</span>
	</div>
	<div role="status">
		{#if checking}<p class="hint">Checking for updates…</p>
		{:else if outcome.length}
			<ul class="outcome" aria-label="Update check results">
				{#each outcome as row, index (index)}<li><span>{row.name}</span><span>{row.text}</span></li>{/each}
			</ul>
		{/if}
	</div>
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
	.check { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-top: 10px; }
	.check .hint { margin: 0; }
	.outcome { list-style: none; margin: 8px 0 0; padding: 0; font-size: 12px; }
	.outcome li { display: flex; justify-content: space-between; gap: 16px; padding: 3px 0; }
	.outcome li span:last-child { color: var(--muted-foreground); text-align: right; }
</style>
