<script lang="ts">
 import type { ModelProvider } from '$lib/api/model-providers';
 let { provider: _provider }: { provider: ModelProvider } = $props();
</script>
<section aria-label="OpenAI account usage" class="account-credits">
 <h4>API credit balance</h4>
 <p>OpenAI does not expose a prepaid credit balance through the model API key. No additional key is needed or requested here.</p>
 <p>An Admin API key reports organization spending, not credit left. Any previously configured usage access is preserved. Usage keeps that spending separate from balances.</p>
 <p>For weekly subscription allowance and available extra credits, use the ChatGPT sign-in provider. ChatGPT credits are separate from OpenAI API billing.</p>
 <a href="https://platform.openai.com/settings/organization/billing/overview" target="_blank" rel="noopener noreferrer">View balance in OpenAI Platform billing</a>
</section>
<style>
 .account-credits { display: grid; gap: .7rem; padding: 1rem 0; min-width: 0; }
 h4, p { margin: 0; } p, a { font-size: .85rem; overflow-wrap: anywhere; }
</style>
