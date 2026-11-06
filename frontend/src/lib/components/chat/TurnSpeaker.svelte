<script lang="ts">
	/**
	 * The name line of an assistant turn. An assistant reply normally carries it
	 * itself; when the turn's activity row comes first, the transcript places it
	 * above that row (`tight`, since the row brings its own padding).
	 */
	import { formatMessageTimeFull } from '$lib/utils/message-time';

	interface Props {
		name: string;
		timestamp?: Date | string;
		streaming?: boolean;
		tight?: boolean;
	}
	let { name, timestamp, streaming = false, tight = false }: Props = $props();
</script>

<div class="aname" class:tight title={formatMessageTimeFull(timestamp)}>
	{#if streaming}
		<span class="live" aria-label="Streaming"></span>
	{/if}
	<span class="aname-text">{name}</span>
</div>

<style>
	.aname {
		display: flex;
		align-items: center;
		gap: 7px;
		font-size: 11.5px;
		font-weight: 500;
		letter-spacing: 0.02em;
		color: var(--subtle-foreground);
		margin-bottom: 9px;
		min-height: 16px;
	}

	.aname.tight {
		margin-bottom: 3px;
	}

	.aname-text {
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.live {
		width: 5px;
		height: 5px;
		flex: none;
		border-radius: 50%;
		background: var(--primary);
		animation: aname-pulse 1.4s ease-in-out infinite;
	}

	@keyframes aname-pulse {
		0%, 100% { opacity: 1; }
		50% { opacity: 0.35; }
	}

	@media (prefers-reduced-motion: reduce) {
		.live { animation: none; }
	}
</style>
