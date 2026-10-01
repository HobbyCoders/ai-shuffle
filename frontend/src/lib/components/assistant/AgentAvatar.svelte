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
  {:else if avatar === 'owl'}
   <path class="shape" d="M12 12q8 2 12 8h16q4-6 12-8v28c0 11-9 16-20 16S12 51 12 40Z" />
   <circle class="ring" cx="25" cy="31" r="7" /><circle class="ring" cx="39" cy="31" r="7" />
   <path class="eyes" d="M25 31v.5m14-.5v.5" />
   <path class="mark" d="m29 40 3 5 3-5Z" />
  {:else if avatar === 'fox'}
   <path class="shape" d="m10 12 14 10h16l14-10-4 24-18 16-18-16Z" />
   <path class="eyes" d="M25 31v3m14-3v3" />
   <path class="eyes" d="M30 42h4" />
  {:else if avatar === 'cat'}
   <path class="shape" d="m13 13 11 9h16l11-9v25c0 10-8 15-19 15s-19-5-19-15Z" />
   <path d="M6 37h11M6 44l11-3m41-4H47m11 7-11-3" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
   <path class="eyes" d="M25 31v3m14-3v3" />
   <path class="smile" d="M29 41q3 3 6 0" />
  {:else if avatar === 'bear'}
   <circle class="shape" cx="17" cy="17" r="7" /><circle class="shape" cx="47" cy="17" r="7" />
   <circle class="shape" cx="32" cy="35" r="20" />
   <ellipse class="ring" cx="32" cy="43" rx="8" ry="6" />
   <path class="eyes" d="M25 30v3m14-3v3" />
   <path class="eyes" d="M30 41h4" />
  {:else if avatar === 'ghost'}
   <path class="shape" d="M14 52V28a18 18 0 0 1 36 0v24l-6-5-6 5-6-5-6 5-6-5Z" />
   <path class="eyes" d="M26 28v5m12-5v5" />
   <path class="smile" d="M29 39q3 2 6 0" />
  {:else if avatar === 'cloud'}
   <path class="shape" d="M18 46a10 10 0 0 1 0-20 14 14 0 0 1 27-4 10 10 0 0 1 3 24Z" />
   <path class="eyes" d="M26 33v3m12-3v3" />
   <path class="smile" d="M29 40q3 3 6 0" />
  {:else if avatar === 'moon'}
   <path class="shape" d="M31.4 14A20 20 0 1 0 48.9 40.4 16 16 0 0 1 31.4 14Z" />
   <path class="eyes" d="M20 31v4" />
   <path class="smile" d="M21 42q4 3 8 0" />
  {:else if avatar === 'sun'}
   <path d="M32 5v7m0 40v7M5 32h7m40 0h7M13 13l5 5m28 28 5 5M13 51l5-5m28-28 5-5" stroke="currentColor" stroke-width="3" stroke-linecap="round" />
   <circle class="shape" cx="32" cy="32" r="14" />
   <path class="eyes" d="M27 29v3m10-3v3" />
   <path class="smile" d="M28 37q4 3 8 0" />
  {:else if avatar === 'bolt'}
   <path class="shape" d="M36 4 14 36h14l-4 24 26-34H36Z" />
   <path class="eyes" d="M27 29v3m8-3v3" />
   <path class="smile" d="M29 38q3 2 6 0" />
  {:else if avatar === 'gem'}
   <path class="shape" d="M20 10h24l12 14-24 32L8 24Z" />
   <path d="M8 24h48M22 24l4-14m16 14-4-14" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" />
   <path class="eyes" d="M27 30v3m10-3v3" />
   <path class="smile" d="M29 38q3 2 6 0" />
  {:else if avatar === 'drop'}
   <path class="shape" d="M32 6C22 22 14 31 14 40a18 18 0 0 0 36 0c0-9-8-18-18-34Z" />
   <path class="eyes" d="M26 38v3m12-3v3" />
   <path class="smile" d="M29 46q3 3 6 0" />
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
 .ring { fill:var(--background); stroke:currentColor; stroke-width:2; }
 .mark { fill:currentColor; stroke:currentColor; stroke-width:1.5; stroke-linejoin:round; }
</style>
