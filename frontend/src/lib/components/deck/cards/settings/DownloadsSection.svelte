<script lang="ts">
	import { Download, Smartphone, ExternalLink } from 'lucide-svelte';

	const androidRelease = 'https://github.com/HobbyCoders/ai-shuffle/releases/tag/android-latest';
	const androidApk = 'https://github.com/HobbyCoders/ai-shuffle/releases/download/android-latest/AI-Shuffle-Android.apk';
</script>

<section class="set-group" aria-labelledby="android-download-title">
	<div class="set-grouphead">
		<h3 id="android-download-title"><Smartphone />Android APK</h3>
	</div>
	<div class="set-row stack">
		<div class="lab">
			<h4>Latest verified Android build</h4>
		</div>
		<div class="ctl download-actions">
			<a class="ed-btn primary" href={androidApk} target="_blank" rel="noopener noreferrer">
				<Download size={16} />Download latest APK
			</a>
			<a class="ed-btn" href={androidRelease} target="_blank" rel="noopener noreferrer">
				<ExternalLink size={16} />Version &amp; release notes
			</a>
		</div>
	</div>
</section>

<style>
	.download-actions {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
	}
	a {
		min-height: 44px;
		text-decoration: none;
	}
</style>
