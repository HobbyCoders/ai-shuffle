<script lang="ts">
	// Mounts the usage popover rows and the settings panels touched by #1607 and
	// #1613 with canned data. The API answers come from vite.usage-rows.config.js.
	import ContextUsageBar from '$lib/components/chat/ContextUsageBar.svelte';
	import LoopGuardPanel from '$lib/components/settings/LoopGuardPanel.svelte';
	import MediaToolsSection from '$lib/components/deck/cards/settings/providers/MediaToolsSection.svelte';
	import { rateLimit } from '$lib/stores/rate-limit';

	const view = new URLSearchParams(location.search).get('view') ?? 'usage';
	const now = Date.now();
	const inAnHour = String(Math.floor(now / 1000) + 3600);
	rateLimit.applySnapshot({
		available: true,
		windows: [],
		self_instance_id: 'self',
		snapshots: [{
			instance_id: 'self', available: true, fetched_at: (now - 42 * 60_000) / 1000,
			windows: [
				{ type: 'five_hour', utilization: 35, resets_at: inAnHour, observed_at: now - 42 * 60_000 },
				{ type: 'seven_day', utilization: 60, resets_at: inAnHour, observed_at: now - 42 * 60_000 }
			]
		}],
		providers: [
			{ provider_id: 'codex', provider_type: 'chatgpt', provider_name: 'ChatGPT', plan_type: 'plus',
				status: 'fresh', fetched_at: now, credits: { balance: 12.5 },
				windows: [{ type: 'five_hour', utilization: 30, resets_at: inAnHour }] },
			{ provider_id: 'openai-api', provider_type: 'openai', provider_name: 'OpenAI', status: 'unavailable',
				last_error_kind: 'unsupported' },
			{ provider_id: 'fal-ai', provider_type: 'fal', provider_name: 'fal.ai', status: 'fresh', fetched_at: now,
				credits: { balance_remaining: 18.11 } }
		]
	});
</script>

{#if view === 'settings'}
	<main class="settings">
		<div class="set-group"><LoopGuardPanel isAdmin /></div>
		<MediaToolsSection />
	</main>
{:else}
	<main class="chat-view">
		<div class="bar"><ContextUsageBar /></div>
	</main>
{/if}

<style>
	:global(body) { margin: 0; background: var(--background); color: var(--foreground); }
	.chat-view { position: relative; height: 100dvh; display: flex; align-items: flex-end; justify-content: flex-end; padding: 0 16px 24px; box-sizing: border-box; }
	.bar { display: inline-flex; }
	.settings { max-width: 720px; margin: 0 auto; padding: 16px; display: grid; gap: 16px; }
</style>
