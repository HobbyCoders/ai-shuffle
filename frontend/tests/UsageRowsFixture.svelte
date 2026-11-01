<script lang="ts">
	// Mounts the usage popover rows and the Settings page touched by #1607 and
	// #1613 with canned data. The API answers come from vite.usage-rows.config.js.
	// The settings view opens Settings > Providers > fal.ai, as a deep link does.
	import ContextUsageBar from '$lib/components/chat/ContextUsageBar.svelte';
	import SettingsCard from '$lib/components/deck/cards/SettingsCard.svelte';
	import { rateLimit } from '$lib/stores/rate-limit';
	import { hydrateModelRegistry } from '$lib/constants/models';

	const view = new URLSearchParams(location.search).get('view') ?? 'usage';
	const now = Date.now();
	const inAnHour = String(Math.floor(now / 1000) + 3600);
	// view=claude-limits: the three Claude limits, the weekly Fable one past its
	// reset and last reported six days ago (an Opus-only week never reports it).
	// Plan views (controlled states for plans without a 5-hour window). The
	// Claude plan comes from the stubbed /api/v1/models (vite.usage-rows.config.js):
	//   plan-ultra:      Claude Ultra, a 5-hour window past its reset (reported
	//                    before the plan changed) and a live weekly one; ChatGPT
	//                    Pro and Plus rows with a weekly window only.
	//   plan-max:        the same windows on Claude Max.
	//   plan-ultra-live: Claude Ultra with a LIVE 5-hour window (an unverified
	//                    Ultra account keeps a temporary one).
	const planView = view.startsWith('plan-');
	const twoDaysAgo = String(Math.floor(now / 1000) - 2 * 86400);
	const inFiveDays = String(Math.floor(now / 1000) + 5 * 86400);
	if (planView) {
		void hydrateModelRegistry();
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			self_instance_id: 'self',
			snapshots: [{
				instance_id: 'self', available: true, fetched_at: now / 1000,
				windows: [
					view === 'plan-ultra-live'
						? { type: 'five_hour', utilization: 72, resets_at: inAnHour, observed_at: now }
						: { type: 'five_hour', utilization: 64, resets_at: twoDaysAgo, observed_at: now - 2 * 86_400_000 - 3_600_000 },
					{ type: 'seven_day', utilization: 58, resets_at: inFiveDays, observed_at: now }
				]
			}],
			providers: [
				{ provider_id: 'codex-pro', provider_type: 'chatgpt', provider_name: 'ChatGPT', plan_type: 'pro',
					status: 'fresh', fetched_at: now,
					windows: [{ type: 'seven_day', utilization: 12, resets_at: inFiveDays }] },
				{ provider_id: 'codex-plus', provider_type: 'chatgpt', provider_name: 'ChatGPT Plus', plan_type: 'plus',
					status: 'fresh', fetched_at: now,
					windows: [{ type: 'seven_day', utilization: 40, resets_at: inFiveDays }] }
			]
		});
	} else if (view === 'claude-limits') rateLimit.applySnapshot({
		available: true,
		windows: [],
		self_instance_id: 'self',
		snapshots: [{
			instance_id: 'self', available: true, fetched_at: now / 1000,
			windows: [
				{ type: 'five_hour', utilization: 88, resets_at: inAnHour, observed_at: now },
				{ type: 'seven_day', utilization: 41, resets_at: String(Math.floor(now / 1000) + 5 * 86400), observed_at: now },
				{ type: 'seven_day_overage_included', utilization: 19,
					resets_at: String(Math.floor(now / 1000) - 86400), observed_at: now - 6 * 86_400_000 }
			]
		}],
		providers: []
	});
	else rateLimit.applySnapshot({
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
				credits: { balance_remaining: 18.11 } },
			{ provider_id: 'router', provider_type: 'openrouter', provider_name: 'Router', status: 'stale',
				fetched_at: now - 25 * 60_000, cooldown_until: now + 30 * 60_000, credits: { usage: 2, limit: 10 } }
		]
	});
</script>

{#if view === 'settings'}
	<main class="settings">
		<SettingsCard initialItemId="category=providers&item=fal%3Akey" />
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
	.settings { height: 100dvh; }
</style>
