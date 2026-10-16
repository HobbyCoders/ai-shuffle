import { describe, expect, it } from "vitest";
import { watchCaptcha, type CaptchaProbe } from "../src/captcha.js";

/** A probe that answers from a script, and a sleep that only counts. */
function scripted(states: CaptchaProbe[]) {
  let calls = 0;
  let slept = 0;
  const probe = async () => states[Math.min(calls++, states.length - 1)];
  const sleep = async (ms: number) => { slept += ms; };
  return { probe, sleep, calls: () => calls, slept: () => slept };
}

describe("watchCaptcha", () => {
  it("answers at once for a page with no captcha", async () => {
    const s = scripted(["none"]);
    expect(await watchCaptcha(s.probe, { sleep: s.sleep })).toBe("none");
    expect(s.calls()).toBe(1);
    expect(s.slept()).toBe(0);
  });

  it("answers at once when the challenge frame is there", async () => {
    const s = scripted(["rendered"]);
    expect(await watchCaptcha(s.probe, { sleep: s.sleep })).toBe("rendered");
    expect(s.slept()).toBe(0);
  });

  it("gives an empty captcha the grace period to load before calling it withheld", async () => {
    const s = scripted(["empty"]);
    expect(await watchCaptcha(s.probe, { sleep: s.sleep, graceMs: 2000, pollMs: 500 })).toBe("withheld");
    expect(s.slept()).toBe(2000);
  });

  it("a widget that loads within the grace period is not withheld", async () => {
    const s = scripted(["empty", "empty", "rendered"]);
    expect(await watchCaptcha(s.probe, { sleep: s.sleep, graceMs: 4000, pollMs: 500 })).toBe("rendered");
    expect(s.slept()).toBe(1000);
  });

  it("lookMs keeps looking for a captcha dialog that opens late", async () => {
    const s = scripted(["none", "none", "empty"]);
    expect(await watchCaptcha(s.probe, { sleep: s.sleep, lookMs: 3000, graceMs: 1000, pollMs: 500 })).toBe("withheld");
  });
});
