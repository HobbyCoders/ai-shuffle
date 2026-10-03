<script lang="ts">
	/**
	 * ProfileSystemPrompt: the agent's base prompt (ruling A12). The fluid
	 * compiler wraps it with environment, instruction files, skills and security
	 * context on every leg, so there is no preset/append mode and no separate
	 * env-injection toggle. Empty is allowed.
	 */
	interface Props {
		systemPromptContent: string;
		onSystemPromptContentChange: (value: string) => void;
	}

	let { systemPromptContent, onSystemPromptContentChange }: Props = $props();

	const charCount = $derived(systemPromptContent.length);
</script>

<div class="set-group">
	<div class="sp-head">
		<span class="hint">Base prompt. Runtime context is added automatically.</span>
		<span class="sp-count" class:over={charCount > 10000}>{charCount.toLocaleString()} / 10,000</span>
	</div>
	<textarea
		value={systemPromptContent}
		oninput={(e) => onSystemPromptContentChange((e.target as HTMLTextAreaElement).value)}
		class="fld code prompt-area"
		aria-label="System prompt"
		placeholder="Describe who this agent is and how it should work…"
	></textarea>
</div>

<style>
	.sp-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 12px;
		margin-bottom: 8px;
	}
	.sp-count {
		font-family: var(--font-mono, monospace);
		font-size: 11px;
		color: var(--muted-foreground);
		flex-shrink: 0;
	}
	.sp-count.over {
		color: var(--destructive);
	}
	textarea.prompt-area {
		min-height: 240px;
	}
</style>
