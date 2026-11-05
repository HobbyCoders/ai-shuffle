<script lang="ts">
 /** Keep the server's reason and recovery, substituting only internal identities. */
 let { error, providerId, modelId }: { error: string; providerId?: string | null; modelId?: string | null } = $props();
 const message = $derived.by(() => {
  let text = error.replace(/prov:[A-Za-z0-9:_+/-]+={0,2}/g, 'the saved model');
  for (const [id, label] of [[providerId, 'the saved provider'], [modelId, 'the saved model']]) {
   if (id) text = text.split(id).join(label!);
  }
  return text;
 });
</script>

<span title={error}>{message}</span>
