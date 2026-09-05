import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '@fontsource-variable/jetbrains-mono';
import '../src/app.css';
// Install mocks before importing the real component/store graph.
localStorage.setItem('ai-shuffle.sessionMode', 'chat');
window.fetch = async (input) => {
 const url = String(input);
 const data = url.includes('/auth/status') ? { authenticated: true, claude_authenticated: true, claude_token_valid: true, has_any_credential: true, onboarding_complete: true } : [];
 return new Response(JSON.stringify(data), { status: 200, headers: {'Content-Type':'application/json'} });
};
window.WebSocket = class { constructor() { throw new Error('Fixture forbids backend WebSockets'); } } as unknown as typeof WebSocket;
const { auth } = await import('../src/lib/stores/auth.svelte');
await auth.checkAuth();
const { default: ComposerGesture } = await import('./ComposerGesture.svelte');
mount(ComposerGesture, { target: document.getElementById('app')! });
