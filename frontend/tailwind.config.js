/** @type {import('tailwindcss').Config} */
export default {
	darkMode: 'class',
	content: ['./src/**/*.{html,js,svelte,ts}'],
	theme: {
		extend: {
			colors: {
				border: 'var(--border)',
				input: 'var(--input)',
				ring: 'var(--ring)',
				background: 'var(--background)',
				foreground: 'var(--foreground)',
				primary: {
					DEFAULT: 'var(--primary)',
					foreground: 'var(--primary-foreground)'
				},
				secondary: {
					DEFAULT: 'var(--secondary)',
					foreground: 'var(--secondary-foreground)'
				},
				destructive: {
					DEFAULT: 'var(--destructive)',
					foreground: 'var(--destructive-foreground)'
				},
				success: {
					DEFAULT: 'var(--success)',
					foreground: 'var(--success-foreground)'
				},
				warning: {
					DEFAULT: 'var(--warning)',
					foreground: 'var(--warning-foreground)'
				},
				info: {
					DEFAULT: 'var(--info)',
					foreground: 'var(--info-foreground)'
				},
				muted: {
					DEFAULT: 'var(--muted)',
					foreground: 'var(--muted-foreground)'
				},
				accent: {
					DEFAULT: 'var(--accent)',
					foreground: 'var(--accent-foreground)'
				},
				popover: {
					DEFAULT: 'var(--popover)',
					foreground: 'var(--popover-foreground)'
				},
				card: {
					DEFAULT: 'var(--card)',
					foreground: 'var(--card-foreground)'
				},
				selection: {
					DEFAULT: 'var(--selection)',
					ring: 'var(--selection-ring)',
					glow: 'var(--selection-glow)',
					text: 'var(--selection-text)'
				},
				/* Studio Noir token pass-through */
				'subtle-foreground': 'var(--subtle-foreground)',
				'border-strong': 'var(--border-strong)',
				sidebar: {
					DEFAULT: 'var(--sidebar)',
					foreground: 'var(--sidebar-foreground)',
					border: 'var(--sidebar-border)',
					accent: 'var(--sidebar-accent)',
					'accent-active': 'var(--sidebar-accent-active)',
					muted: 'var(--sidebar-muted)'
				},
				scrim: 'var(--scrim)',
				'code-bg': 'var(--code-bg)'
			},
			borderRadius: {
				lg: 'var(--radius)',
				md: 'calc(var(--radius) - 2px)',
				sm: 'calc(var(--radius) - 4px)'
			},
			boxShadow: {
				s: 'var(--shadow-s)',
				m: 'var(--shadow-m)',
				l: 'var(--shadow-l)',
				selection: 'var(--selection-shadow)',
				'selection-subtle': 'var(--selection-shadow-subtle)'
			},
			backgroundColor: {
				'hover-overlay': 'var(--hover-overlay)',
				'active-overlay': 'var(--active-overlay)',
				'input-bg': 'var(--input-bg)',
				'input-hover': 'var(--input-bg-hover)',
				'input-focus': 'var(--input-bg-focus)',
				'status-success': 'var(--status-success-bg)',
				'status-warning': 'var(--status-warning-bg)',
				'status-error': 'var(--status-error-bg)',
				'status-info': 'var(--status-info-bg)',
				'selection': 'var(--selection-bg)',
				'selection-emphasis': 'var(--selection-bg-emphasis)'
			},
			borderColor: {
				'subtle': 'var(--border-subtle)',
				'input-border': 'var(--input-border)',
				'input-hover': 'var(--input-border-hover)',
				'status-success': 'var(--status-success-border)',
				'status-warning': 'var(--status-warning-border)',
				'status-error': 'var(--status-error-border)',
				'status-info': 'var(--status-info-border)',
				'selection': 'var(--selection-ring)'
			},
			textColor: {
				'placeholder': 'var(--input-placeholder)'
			}
		}
	},
	plugins: []
};
