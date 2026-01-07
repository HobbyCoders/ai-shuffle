import { test, expect } from '@playwright/test';

// Use port 5174 since 5173 is in use
const BASE_URL = 'http://localhost:5174';

test.describe('Session Switching Test', () => {
  test.setTimeout(300000); // 5 minutes timeout for this test

  test('test session switching between two sessions', async ({ page }) => {
    // Go to the app
    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');

    // Take initial screenshot
    await page.screenshot({ path: 'tests/screenshots/01-initial.png', fullPage: true });

    // Check if we need to login
    const currentUrl = page.url();
    console.log('Current URL:', currentUrl);

    if (currentUrl.includes('/setup')) {
      console.log('Setting up admin account...');
      await page.fill('input[placeholder="admin"]', 'admin');
      await page.fill('input[placeholder="Enter password"]', 'password123');
      await page.fill('input[placeholder="Confirm password"]', 'password123');
      await page.click('button[type="submit"]');
      await page.waitForURL('**/');
      await page.waitForLoadState('networkidle');
    } else if (currentUrl.includes('/login')) {
      console.log('Logging in...');
      await page.fill('input[placeholder="Username"]', 'admin');
      await page.fill('input[placeholder="Password"]', 'password123');
      await page.click('button[type="submit"]');
      await page.waitForURL('**/');
      await page.waitForLoadState('networkidle');
    }

    // Now we should be on the main chat page
    await page.screenshot({ path: 'tests/screenshots/02-main-page.png', fullPage: true });

    const textarea = page.locator('textarea[placeholder="Type a message..."]');

    // ==========================================
    // Create first chat session with unique identifier
    // ==========================================
    console.log('Creating FIRST chat session (APPLE)...');

    await textarea.fill('Hello! This is the APPLE session. Remember: APPLE = 111');
    await page.click('button:has-text("Send")');

    // Wait for response
    console.log('Waiting for first response...');
    await page.waitForSelector('button:has-text("Send")', { timeout: 120000 });
    await page.waitForTimeout(3000);

    await page.screenshot({ path: 'tests/screenshots/03-first-session-created.png', fullPage: true });

    // ==========================================
    // Create second chat session with different identifier
    // ==========================================
    console.log('Creating SECOND chat session (BANANA)...');

    // Click New Chat
    await page.click('button:has-text("New Chat")');
    await page.waitForTimeout(1000);

    await textarea.fill('Hello! This is the BANANA session. Remember: BANANA = 222');
    await page.click('button:has-text("Send")');

    // Wait for response
    console.log('Waiting for second response...');
    await page.waitForSelector('button:has-text("Send")', { timeout: 120000 });
    await page.waitForTimeout(3000);

    await page.screenshot({ path: 'tests/screenshots/04-second-session-created.png', fullPage: true });

    // Now we have 2 sessions in the sidebar
    // Sessions are ordered by updated_at DESC, so most recently updated is at top
    // We need to identify sessions by their content, not position

    // ==========================================
    // Resume FIRST session (APPLE) - find by content
    // ==========================================
    console.log('Resuming FIRST session (APPLE) - finding by content...');

    const sessionButtons = page.locator('aside .space-y-1 > button');
    const sessionCount = await sessionButtons.count();
    console.log(`Found ${sessionCount} sessions in sidebar`);

    // Find session containing APPLE by looking at session titles/content
    // The session title is derived from the first message, so APPLE session should contain "APPLE"
    let appleSessionButton = page.locator('aside .space-y-1 > button:has-text("APPLE")');
    let appleButtonCount = await appleSessionButton.count();
    console.log(`Found ${appleButtonCount} buttons with APPLE text`);

    // If no button found with APPLE text, fall back to second button (index 1)
    if (appleButtonCount === 0) {
      console.log('No APPLE button found by text, using index 1');
      appleSessionButton = sessionButtons.nth(1);
    }

    await appleSessionButton.first().click();
    await page.waitForTimeout(2000);

    await page.screenshot({ path: 'tests/screenshots/05-switched-to-apple.png', fullPage: true });

    // Verify we're in APPLE session by sending follow-up
    await textarea.fill('What fruit word did I tell you to remember?');
    await page.click('button:has-text("Send")');

    console.log('Waiting for APPLE session response...');
    await page.waitForSelector('button:has-text("Send")', { timeout: 120000 });
    await page.waitForTimeout(3000);

    await page.screenshot({ path: 'tests/screenshots/06-apple-followup.png', fullPage: true });

    // Get the response text to verify
    const appleResponse = await page.locator('.prose').last().textContent();
    console.log('APPLE session response:', appleResponse);

    // ==========================================
    // NOW switch to SECOND session (BANANA) - find by content
    // After sending message in APPLE, the list will have reordered (APPLE at top)
    // ==========================================
    console.log('NOW switching to SECOND session (BANANA) - finding by content...');

    // Find session containing BANANA
    let bananaSessionButton = page.locator('aside .space-y-1 > button:has-text("BANANA")');
    let bananaButtonCount = await bananaSessionButton.count();
    console.log(`Found ${bananaButtonCount} buttons with BANANA text`);

    // If no button found with BANANA text, we need to find the OTHER session
    if (bananaButtonCount === 0) {
      console.log('No BANANA button found by text, looking for non-APPLE session');
      // Get all session buttons and find the one that's NOT the current session
      const allButtons = page.locator('aside .space-y-1 > button');
      const count = await allButtons.count();
      for (let i = 0; i < count; i++) {
        const btn = allButtons.nth(i);
        const isSelected = await btn.evaluate(el => el.classList.contains('bg-[var(--color-surface-hover)]'));
        if (!isSelected) {
          bananaSessionButton = btn;
          break;
        }
      }
    }

    await bananaSessionButton.first().click();
    await page.waitForTimeout(2000);

    await page.screenshot({ path: 'tests/screenshots/07-switched-to-banana.png', fullPage: true });

    // Send message to BANANA session
    await textarea.fill('What fruit word did I tell you to remember in THIS session?');

    console.log('Sending message to BANANA session...');
    await page.click('button:has-text("Send")');

    // This is where the timeout error should occur if the bug exists
    try {
      console.log('Waiting for BANANA session response (this is where bug occurs)...');
      await page.waitForSelector('button:has-text("Send")', { timeout: 120000 });
      await page.waitForTimeout(3000);

      await page.screenshot({ path: 'tests/screenshots/08-banana-followup-SUCCESS.png', fullPage: true });

      // Get the response text
      const bananaResponse = await page.locator('.prose').last().textContent();
      console.log('BANANA session response:', bananaResponse);

      // Verify the response mentions BANANA, not APPLE
      if (bananaResponse && bananaResponse.toLowerCase().includes('banana')) {
        console.log('SUCCESS: Session switching works correctly!');
      } else if (bananaResponse && bananaResponse.toLowerCase().includes('apple')) {
        console.log('BUG: Wrong session! Got APPLE instead of BANANA');
        throw new Error('Session switching bug: returned wrong session context');
      }

    } catch (error) {
      await page.screenshot({ path: 'tests/screenshots/08-banana-ERROR.png', fullPage: true });
      console.log('ERROR during BANANA session:', error);

      // Check for error message in UI
      const errorText = await page.locator('.text-red-300').textContent().catch(() => null);
      if (errorText) {
        console.log('UI Error message:', errorText);
      }

      throw error;
    }

    // Final screenshot
    await page.screenshot({ path: 'tests/screenshots/09-final.png', fullPage: true });
  });
});
