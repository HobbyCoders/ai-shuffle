// Plaid's SDK runs only in the dedicated connection window, never in chat.
export type LinkHandler = { open: () => void; destroy: () => void };
type LinkOptions = { token: string; receivedRedirectUri?: string; onSuccess: (token: string) => void; onExit: (error: { display_message?: string } | null) => void };
declare global { interface Window { Plaid?: { create: (options: LinkOptions) => LinkHandler } } }
let loading: Promise<void> | undefined;
export function loadPlaid(): Promise<void> {
    if (window.Plaid) return Promise.resolve();
    if (loading) return loading;
    loading = new Promise<void>((resolve, reject) => {
        const script = document.createElement('script');
        script.src = 'https://cdn.plaid.com/link/v2/stable/link-initialize.js';
        script.onload = () => window.Plaid ? resolve() : reject(new Error('Plaid Link did not load.'));
        script.onerror = () => { script.remove(); loading = undefined; reject(new Error('Could not load Plaid Link. Check network access.')); };
        document.head.appendChild(script);
    });
    return loading;
}
