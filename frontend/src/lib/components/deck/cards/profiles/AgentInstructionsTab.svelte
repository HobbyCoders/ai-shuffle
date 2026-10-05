<script lang="ts">
	/**
	 * The Instructions tab: the agent's base prompt (ruling A12). The fluid
	 * compiler wraps it with environment, instruction files, skills and
	 * security context on every leg, so there is no preset/append mode. A new
	 * agent can start from a template.
	 */
	import RowGroup from '$lib/components/settings/rows/RowGroup.svelte';
	import SettingRow from '$lib/components/settings/rows/SettingRow.svelte';
	import SelectField from '$lib/components/settings/rows/SelectField.svelte';
	import type { AgentTemplate } from './agentForm';

	const PROMPT_LIMIT = 10_000;

	interface Props {
		prompt: string;
		onPrompt: (value: string) => void;
		/** Offered only while the agent is new. */
		templates: AgentTemplate[];
		isNew: boolean;
		onApplyTemplate: (templateId: string) => void;
	}

	let { prompt, onPrompt, templates, isNew, onApplyTemplate }: Props = $props();
	let templateValue = $state('');
	const over = $derived(prompt.length > PROMPT_LIMIT);
</script>

{#if isNew && templates.length > 0}
	<RowGroup title="Start from">
		<SettingRow label="Template" hint="Replaces the prompt.">
			<SelectField
				bind:value={templateValue}
				label="Prompt template"
				options={[{ value: '', label: 'Choose a template' }, ...templates.map((t) => ({ value: t.id, label: t.name }))]}
				onchange={(v) => {
					if (v) onApplyTemplate(v);
					templateValue = '';
				}}
			/>
		</SettingRow>
	</RowGroup>
{/if}

<RowGroup title="Prompt">
	{#snippet actions()}
		<span class="count" class:over>{prompt.length.toLocaleString('en-US')} / 10,000</span>
	{/snippet}
	<div class="body">
		<textarea
			class="prompt"
			aria-label="System prompt"
			placeholder="Who is this agent and how should it work?"
			value={prompt}
			oninput={(e) => onPrompt((e.target as HTMLTextAreaElement).value)}
		></textarea>
	</div>
</RowGroup>
<p class="st-note">Runtime context is added automatically.</p>

<style>
	.body {
		padding: 14px 16px 16px;
	}
	.prompt {
		display: block;
		width: 100%;
		min-height: 320px;
		padding: 10px 12px;
		border: 1px solid var(--st-input);
		background: var(--code-bg, var(--st-muted));
		border-radius: var(--st-radius-md);
		font-family: var(--st-mono);
		font-size: 12.5px;
		line-height: 1.55;
		color: var(--st-fg);
		resize: vertical;
		outline: none;
	}
	.prompt::placeholder {
		color: var(--st-subtle);
	}
	.prompt:focus {
		border-color: var(--st-primary);
		box-shadow: 0 0 0 3px var(--st-primary-soft);
	}
	.count {
		font-family: var(--st-mono);
		font-size: 11.5px;
		color: var(--st-subtle);
		font-variant-numeric: tabular-nums;
	}
	.count.over {
		color: var(--st-destructive);
	}
	.st-note {
		margin: 8px 2px 0;
		font-size: 12.5px;
		color: var(--st-mfg);
	}
	@container (max-width: 560px) {
		.body {
			padding: 12px;
		}
		.prompt {
			min-height: 260px;
		}
	}
</style>
