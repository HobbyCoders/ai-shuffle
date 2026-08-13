<script lang="ts">
	/**
	 * ProfileSystemPrompt — System prompt editor section (Studio-v2 grouped fields)
	 */
	import { FileText, MessageSquare } from 'lucide-svelte';

	interface Props {
		systemPromptType: string;
		systemPromptAppend: string;
		systemPromptContent: string;
		systemPromptInjectEnv: boolean;
		onSystemPromptTypeChange: (value: string) => void;
		onSystemPromptAppendChange: (value: string) => void;
		onSystemPromptContentChange: (value: string) => void;
		onSystemPromptInjectEnvToggle: () => void;
	}

	let {
		systemPromptType,
		systemPromptAppend,
		systemPromptContent,
		systemPromptInjectEnv,
		onSystemPromptTypeChange,
		onSystemPromptAppendChange,
		onSystemPromptContentChange,
		onSystemPromptInjectEnvToggle
	}: Props = $props();

	const isPreset = $derived(systemPromptType === 'preset');
	const charCount = $derived((isPreset ? systemPromptAppend : systemPromptContent).length);
</script>

<div class="set-group">
	<div class="set-grouphead"><h3><FileText />System prompt</h3><p>Choose how the system prompt is configured for this agent.</p></div>
	<div class="set-row">
		<div class="lab"><h4>Mode</h4><p>Append to Claude Code's default, or replace it entirely.</p></div>
		<div class="ctl">
			<select class="sel" value={systemPromptType} onchange={(e) => onSystemPromptTypeChange((e.target as HTMLSelectElement).value)}>
				<option value="preset">Claude Code Default + Append</option>
				<option value="custom">Custom Prompt</option>
			</select>
		</div>
	</div>
	<div class="set-row stack">
		<div class="lab lab-row">
			<h4>{isPreset ? 'Append to default' : 'Custom prompt'}</h4>
			<span class="count">{charCount.toLocaleString()} / 10,000</span>
		</div>
		<div class="ctl">
			{#if isPreset}
				<textarea
					value={systemPromptAppend}
					oninput={(e) => onSystemPromptAppendChange((e.target as HTMLTextAreaElement).value)}
					class="fld code prompt-area"
					placeholder="Additional instructions to append to Claude Code's system prompt…"
				></textarea>
			{:else}
				<textarea
					value={systemPromptContent}
					oninput={(e) => onSystemPromptContentChange((e.target as HTMLTextAreaElement).value)}
					class="fld code prompt-area"
					placeholder="Enter your custom system prompt…"
				></textarea>
			{/if}
		</div>
	</div>
</div>

<div class="set-group">
	<div class="set-grouphead"><h3><MessageSquare />Context injection</h3></div>
	<div class="set-row">
		<div class="lab"><h4>Inject environment details</h4><p>Automatically inject OS, platform, and working directory info into the prompt.</p></div>
		<div class="ctl">
			<button
				type="button"
				class="switch"
				class:on={systemPromptInjectEnv}
				role="switch"
				aria-checked={systemPromptInjectEnv}
				aria-label="Toggle inject environment details"
				onclick={onSystemPromptInjectEnvToggle}
			><i></i></button>
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
