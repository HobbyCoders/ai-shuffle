<script lang="ts">
	import { ArrowUpRight, Film } from 'lucide-svelte';
	import { openMediaGallery } from '$lib/stores/lightbox';
	import type { GalleryMedia } from '$lib/utils/media-gallery';
	let { items }: { items: GalleryMedia[] } = $props();
	const previews = $derived(items.slice(0, 3));
	const images = $derived(items.filter(item => item.kind === 'image').length);
	const videos = $derived(items.length - images);
	const description = $derived([
		images ? `${images} ${images === 1 ? 'image' : 'images'}` : '',
		videos ? `${videos} ${videos === 1 ? 'video' : 'videos'}` : ''
	].filter(Boolean).join(' · '));
</script>

<button type="button" class="media-gallery" aria-haspopup="dialog"
	aria-label={`Open media gallery, ${description}`} onclick={() => openMediaGallery(items)}>
	<span class="stack" aria-hidden="true">
		{#each previews as item (item.src)}
			<span class="preview">
				{#if item.kind === 'image'}
					<img src={item.src} alt="" loading="lazy" />
				{:else}
					<Film size={22} />
				{/if}
			</span>
		{/each}
	</span>
	<span class="gallery-copy"><span class="gallery-title">Media gallery</span><span class="gallery-meta">{description}</span></span>
	<ArrowUpRight size={14} class="open-icon" aria-hidden="true" />
</button>

<style>
	.media-gallery { display:flex;align-items:center;gap:9px;width:100%;min-width:0;min-height:78px;padding:6px 8px;border:0;border-radius:8px;background:transparent;text-align:left;cursor:pointer;transition:background-color 180ms ease-out; }
	.media-gallery:hover { background:var(--scrim); }
	.media-gallery:focus-visible { outline:2px solid var(--ring);outline-offset:-2px;background:var(--scrim); }
	.stack { position:relative;display:block;width:98px;height:66px;flex:none; }
	.preview { position:absolute;display:grid;place-items:center;left:12px;top:12px;width:62px;height:44px;overflow:hidden;border:1.5px solid var(--subtle-foreground);border-radius:5px;background:var(--muted);color:var(--muted-foreground);box-shadow:0 4px 8px #0004;transition:transform 400ms ease; }
	.preview img { width:100%;height:100%;object-fit:cover; }
	.preview:nth-child(1) { transform:translate(-8px,-1px) rotate(-11deg); }
	.preview:nth-child(2) { transform:translate(9px,-2px) rotate(10deg); }
	.preview:nth-child(3) { transform:translateY(4px) rotate(-2deg); }
	.media-gallery:is(:hover,:focus-visible) .preview:nth-child(1) { transform:translate(-13px,-5px) rotate(-17deg);animation:float-left 3s .4s ease-in-out infinite; }
	.media-gallery:is(:hover,:focus-visible) .preview:nth-child(2) { transform:translate(16px,-4px) rotate(16deg);animation:float-right 3.5s .4s ease-in-out infinite; }
	.media-gallery:is(:hover,:focus-visible) .preview:nth-child(3) { transform:translateY(7px) rotate(-3deg);animation:float-front 3s .4s ease-in-out infinite; }
	.gallery-copy { display:flex;align-items:center;flex-wrap:wrap;gap:5px 10px;min-width:0; }
	.gallery-title { font-size:12.5px;font-weight:600;color:var(--foreground); }
	.gallery-meta { font-family:var(--font-mono);font-size:11.5px;color:var(--muted-foreground); }
	.media-gallery :global(.open-icon) { margin-left:auto;flex:none;color:var(--subtle-foreground); }
	@keyframes float-left { 50% { transform:translate(-15px,-8px) rotate(-19deg); } }
	@keyframes float-right { 50% { transform:translate(18px,-1px) rotate(19deg); } }
	@keyframes float-front { 50% { transform:translateY(4px) rotate(1deg); } }
	@media(max-width:639px) { .stack { width:88px; } .gallery-copy { flex-direction:column;align-items:flex-start; } .gallery-meta { font-size:10.5px; } }
	@media(prefers-reduced-motion:reduce) { .preview { transition:none;animation:none!important; } }
</style>
