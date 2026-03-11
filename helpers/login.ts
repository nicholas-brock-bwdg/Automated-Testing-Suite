import type { Page } from '@playwright/test';

/**
 * Log in to the Ignition gateway and navigate to targetUrl.
 *
 * Credentials are read from env vars set in .env.test:
 *   IGNITION_TEST_USER
 *   IGNITION_TEST_PASSWORD
 */
export async function login(page: Page, targetUrl: string): Promise<void> {
  const user = process.env.IGNITION_TEST_USER;
  const pass = process.env.IGNITION_TEST_PASSWORD;

  if (!user || !pass) {
    throw new Error(
      'IGNITION_TEST_USER and IGNITION_TEST_PASSWORD must be set in .env.test'
    );
  }

  // Navigate to the view — Ignition will redirect to login if unauthenticated
  await page.goto(targetUrl, { timeout: 15000 });

  // Wait for the password field — present on both HTTP-redirect and SPA login pages
  await page.waitForSelector('input[type="password"]', { timeout: 10000 });

  // Fill username — try progressively broader selectors until one hits
  const usernameSelectors = [
    'input[autocomplete="username"]',
    'input[name*="user" i]',
    'input[id*="user" i]',
    'input[placeholder*="user" i]',
    'input[type="text"]',
  ];
  for (const sel of usernameSelectors) {
    if ((await page.locator(sel).count()) > 0) {
      await page.fill(sel, user);
      break;
    }
  }

  await page.fill('input[type="password"]', pass);

  // Submit — Ignition's button text varies by version/theme
  await page.click(
    'button[type="submit"], [type="submit"], button:has-text("Login"), button:has-text("Sign In")'
  );

  // Wait for navigation away from the login page
  await page
    .waitForURL(url => !url.toString().match(/login|auth|status=401|status=403/i), {
      timeout: 15000,
    })
    .catch(() => {
      // If URL didn't change, the login may have failed — test will catch it
    });

  // Navigate to the target view if we ended up elsewhere (e.g. gateway home)
  if (page.url() !== targetUrl) {
    await page.goto(targetUrl, { timeout: 15000 });
  }
}
