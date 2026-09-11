<script lang="ts">
  import { onMount } from 'svelte';
  import { theme } from '$lib/stores/theme.svelte';
  import UserMessage from '$lib/components/chat/UserMessage.svelte';
  import AssistantMessage from '$lib/components/chat/AssistantMessage.svelte';
  import { samples, freshMessages, sendDemoMessage } from '../../demo/samples';
  let selected = $state(0);
  let messages = $state(freshMessages(0));
  let draft = $state('');
  let notice = $state('');
  onMount(() => theme.init());
  function select(index: number) {
    selected = index; messages = freshMessages(index); draft = ''; notice = '';
  }
  function send(event: SubmitEvent) {
    event.preventDefault();
    messages = sendDemoMessage(messages, draft);
    draft = ''; notice = 'Preset reply added. Nothing was sent to a worker.';
  }
</script>

<svelte:head><title>AI-Shuffle — Offline demo</title></svelte:head>
<main>
  <header>
    <h1>Offline demo</h1>
    <p>No account · No worker · No AI usage</p>
  </header>
  <aside aria-label="Demonstration limits">
    <strong>Sample data and preset replies only.</strong>
    This uses the app’s message display components. Live connections, AI voice, remote tools,
    and your account data are not available here. Nothing is saved; leaving resets this demo.
    Use Android Back to return to the app’s normal connection and settings screens.
  </aside>
  <nav aria-label="Sample conversations">
    {#each samples as sample, index}
      <button class:selected={selected === index} aria-pressed={selected === index}
        onclick={() => select(index)}>{sample.title}</button>
    {/each}
  </nav>
  <section aria-label="Sample messages">
    {#each messages as message, index}
      {#if message.role === 'user'}
        <UserMessage content={message.content} />
      {:else}
        <AssistantMessage content={message.content} messageIndex={index}
          messageId={`demo-${selected}-${index}`} profileName="Preset sample — not AI" />
      {/if}
    {/each}
  </section>
  <form onsubmit={send}>
    <label for="demo-message">Try a local message</label>
    <textarea id="demo-message" bind:value={draft} maxlength="2000" rows="2"
      placeholder="Do not enter private information."></textarea>
    <div class="actions">
      <button type="submit" disabled={!draft.trim() || messages.length >= 42}>Send locally</button>
      <button type="button" onclick={() => select(selected)}>Reset sample</button>
    </div>
    <p role="status">{notice || 'Replies are fixed sample text. No network request is made.'}</p>
  </form>
</main>

<style>
  main { max-width: 800px; margin: auto; padding: 16px; color: var(--foreground); background: var(--background); min-height: 100dvh; }
  h1 { font-size: 1.5rem; font-weight: 700; }
  header p, form p { font-size: .85rem; margin: 6px 0; }
  aside { border: 1px solid var(--border); border-radius: 12px; padding: 12px; margin: 16px 0; font-size: .875rem; line-height: 1.6; }
  aside strong { display: block; }
  nav, .actions { display: flex; flex-wrap: wrap; gap: 8px; }
  button { min-height: 44px; padding: 8px 12px; border: 1px solid var(--border); border-radius: 8px; background: var(--background); color: var(--foreground); cursor: pointer; }
  button.selected, button[type='submit'] { background: var(--foreground); color: var(--background); }
  button:disabled { opacity: .45; cursor: default; }
  section { display: flex; flex-direction: column; gap: 20px; padding: 24px 0; overflow-wrap: anywhere; }
  form { border-top: 1px solid var(--border); padding-top: 16px; }
  label { display: block; font-weight: 600; }
  textarea { box-sizing: border-box; width: 100%; resize: vertical; margin: 8px 0; padding: 12px; border: 1px solid var(--border); border-radius: 8px; background: var(--background); color: var(--foreground); font: inherit; }
  :is(button, textarea):focus-visible { outline: 2px solid #6366f1; outline-offset: 3px; }
</style>
