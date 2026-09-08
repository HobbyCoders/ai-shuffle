<script lang="ts">
	import { Download, Smartphone, ExternalLink } from 'lucide-svelte';

	const androidRelease = 'https://github.com/HobbyCoders/ai-shuffle/releases/tag/android-latest';
	const androidApk = 'https://github.com/HobbyCoders/ai-shuffle/releases/download/android-latest/AI-Shuffle-Android.apk';
</script>

<section class="set-group" aria-labelledby="android-download-title">
	<div class="set-grouphead">
		<h3 id="android-download-title"><Smartphone />Android APK</h3>
		<p>Use AI Shuffle on your phone and connect to your workers.</p>
	</div>
	<div class="set-row stack">
		<div class="lab">
			<h4>Latest verified Android build</h4>
			<p>For Android 8.0 and newer. Updated after the Android checks pass.</p>
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
	<div class="set-row stack">
		<div class="lab">
			<h4>Install or update</h4>
			<p>Open the downloaded APK on your phone and allow installation from your browser if Android asks. Install over your existing AI Shuffle app to keep your accounts, workers and settings.</p>
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
