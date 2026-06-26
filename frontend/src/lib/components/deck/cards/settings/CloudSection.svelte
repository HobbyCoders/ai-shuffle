<script lang="ts">
	/**
	 * CloudSection — configure a second AI Shuffle install to offload work to.
	 *
	 * When a session uses "cloud" execution mode, this install forwards the work
	 * to the second install configured here (URL + API key). Self-contained: it
	 * loads and saves its own state via /settings/cloud.
	 */
	import { onMount } from 'svelte';
	import { Cloud } from 'lucide-svelte';
	import { api, getErrorMessage } from '$lib/api/client';

	let url = $state('');
	let apiKey = $state('');
	let apiKeySet = $state(false);
	let apiKeyMasked = $state('');
	let configured = $state(false);
	let loading = $state(false);
	let saving = $state(false);
	let error = $state('');
	let success = $state('');

	onMount(loadConfig);

	async function loadConfig() {
		loading = true;
		try {
			const cfg = await api.get<{
				url: string;
				api_key_set: boolean;
				api_key_masked: string;
				configured: boolean;
			}>('/settings/cloud');
			url = cfg.url || '';
			apiKeySet = cfg.api_key_set;
			apiKeyMasked = cfg.api_key_masked || '';
			configured = cfg.configured;
		} catch (e) {
			console.error('Failed to load cloud config:', e);
		} finally {
			loading = false;
		}
	}

	async function save() {
		const trimmed = url.trim();
		if (trimmed && !/^https?:\/\//.test(trimmed)) {
			error = 'URL must start with http:// or https://';
			return;
		}
		saving = true;
		error = '';
		success = '';
		try {
			// Only send api_key when the user typed a new one, so we don't clobber
			// the stored key with an empty value.
			const body: { url: string; api_key?: string } = { url: trimmed };
			if (apiKey.trim()) body.api_key = apiKey.trim();
			const cfg = await api.post<{
				url: string;
				api_key_set: boolean;
				api_key_masked: string;
				configured: boolean;
			}>('/settings/cloud', body);
			url = cfg.url;
			apiKeySet = cfg.api_key_set;
			apiKeyMasked = cfg.api_key_masked;
			configured = cfg.configured;
			apiKey = '';
			success = 'Cloud offload settings saved';
			setTimeout(() => (success = ''), 3000);
		} catch (e: unknown) {
			error = getErrorMessage(e, 'Failed to save cloud settings');
		} finally {
			saving = false;
		}
	}

	async function clearConfig() {
		saving = true;
		error = '';
		success = '';
		try {
			await api.delete('/settings/cloud');
			url = '';
			apiKey = '';
			apiKeySet = false;
			apiKeyMasked = '';
			configured = false;
			success = 'Cloud offload settings cleared';
			setTimeout(() => (success = ''), 3000);
		} catch (e: unknown) {
			error = getErrorMessage(e, 'Failed to clear cloud settings');
		} finally {
			saving = false;
		}
	}
</script>

<div class="set-group">
	<div class="set-grouphead">
		<h3><Cloud />Legacy cloud offload
			{#if configured}<span class="b b-on">Configured</span>{:else}<span class="b b-muted">Not configured</span>{/if}
		</h3>
		<p>Fallback for offloading to a single install by URL + API key. On a shared database, prefer the auto-registered <strong>Devices</strong> above and the in-chat device picker; this manual config still works as a fallback when no device is picked. GitHub-backed projects only.</p>
	</div>

	{#if loading}
		<div class="sg-body"><p class="muted">Loading…</p></div>
	{:else}
		<div class="set-row">
			<div class="lab"><h4>Second install URL</h4><p>Base URL of the install that runs the work.</p></div>
			<div class="ctl">
				<input type="text" class="inp mono cloud-in" bind:value={url} placeholder="https://second-install.example.com" autocomplete="off" />
			</div>
		</div>
		<div class="set-row">
			<div class="lab"><h4>API key</h4><p>Create one on the second install (System → API users).</p></div>
			<div class="ctl">
				<input type="password" class="inp cloud-in" bind:value={apiKey} placeholder={apiKeySet ? `Saved (${apiKeyMasked}) — leave blank to keep` : 'aih_…'} autocomplete="off" />
			</div>
		</div>
		<div class="set-row">
			<div class="lab"><h4>Apply changes</h4><p>Save the configuration, or clear it to disable cloud offload.</p></div>
			<div class="ctl">
				<button type="button" class="ed-btn" onclick={clearConfig} disabled={saving || (!url && !apiKeySet)}>Clear</button>
				<button type="button" class="ed-btn primary" onclick={save} disabled={saving}>{saving ? 'Saving…' : 'Save'}</button>
			</div>
		</div>
		{#if error}<div class="kc-foot err">{error}</div>{/if}
		{#if success}<div class="kc-foot ok">{success}</div>{/if}
	{/if}
</div>

<style>
	.sg-body {
		padding: 14px 18px 16px;
	}
	.muted {
		margin: 0;
		font-size: 13px;
		color: var(--muted-foreground);
	}
	.cloud-in {
		width: 320px;
		max-width: 100%;
	}
	.b {
		font-family: var(--font-mono, monospace);
		font-size: 10px;
		font-weight: 600;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		padding: 2px 7px;
		border-radius: 999px;
	}
	.b-on {
		color: var(--primary-foreground);
		background: var(--primary);
	}
	.b-muted {
		color: var(--muted-foreground);
		background: var(--muted);
	}
	.kc-foot {
		padding: 8px 18px 12px;
		font-size: 12.5px;
	}
	.kc-foot.ok {
		color: var(--success, #16a34a);
	}
	.kc-foot.err {
		color: var(--destructive);
	}
</style>
