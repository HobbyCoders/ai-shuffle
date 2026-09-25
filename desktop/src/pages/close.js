// Evaluated by the desktop main process in the app page (or bundled fallback).
// Keep this aligned with ConfirmDialog.svelte: inherit the user's live theme tokens.
(async function showCloseDialog({ activeCount = null, error = false, offerForce = false } = {}) {
    const previousFocus = document.activeElement;
    const style = document.createElement('style');
    style.textContent = `
      .shuffle-close-dialog { --quit-card: #151819; --quit-fg: #f2f4f4; --quit-muted: #a1a9ac; --quit-border: #ffffff18; --quit-btn: #23282a;
        box-sizing: border-box; width: min(460px, calc(100vw - 32px)); max-height: calc(100vh - 32px); overflow: auto;
        margin: auto; padding: 28px; border: 1px solid var(--border, var(--quit-border)); border-radius: 16px;
        background: var(--card, var(--quit-card)); color: var(--foreground, var(--quit-fg));
        font: 14px/1.5 var(--font-sans, 'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif);
        box-shadow: 0 24px 64px #00000040; animation: shuffle-close-in 160ms ease-out; }
      .shuffle-close-dialog::backdrop { background: #0009; backdrop-filter: blur(4px); }
      .shuffle-close-dialog * { box-sizing: border-box; }
      .shuffle-close-dialog .quit-icon { width: 40px; height: 40px; display: grid; place-items: center; border-radius: 12px;
        background: color-mix(in srgb, var(--warning, #d6aa55) 12%, transparent); color: var(--warning, #d6aa55); margin-bottom: 18px; }
      .shuffle-close-dialog h2 { margin: 0; color: inherit; font-size: 18px; line-height: 1.4; font-weight: 600; letter-spacing: -.3px; }
      .shuffle-close-dialog p { margin: 10px 0 0; color: var(--muted-foreground, var(--quit-muted)); font-size: 14px; line-height: 1.6; }
      .shuffle-close-dialog .quit-actions { display: flex; justify-content: flex-end; gap: 12px; margin-top: 24px; }
      .shuffle-close-dialog button { cursor: pointer; font: inherit; font-weight: 500; line-height: 1.4; padding: 10px 16px; border-radius: 12px;
        border: 1px solid var(--border, var(--quit-border)); background: var(--muted, var(--quit-btn)); color: var(--foreground, var(--quit-fg)); }
      .shuffle-close-dialog button:hover { filter: brightness(.94); }
      .shuffle-close-dialog button:focus-visible { outline: 2px solid var(--ring, #00c4ad); outline-offset: 3px; }
      .shuffle-close-dialog .quit-confirm { background: var(--destructive, #e57777); color: var(--destructive-foreground, #1e0c0c); border-color: transparent; }
      @media (prefers-color-scheme: light) { .shuffle-close-dialog { --quit-card: #fff; --quit-fg: #1d1d1f; --quit-muted: #646b70; --quit-border: #0000001a; --quit-btn: #f2f3f4; } }
      @media (max-width: 430px) { .shuffle-close-dialog { padding: 24px; } .shuffle-close-dialog .quit-actions { flex-direction: column-reverse; } }
      @keyframes shuffle-close-in { from { opacity: 0; transform: translateY(8px) scale(.97); } to { opacity: 1; transform: none; } }
      @media (prefers-reduced-motion: reduce) { .shuffle-close-dialog { animation: none; } }
    `;
    const dialog = document.createElement('dialog');
    dialog.className = 'shuffle-close-dialog';
    dialog.setAttribute('role', 'alertdialog');
    dialog.setAttribute('aria-labelledby', 'shuffle-close-title');
    dialog.setAttribute('aria-describedby', 'shuffle-close-description');
    dialog.innerHTML = `<div class="quit-icon" aria-hidden="true"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 2.5 17.4A2 2 0 0 0 4.2 20h15.6a2 2 0 0 0 1.7-2.6L13.7 3.9a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4m0 3h.01"/></svg></div>
      <h2 id="shuffle-close-title"></h2><p id="shuffle-close-description"></p>
      <div class="quit-actions"><button type="button" class="quit-cancel" autofocus>Keep Open</button><button type="button" class="quit-confirm">Stop Sessions and Close</button></div>`;
    const title = error ? 'Sessions are still stopping' : 'Close AI Shuffle?';
    const activity = activeCount === null ? 'Unable to check for active sessions.' :
        `You have ${activeCount} active session${activeCount === 1 ? '' : 's'}.`;
    dialog.querySelector('h2').textContent = title;
    dialog.querySelector('p').textContent = !error
        ? `${activity} Closing the app will stop ${activeCount === 1 ? "it" : "any active sessions"}. Are you sure you want to close?`
        : offerForce
            ? 'AI Shuffle still could not confirm that every session has stopped. The worker may be stuck. Quit Anyway ends it now; sessions that were running may be interrupted.'
            : 'AI Shuffle could not confirm that every session has stopped. The app will stay open. Wait a moment, then try closing again.';
    const cancel = dialog.querySelector('.quit-cancel');
    const confirm = dialog.querySelector('.quit-confirm');
    // A first failure offers no destructive action; a repeated one offers to quit anyway.
    if (error && !offerForce) confirm.remove();
    else if (error) confirm.textContent = 'Quit Anyway';
    document.head.append(style);
    document.body.append(dialog);
    try {
        return await new Promise(resolve => {
            dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), { once: true });
            cancel.addEventListener('click', () => dialog.close('cancel'));
            confirm.addEventListener('click', () => dialog.close('confirm'));
            // Keep app-wide shortcuts from interpreting Enter as another confirmation.
            dialog.addEventListener('keydown', event => event.stopPropagation());
            dialog.addEventListener('click', event => {
                if (event.target !== dialog) return;
                const rect = dialog.getBoundingClientRect();
                if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close('cancel');
            });
            dialog.showModal();
            cancel.focus();
        });
    } finally {
        dialog.remove();
        style.remove();
        if (previousFocus?.isConnected && typeof previousFocus.focus === 'function') previousFocus.focus();
    }
})
