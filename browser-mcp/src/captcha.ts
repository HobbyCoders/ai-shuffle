/**
 * Withheld-captcha detection.
 *
 * Some sites decide server-side not to send their captcha widget to an
 * automated or unrecognised browser. The page still shows its "Captcha
 * required" dialog, but its body stays empty: no challenge frame is ever
 * added, so neither the agent nor a person watching through the Browser panel
 * can solve it. Nothing in this runtime blocks the widgets (the reCAPTCHA demo
 * renders and passes here); the site withholds them.
 *
 * Detection is deliberately narrow: a visible captcha container (a widget
 * placeholder such as `.g-recaptcha`, or a dialog that says captcha) holding
 * no frame, image, canvas or input, while the page has no challenge frame at
 * all, and still so after a grace period for the widget to load.
 */

import type { Page } from "playwright-core";

/** What the panel shows, verbatim. */
export const CAPTCHA_PANEL_NOTICE =
  "This step needs your own browser. The site asked for a captcha but did not send it to this browser, so it cannot be solved here.";

/** What the agent reads in navigate / act / snapshot results. */
export const CAPTCHA_AGENT_NOTE =
  "This step needs your own browser: the page asks for a captcha, but the site never sent its captcha widget to this browser, " +
  "so it cannot be solved here, by you or by the user through the Browser panel. Retrying will not help. " +
  "Tell the user to finish this step in their own browser, then continue.";

/** `none`: no captcha on the page; `rendered`: a challenge frame is there; `empty`: a captcha container with nothing in it. */
export type CaptchaProbe = "none" | "rendered" | "empty";
export type CaptchaVerdict = "none" | "rendered" | "withheld";

/** How long an empty captcha gets to load its widget before it counts as withheld. */
export const CAPTCHA_GRACE_MS = 4000;
const POLL_MS = 500;

/**
 * Runs IN THE PAGE (passed to page.evaluate), so it must stay self-contained.
 * Exported for the browser test, which evaluates it against fixture pages.
 */
export function probeCaptchaInPage(): CaptchaProbe {
  const FRAME_RE =
    /recaptcha\/(api2|enterprise)\/|hcaptcha\.com|challenges\.cloudflare\.com|arkoselabs\.com|funcaptcha\.com|captcha-delivery\.com/i;
  const frames = Array.from(document.querySelectorAll("iframe"));
  if (frames.some((f) => FRAME_RE.test(f.getAttribute("src") || ""))) return "rendered";

  const visible = (el: Element): boolean => {
    const box = el.getBoundingClientRect();
    if (box.width <= 0 || box.height <= 0) return false;
    const style = getComputedStyle(el);
    return style.visibility !== "hidden" && style.display !== "none" && style.opacity !== "0";
  };
  // A widget, or a hand-made challenge (an image and a text box), is content.
  const hasContent = (el: Element): boolean =>
    !!el.querySelector("iframe, img, canvas, svg image, input:not([type=hidden]), textarea");

  const widgets = document.querySelectorAll(
    ".g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey], #g-recaptcha, #recaptcha",
  );
  for (const el of Array.from(widgets)) {
    if (visible(el) && !hasContent(el)) return "empty";
  }

  const TEXT_RE = /captcha|verify (that )?you('| a)re (a )?human|i'm not a robot/i;
  const dialogs = document.querySelectorAll('[role="dialog"], [role="alertdialog"], dialog[open]');
  for (const el of Array.from(dialogs)) {
    if (!visible(el)) continue;
    const words = ((el as HTMLElement).innerText || el.textContent || "").slice(0, 2000);
    if (TEXT_RE.test(words) && !hasContent(el)) return "empty";
  }
  return "none";
}

/**
 * Probe, and give an empty captcha {@link CAPTCHA_GRACE_MS} to load its
 * widget. Only the empty case waits; a page without a captcha answers at once.
 */
export async function watchCaptcha(
  probe: () => Promise<CaptchaProbe>,
  opts: {
    graceMs?: number;
    pollMs?: number;
    /** Keep looking this long for a captcha that has not appeared yet (a dialog opened by a request). */
    lookMs?: number;
    sleep?: (ms: number) => Promise<void>;
  } = {},
): Promise<CaptchaVerdict> {
  const graceMs = opts.graceMs ?? CAPTCHA_GRACE_MS;
  const pollMs = opts.pollMs ?? POLL_MS;
  const lookMs = opts.lookMs ?? 0;
  const sleep = opts.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
  let state = await probe();
  for (let looked = 0; state === "none" && looked < lookMs; looked += pollMs) {
    await sleep(pollMs);
    state = await probe();
  }
  for (let waited = 0; state === "empty" && waited < graceMs; waited += pollMs) {
    await sleep(pollMs);
    state = await probe();
  }
  return state === "empty" ? "withheld" : state;
}

/** {@link watchCaptcha} against a live page; a probe that throws (navigation, closed tab) reads as no captcha. */
export function watchPageCaptcha(page: Page, opts?: Parameters<typeof watchCaptcha>[1]): Promise<CaptchaVerdict> {
  return watchCaptcha(
    () => Promise.resolve()
      .then(() => page.evaluate(probeCaptchaInPage))
      .then((state) => (state === "rendered" || state === "empty" ? state : "none"))
      .catch((): CaptchaProbe => "none"),
    opts,
  );
}
