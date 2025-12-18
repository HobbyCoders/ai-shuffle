import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [sveltekit()],
	server: {
		proxy: {
			'/api': {
				target: 'http://localhost:8000',
				changeOrigin: true,
				cookieDomainRewrite: '',
				secure: false
			},
			'/health': {
				target: 'http://localhost:8000',
				changeOrigin: true
			}
		}
	}
});
