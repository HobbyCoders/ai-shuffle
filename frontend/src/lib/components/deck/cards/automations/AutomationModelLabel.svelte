<script lang="ts">
 import { decodeProviderModel, encodeProviderModel, getModelLabel, getModels, getProviders } from '$lib/constants/models';
 import type { Automation } from '$lib/api/automations';
 let { automation }: { automation: Pick<Automation, 'model' | 'effective_model' | 'effective_provider_id' | 'model_source' | 'model_error'> } = $props();
 const raw = $derived(automation.model || automation.effective_model || '');
 const name = $derived.by(() => {
  if (automation.model_error) return 'Model unavailable';
  let value = raw;
  let provider = automation.effective_provider_id;
  // Some saved agent records already carry a composite model value.
  // Decode before applying the separately reported provider, never encode twice.
  for (let depth = 0; depth < 4; depth++) {
   const decoded = decodeProviderModel(value);
   if (!decoded) break;
   provider = decoded.providerId;
   value = decoded.modelId;
  }
  if (!value) return automation.model_source === 'default' ? 'Worker default' : 'Saved model';
  const match = provider
   ? getProviders().find(p => p.id === provider)?.models.find(m => m.id === value)?.label
   : getModels().find(m => m.value === value)?.label || getProviders().flatMap(p => p.models).find(m => m.id === value)?.label;
  if (match) return match;
  const label = getModelLabel(provider ? encodeProviderModel(provider, value) : value);
  return label !== value && !label.startsWith('prov:') ? label : 'Saved model';
 });
</script>
<span data-testid="automation-model-badge" title={automation.model_error || raw || 'Model its runs use'}>{name}{#if !automation.model && automation.model_source === 'agent'}{' '}<span class="source">· Saved on agent</span>{:else if !automation.model && automation.model_source === 'default' && raw}{' '}<span class="source">· Worker default</span>{/if}</span>
<style>
 .source { color: var(--st-mfg); }
</style>
