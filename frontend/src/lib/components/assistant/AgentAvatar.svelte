<script lang="ts">
 /** Shared, credential-free agent presentation. No image network requests. */
 import type { AvatarId } from '$lib/api/assistant';
 let { avatar = 'robot', size = 40 }: { avatar?: AvatarId; size?: number } = $props();
</script>

<span class="agent-avatar" data-avatar={avatar} style:width="{size}px" style:height="{size}px" aria-hidden="true">
 <svg viewBox="0 0 64 64" fill="none">
  {#if avatar === 'spark'}
   <path class="shape" d="m32 5 7 15 16 2-10 13 2 17-15-7-15 7 2-17L9 22l16-2Z" />
   <path class="eyes" d="M26 28v4m12-4v4" />
   <path class="smile" d="M28 38q4 4 8 0" />
  {:else if avatar === 'orbit'}
   <circle class="shape" cx="32" cy="32" r="19" />
   <ellipse cx="32" cy="32" rx="29" ry="10" transform="rotate(-28 32 32)" stroke="currentColor" stroke-width="2" />
   <path class="eyes" d="M26 27v4m12-4v4" />
   <path class="smile" d="M29 37q3 3 6 0" />
  {:else if avatar === 'sprout'}
   <path class="shape" d="M32 23C15 23 12 13 15 7c13-1 18 5 17 16Zm0 0C32 9 40 5 50 8c1 10-6 16-18 15Z" />
   <path class="shape" d="M15 28h34l-5 23H20Z" />
   <path class="eyes" d="M26 34v4m12-4v4" />
   <path class="smile" d="M29 43q3 3 6 0" />
  {:else if avatar === 'pixel'}
   <path class="shape" d="M18 12h28v6h6v28h-6v6H18v-6h-6V18h6Z" />
   <path class="eyes" d="M24 26v6m16-6v6" />
   <path class="smile" d="M25 41h14" />
  {:else}
   <path d="M32 10v9" stroke="currentColor" stroke-width="3" /><circle cx="32" cy="8" r="4" fill="currentColor" />
   <rect class="shape" x="12" y="19" width="40" height="33" rx="12" />
   <path d="M7 30v11m50-11v11" stroke="currentColor" stroke-width="4" stroke-linecap="round" />
   <path class="eyes" d="M24 29v5m16-5v5" />
   <path class="smile" d="M26 42q6 5 12 0" />
  {/if}
 </svg>
</span>

<style>
 .agent-avatar { display:inline-flex; flex:none; align-items:center; justify-content:center; color:var(--primary); background:color-mix(in oklch,var(--primary) 9%,var(--background)); border:1px solid color-mix(in oklch,var(--primary) 22%,var(--border)); border-radius:28%; overflow:hidden; }
 svg { width:86%; height:86%; }
 .shape { fill:color-mix(in oklch,var(--primary) 26%,var(--background)); stroke:currentColor; stroke-width:2; stroke-linejoin:round; }
 .eyes { stroke:var(--foreground); stroke-width:4; stroke-linecap:round; }
 .smile { stroke:var(--foreground); stroke-width:2; stroke-linecap:round; }
</style>
