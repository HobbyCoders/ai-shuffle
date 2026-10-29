<!-- Shared by cold history loads and the delayed switch overlay. -->
<div class="loading-transcript" role="status" aria-live="polite" aria-atomic="true">
	<span class="sr-only">Loading conversation…</span>
	{#each [0, 1, 2] as group (group)}
		<div class="sk-turn" aria-hidden="true">
			<div class="sk-line sk-prompt"></div>
			<div class="sk-response">
				<div class="sk-line sk-name"></div>
				<div class="sk-line"></div>
				<div class="sk-line"></div>
				<div class="sk-line sk-short"></div>
			</div>
		</div>
	{/each}
</div>

<style>
	.loading-transcript {
		width: 100%;
		max-width: 768px;
		margin: 0 auto;
		padding: 26px 24px 0;
		display: flex;
		flex-direction: column;
		gap: 22px;
	}
	.sk-turn { display: flex; flex-direction: column; gap: 12px; }
	.sk-response { display: flex; flex-direction: column; gap: 9px; }
	.sk-line {
		height: 12px;
		border-radius: 6px;
		background: color-mix(in oklch, var(--foreground) 9%, transparent);
		animation: sk-pulse 1.6s ease-in-out infinite;
	}
	.sk-prompt {
		height: 34px;
		width: 46%;
		align-self: flex-end;
		border-radius: 14px;
		background: color-mix(in oklch, var(--foreground) 12%, transparent);
	}
	.sk-name { width: 118px; height: 10px; }
	.sk-short { width: 62%; }
	.sk-turn:nth-child(2) .sk-prompt { width: 34%; }
	.sk-turn:nth-child(3) .sk-prompt { width: 52%; }
	.sk-turn:nth-child(2) .sk-short { width: 44%; }
	.sk-turn:nth-child(2) .sk-line { animation-delay: 120ms; }
	.sk-turn:nth-child(3) .sk-line { animation-delay: 240ms; }
	@keyframes sk-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.45; } }
	@media (prefers-reduced-motion: reduce) { .sk-line { animation: none; } }
</style>
