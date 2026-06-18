<script lang="ts">
	/**
	 * CloudSection — configure a second AI Shuffle install to offload work to.
	 *
	 * When a session uses "cloud" execution mode, this install forwards the work
	 * to the second install configured here (URL + API key). Self-contained: it
	 * loads and saves its own state via /settings/cloud.
	 */
	import { onMount } from 'svelte';
	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import { api, getErrorMessage } from '$lib/api/client';
	import './settings-shared.css';

	interface Props {
		expanded: boolean;
		onToggle: () => void;
	}

	let { expanded, onToggle }: Props = $props();

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

<section class="section">
	<button class="section-header" onclick={onToggle}>
		<h3>Cloud Offload</h3>
		<svg class="chevron" class:expanded viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
			<polyline points="6 9 12 15 18 9" />
		</svg>
	</button>
	{#if expanded}
		<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
			<p class="setting-desc mb-3">
				Configure a second AI Shuffle install to offload work to. When you start a
				session in <strong>Cloud</strong> mode, this install hands the prompt to the
				second install over its API: it clones the GitHub repo into its own
				<code>/workspace</code>, runs the work in a fresh git worktree, and streams the
				result back here so you can chat, interrupt and interact as usual. Cloud mode
				only works with GitHub-backed projects.
			</p>

			{#if loading}
				<div class="loading-text">Loading...</div>
			{:else}
				<div class="field">
					<label for="cloud-url">Second install URL</label>
					<input
						id="cloud-url"
						type="text"
						class="input"
						bind:value={url}
						placeholder="https://second-install.example.com"
						autocomplete="off"
					/>
				</div>

				<div class="field">
					<label for="cloud-key">API key for the second install</label>
					<input
						id="cloud-key"
						type="password"
						class="input"
						bind:value={apiKey}
						placeholder={apiKeySet ? `Saved (${apiKeyMasked}) — leave blank to keep` : 'aih_…'}
						autocomplete="off"
					/>
					<span class="setting-desc">
						Create an API key on the second install (System → API Users). It lets this
						install authenticate when offloading work.
					</span>
				</div>

				<div class="status-row">
					{#if configured}
						<span class="badge primary">Configured</span>
					{:else}
						<span class="badge muted">Not configured</span>
					{/if}
				</div>

				<div class="actions">
					<button class="btn secondary compact" onclick={clearConfig} disabled={saving || (!url && !apiKeySet)}>
						Clear
					</button>
					<button class="btn primary compact" onclick={save} disabled={saving}>
						{saving ? 'Saving...' : 'Save'}
					</button>
				</div>

				{#if error}<p class="status error">{error}</p>{/if}
				{#if success}<p class="status success">{success}</p>{/if}
			{/if}
		</div>
	{/if}
</section>

<style>
	.field {
		display: flex;
		flex-direction: column;
		gap: 4px;
		margin-bottom: 12px;
	}

	.field label {
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--foreground);
	}

	.status-row {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-bottom: 12px;
	}

	.actions {
		display: flex;
		justify-content: flex-end;
		gap: 8px;
	}

	code {
		font-size: 0.75rem;
		padding: 1px 4px;
		border-radius: 4px;
		background: var(--muted);
	}
</style>
