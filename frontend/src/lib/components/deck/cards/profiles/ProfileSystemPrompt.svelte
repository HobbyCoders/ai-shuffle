<script lang="ts">
	/**
	 * ProfileSystemPrompt — System prompt editor section (Studio-v2 grouped fields)
	 *
	 * One prompt field, per ruling A12: the configured prompt IS the agent's base
	 * prompt. The omni compiler wraps it with environment, instruction files,
	 * skills, and security context on every leg — there is no preset/append mode
	 * and no separate env-injection toggle anymore.
	 */
	import { FileText } from 'lucide-svelte';

	interface Props {
		systemPromptContent: string;
		onSystemPromptContentChange: (value: string) => void;
	}

	let { systemPromptContent, onSystemPromptContentChange }: Props = $props();

	const charCount = $derived(systemPromptContent.length);
</script>

<div class="set-group">
	<div class="set-grouphead">
		<h3><FileText />System prompt</h3>
		<p>
			The agent's base prompt. Environment details, instruction files, and
			security context are added automatically at runtime. Empty is allowed —
			the agent then runs with only that runtime context.
		</p>
	</div>
	<div class="set-row stack">
		<div class="lab lab-row">
			<h4>Prompt</h4>
			<span class="count">{charCount.toLocaleString()} / 10,000</span>
		</div>
		<div class="ctl">
			<textarea
				value={systemPromptContent}
				oninput={(e) => onSystemPromptContentChange((e.target as HTMLTextAreaElement).value)}
				class="fld code prompt-area"
				placeholder="Describe who this agent is and how it should work…"
			></textarea>
		</div>
	</div>
</div>

<style>
	.lab-row {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 12px;
	}
	.count {
		font-family: var(--font-mono, monospace);
		font-size: 11px;
		color: var(--muted-foreground);
		flex-shrink: 0;
	}
	textarea.prompt-area {
		min-height: 220px;
	}
</style>
