/** @type {import('tailwindcss').Config} */
export default {
	content: ['./src/**/*.{html,js,svelte,ts}'],
	theme: {
		extend: {
			colors: {
				primary: {
					50: '#fdf4f3',
					100: '#fce7e4',
					200: '#fbd3cd',
					300: '#f7b3a9',
					400: '#f08778',
					500: '#e5604d',
					600: '#d14430',
					700: '#af3625',
					800: '#913022',
					900: '#782d22',
					950: '#41140d'
				}
			}
		}
	},
	plugins: []
};
