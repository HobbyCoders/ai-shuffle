<script lang="ts">
	import { onMount } from 'svelte';

	let canvas: HTMLCanvasElement;
	let particles: Array<{
		x: number;
		y: number;
		vx: number;
		vy: number;
		size: number;
		opacity: number;
		pulse: number;
	}> = [];
	let animationId: number;

	onMount(() => {
		const ctx = canvas.getContext('2d');
		if (!ctx) return;

		function resizeCanvas() {
			canvas.width = window.innerWidth;
			canvas.height = window.innerHeight;
		}

		function createParticles() {
			particles = [];
			const count = Math.min(35, Math.floor(window.innerWidth / 35));

			for (let i = 0; i < count; i++) {
				particles.push({
					x: Math.random() * canvas.width,
					y: Math.random() * canvas.height,
					vx: (Math.random() - 0.5) * 0.2,
					vy: (Math.random() - 0.5) * 0.15,
					size: Math.random() * 2 + 0.5,
					opacity: Math.random() * 0.25 + 0.05,
					pulse: Math.random() * Math.PI * 2
				});
			}
		}

		function animate() {
			ctx.clearRect(0, 0, canvas.width, canvas.height);

			particles.forEach((p) => {
				// Update position
				p.x += p.vx;
				p.y += p.vy;
				p.pulse += 0.01;

				// Wrap around edges
				if (p.x < 0) p.x = canvas.width;
				if (p.x > canvas.width) p.x = 0;
				if (p.y < 0) p.y = canvas.height;
				if (p.y > canvas.height) p.y = 0;

				// Pulsing opacity
				const opacity = p.opacity * (0.7 + 0.3 * Math.sin(p.pulse));

				// Draw particle
				ctx.beginPath();
				ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
				ctx.fillStyle = `rgba(245, 230, 200, ${opacity})`;
				ctx.fill();
			});

			animationId = requestAnimationFrame(animate);
		}

		resizeCanvas();
		createParticles();
		animate();

		const handleResize = () => {
			resizeCanvas();
			createParticles();
		};

		window.addEventListener('resize', handleResize);

		return () => {
			window.removeEventListener('resize', handleResize);
			cancelAnimationFrame(animationId);
			particles = [];
		};
	});
</script>

<canvas bind:this={canvas} class="spotlight-canvas"></canvas>

<style>
	.spotlight-canvas {
		position: absolute;
		top: 0;
		left: 0;
		width: 100%;
		height: 100%;
		pointer-events: none;
		z-index: 1;
	}

	@media (prefers-reduced-motion: reduce) {
		.spotlight-canvas {
			display: none;
		}
	}
</style>
