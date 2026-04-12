<script lang="ts">
	/**
	 * ProfileSystemPrompt — System prompt editor section
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
</script>

<div class="pf-section">
	<div class="pf-section-header">
		<FileText size={16} />
		<h3>System Prompt Mode</h3>
	</div>
	<p class="pf-desc">Choose how the system prompt is configured for this profile.</p>
	<select class="pf-select" value={systemPromptType} onchange={(e) => onSystemPromptTypeChange((e.target as HTMLSelectElement).value)}>
		<option value="preset">Claude Code Default + Append</option>
		<option value="custom">Custom Prompt</option>
	</select>
</div>

<div class="pf-section pf-section-prompt">
	<div class="pf-section-header">
		<MessageSquare size={16} />
		<h3>Custom Prompt</h3>
		<span class="pf-char-count">
			{(systemPromptType === 'preset' ? systemPromptAppend : systemPromptContent).length.toLocaleString()} / 10,000
		</span>
	</div>
	{#if systemPromptType === 'preset'}
		<textarea
			value={systemPromptAppend}
			oninput={(e) => onSystemPromptAppendChange((e.target as HTMLTextAreaElement).value)}
			class="pf-textarea"
			placeholder="Additional instructions to append to Claude Code's system prompt..."
		></textarea>
	{:else}
		<textarea
			value={systemPromptContent}
			oninput={(e) => onSystemPromptContentChange((e.target as HTMLTextAreaElement).value)}
			class="pf-textarea"
			placeholder="Enter your custom system prompt..."
		></textarea>
	{/if}
</div>

<div class="pf-toggle-row">
	<div class="pf-toggle-info">
		<span class="pf-toggle-title">Inject Environment Details</span>
		<span class="pf-toggle-desc">Automatically inject OS, platform, and working directory info into the prompt.</span>
	</div>
	<button
		type="button"
		class="pf-toggle"
		class:active={systemPromptInjectEnv}
		role="switch"
		aria-checked={systemPromptInjectEnv}
		aria-label="Toggle inject environment details"
		onclick={onSystemPromptInjectEnvToggle}
	>
		<span class="pf-toggle-knob"></span>
	</button>
</div>

<style>
	.pf-section {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.pf-section-header {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--text-primary);
	}

	.pf-section-header h3 {
		font-size: var(--text-md, 0.875rem);
		font-weight: 600;
		margin: 0;
		color: var(--text-primary);
	}

	.pf-desc {
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary);
		margin: 0;
	}

	.pf-char-count {
		margin-left: auto;
		font-size: var(--text-xs, 0.6875rem);
		color: var(--text-secondary);
		font-weight: 400;
	}

	.pf-select {
		width: 100%;
		padding: 10px 14px;
		padding-right: 36px;
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		color: var(--text-primary);
		font-size: var(--text-md, 0.875rem);
		font-family: inherit;
		cursor: pointer;
		outline: none;
		appearance: none;
		background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='16' viewBox='0 0 24 24' fill='none' stroke='%239090A8' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E");
		background-repeat: no-repeat;
		background-position: right 12px center;
		background-size: 16px;
		transition: border-color 200ms, box-shadow 200ms;
	}

	.pf-select:focus {
		border-color: var(--primary);
		box-shadow: 0 0 0 3px color-mix(in oklch, var(--primary) 15%, transparent);
	}

	.pf-section-prompt {
		flex: 1;
	}

	.pf-textarea {
		width: 100%;
		flex: 1;
		min-height: 200px;
		padding: 12px 14px;
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		color: var(--text-primary);
		font-size: var(--text-md, 0.875rem);
		font-family: inherit;
		resize: vertical;
		outline: none;
		transition: border-color 200ms, box-shadow 200ms;
	}

	.pf-textarea:focus {
		border-color: var(--primary);
		box-shadow: 0 0 0 3px color-mix(in oklch, var(--primary) 15%, transparent);
	}

	.pf-textarea::placeholder {
		color: var(--text-tertiary);
	}

	.pf-toggle-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 14px 16px;
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
	}

	.pf-toggle-info {
		display: flex;
		flex-direction: column;
		gap: 2px;
		flex: 1;
		min-width: 0;
	}

	.pf-toggle-title {
		font-size: var(--text-md, 0.875rem);
		font-weight: 500;
		color: var(--text-primary);
	}

	.pf-toggle-desc {
		font-size: var(--text-xs, 0.6875rem);
		color: var(--text-secondary);
	}

	.pf-toggle {
		position: relative;
		width: 44px;
		height: 24px;
		background: var(--muted);
		border: 1px solid var(--border-subtle);
		border-radius: 12px;
		cursor: pointer;
		transition: all 200ms ease;
		flex-shrink: 0;
		margin-left: 16px;
	}

	.pf-toggle.active {
		background: var(--primary);
		border-color: var(--primary);
	}

	.pf-toggle-knob {
		position: absolute;
		top: 2px;
		left: 2px;
		width: 18px;
		height: 18px;
		background: var(--primary-foreground);
		border-radius: 50%;
		transition: transform 200ms ease;
	}

	.pf-toggle.active .pf-toggle-knob {
		transform: translateX(20px);
	}
</style>
