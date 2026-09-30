<script lang="ts">
 import { onMount } from 'svelte';
 import type { ModelProvider } from '$lib/api/model-providers';
 import { getOpenRouterUsage, type OpenRouterUsageStatus } from '$lib/api/openrouter-billing';
 let { provider }: { provider: ModelProvider } = $props();
 let status = $state<OpenRouterUsageStatus | null>(null);
 let disposed = false;
 onMount(() => { void Promise.resolve().then(() => getOpenRouterUsage(provider.id)).then(value => { if (!disposed) status = value; }).catch(() => {}); return () => { disposed = true; }; });
</script>
<section aria-label="OpenRouter account credits" class="account-credits">
 <h4>Account credits</h4>
 <p>Usage reads your remaining account credits with the API key already saved for this provider. No separate key is needed.</p>
 {#if status?.credits}<p>${status.credits.balance_remaining.toFixed(2)} left · last reported by OpenRouter</p>{/if}
 {#if status?.configured}<p>An existing legacy management key is still configured; it has not been changed.</p>{/if}
 <p>Refreshes on demand, at most once every five minutes. If this key cannot read account credits, Usage shows its remaining key limit or an unavailable balance—not spending as credit left.</p>
 <a href="https://openrouter.ai/settings/credits" target="_blank" rel="noopener noreferrer">View credits in OpenRouter</a>
</section>
<style>
 .account-credits { display: grid; gap: .7rem; padding: 1rem 0; min-width: 0; }
 h4, p { margin: 0; } p, a { font-size: .85rem; overflow-wrap: anywhere; }
</style>
