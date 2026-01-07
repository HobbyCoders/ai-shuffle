<script lang="ts">
	/**
	 * InfoTabContent - Session stats, costs, tokens
	 */

	import { Info, Cpu, Coins, Clock, Zap } from 'lucide-svelte';
	import type { SessionInfo } from '../types';

	interface Props {
		sessionInfo?: SessionInfo | null;
	}

	let {
		sessionInfo = null
	}: Props = $props();

	function formatCost(cost: number): string {
		return cost < 0.01 ? '<$0.01' : `$${cost.toFixed(2)}`;
	}

	function formatTokens(tokens: number): string {
		if (tokens >= 1000000) return `${(tokens / 1000000).toFixed(1)}M`;
		if (tokens >= 1000) return `${(tokens / 1000).toFixed(1)}K`;
		return tokens.toString();
	}

	function formatDuration(seconds: number): string {
		const mins = Math.floor(seconds / 60);
		const secs = seconds % 60;
		if (mins === 0) return `${secs}s`;
		return `${mins}m ${secs}s`;
	}
</script>

<div class="info-content">
	<div class="section">
		<div class="section-header">
			<Info size={14} strokeWidth={1.5} />
			<span>Session Info</span>
		</div>

		{#if !sessionInfo}
			<div class="empty-state">No session selected</div>
		{:else}
			<div class="info-grid">
				<!-- Model -->
				<div class="info-card">
					<div class="info-icon">
						<Cpu size={16} strokeWidth={1.5} />
					</div>
					<div class="info-details">
						<span class="info-label">Model</span>
						<span class="info-value">{sessionInfo.model}</span>
					</div>
				</div>

				<!-- Tokens -->
				<div class="info-card">
					<div class="info-icon">
						<Zap size={16} strokeWidth={1.5} />
					</div>
					<div class="info-details">
						<span class="info-label">Tokens</span>
						<span class="info-value">
							<span class="token-in">{formatTokens(sessionInfo.tokens.input)}</span>
							<span class="token-separator">/</span>
							<span class="token-out">{formatTokens(sessionInfo.tokens.output)}</span>
						</span>
					</div>
				</div>

				<!-- Cost -->
				<div class="info-card">
					<div class="info-icon">
						<Coins size={16} strokeWidth={1.5} />
					</div>
					<div class="info-details">
						<span class="info-label">Cost</span>
						<span class="info-value cost">{formatCost(sessionInfo.cost)}</span>
					</div>
				</div>

				<!-- Duration -->
				{#if sessionInfo.duration}
					<div class="info-card">
						<div class="info-icon">
							<Clock size={16} strokeWidth={1.5} />
						</div>
						<div class="info-details">
							<span class="info-label">Duration</span>
							<span class="info-value">{formatDuration(sessionInfo.duration)}</span>
						</div>
					</div>
				{/if}
			</div>

			<!-- Token Breakdown -->
			<div class="token-breakdown">
				<div class="breakdown-header">Token Usage</div>
				<div class="breakdown-bar">
					<div
						class="bar-segment input"
						style:width="{(sessionInfo.tokens.input / sessionInfo.tokens.total) * 100}%"
						title="Input: {formatTokens(sessionInfo.tokens.input)}"
					></div>
					<div
						class="bar-segment output"
						style:width="{(sessionInfo.tokens.output / sessionInfo.tokens.total) * 100}%"
						title="Output: {formatTokens(sessionInfo.tokens.output)}"
					></div>
				</div>
				<div class="breakdown-labels">
					<div class="breakdown-label">
						<span class="dot input"></span>
						<span>Input</span>
					</div>
					<div class="breakdown-label">
						<span class="dot output"></span>
						<span>Output</span>
					</div>
				</div>
			</div>
		{/if}
	</div>
</div>

<style>
	.info-content {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.section {
		background: var(--secondary);
		border: 1px solid var(--border);
		border-radius: 10px;
		overflow: hidden;
	}

	.section-header {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 10px 12px;
		font-size: 0.75rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--foreground);
		border-bottom: 1px solid var(--border);
	}

	.empty-state {
		padding: 24px 16px;
		text-align: center;
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	.info-grid {
		display: grid;
		grid-template-columns: repeat(2, 1fr);
		gap: 8px;
		padding: 12px;
	}

	.info-card {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 10px;
		background: var(--muted);
		border: 1px solid var(--border);
		border-radius: 8px;
	}

	.info-icon {
		flex-shrink: 0;
		width: 32px;
		height: 32px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: var(--background);
		border-radius: 6px;
		color: var(--muted-foreground);
	}

	.info-details {
		display: flex;
		flex-direction: column;
		gap: 2px;
		min-width: 0;
	}

	.info-label {
		font-size: 0.625rem;
		color: var(--muted-foreground);
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.info-value {
		font-size: 0.8125rem;
		font-weight: 600;
		color: var(--foreground);
	}

	.info-value.cost {
		color: var(--success);
	}

	.token-in {
		color: #3b82f6;
	}

	.token-separator {
		color: var(--muted-foreground);
		margin: 0 2px;
	}

	.token-out {
		color: #10b981;
	}

	.token-breakdown {
		padding: 12px;
		border-top: 1px solid var(--border);
	}

	.breakdown-header {
		font-size: 0.6875rem;
		font-weight: 600;
		color: var(--muted-foreground);
		text-transform: uppercase;
		letter-spacing: 0.05em;
		margin-bottom: 8px;
	}

	.breakdown-bar {
		display: flex;
		height: 8px;
		background: var(--muted);
		border-radius: 4px;
		overflow: hidden;
		margin-bottom: 8px;
	}

	.bar-segment {
		height: 100%;
		transition: width 0.3s ease;
	}

	.bar-segment.input {
		background: #3b82f6;
	}

	.bar-segment.output {
		background: #10b981;
	}

	.breakdown-labels {
		display: flex;
		gap: 16px;
	}

	.breakdown-label {
		display: flex;
		align-items: center;
		gap: 6px;
		font-size: 0.6875rem;
		color: var(--muted-foreground);
	}

	.dot {
		width: 8px;
		height: 8px;
		border-radius: 50%;
	}

	.dot.input {
		background: #3b82f6;
	}

	.dot.output {
		background: #10b981;
	}
</style>
