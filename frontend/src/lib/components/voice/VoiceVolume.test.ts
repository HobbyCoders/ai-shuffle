import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, expect, it } from 'vitest';
import { get } from 'svelte/store';
import VoiceVolume from './VoiceVolume.svelte';
import { setVoiceVolume, voiceVolume } from '$lib/services/voiceVolume';
afterEach(() => { cleanup(); setVoiceVolume(100); localStorage.clear(); });
it('allows in-call boost and updates the saved device volume immediately', async () => {
 setVoiceVolume(100); render(VoiceVolume);
 const slider = screen.getByRole('slider', { name: 'Voice playback volume' });
 expect(slider.getAttribute('max')).toBe('300');
 await fireEvent.input(slider, { target: { value: '250' } });
 expect(get(voiceVolume)).toBe(250);
 expect(localStorage.getItem('voice-playback-volume')).toBe('250');
 expect(screen.getByText('250%')).toBeTruthy();
 expect(slider.getAttribute('aria-valuetext')).toBe('250% (boosted)');
});
