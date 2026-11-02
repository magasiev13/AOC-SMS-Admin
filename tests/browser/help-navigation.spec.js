const { test, expect } = require('@playwright/test');
const { login, installFailureDiagnostics, attachFailureDiagnostics } = require('./helpers');

test.beforeEach(async ({ page }) => installFailureDiagnostics(page));
test.afterEach(async ({ page }, info) => attachFailureDiagnostics(page, info));

async function assertFits(page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
}

for (const width of [1440, 390]) {
  test(`contextual guidance opens the right topic and help search recovers at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    await login(page, 'owner@browser.test', 'Owner-pass1!');
    await page.goto('/settings/event-sync?provider=app');
    const guidance = page.locator('[data-context-help]');
    await guidance.locator('summary').focus();
    await page.keyboard.press('Enter');
    await expect(guidance).toHaveAttribute('open', '');
    await expect(guidance).toContainText('optional and usually needs a developer');
    await guidance.getByRole('link', { name: /Read the App Integrations Guide/ }).click();
    await expect(page).toHaveURL(/\/help#settings$/);
    await expect(page.locator('#settings')).toHaveAttribute('open', '');
    await expect(page.locator('#settings')).toContainText('App Integrations is optional');
    await expect(page.locator('main')).not.toContainText('built-in method');
    await page.getByRole('searchbox', { name: 'Search Help' }).fill('scheduling');
    await page.getByRole('button', { name: 'Search', exact: true }).click();
    await expect(page.locator('[data-help-topic]')).not.toHaveCount(0);
    await expect(page.locator('[data-help-topic]').first()).toHaveAttribute('open', '');
    await page.getByRole('searchbox', { name: 'Search Help' }).fill('no-help-topic-matches-this');
    await page.getByRole('button', { name: 'Search', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'No Matching Help Topics' })).toBeVisible();
    await page.getByRole('link', { name: 'Show All Topics' }).click();
    await expect(page.getByRole('heading', { name: 'What Would You Like to Do?' })).toBeVisible();
    await expect(page.locator('[data-help-topic]')).toHaveCount(8);
    await assertFits(page);
  });

  test(`navigation identifies one location and mobile menu can reach Help at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    await login(page, 'owner@browser.test', 'Owner-pass1!');
    await page.goto('/settings/event-sync?provider=app');
    const navigation = width === 390 ? page.locator('#appMobileNav') : page.locator('.app-sidebar');
    if (width === 390) {
      await page.getByRole('button', { name: 'Toggle navigation' }).click();
      await expect(page.getByRole('button', { name: 'Toggle navigation' })).toHaveAttribute('aria-expanded', 'true');
    }
    await expect(navigation.locator('.app-nav-link[aria-current="page"]')).toHaveText('Settings');
    await navigation.getByRole('link', { name: 'Help & Getting Started', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Help & Getting Started', exact: true })).toBeVisible();
    if (width === 390) await page.getByRole('button', { name: 'Toggle navigation' }).click();
    const current = navigation.locator('.app-nav-link[aria-current="page"]');
    await expect(current).toHaveCount(1);
    await expect(current).toHaveText('Help & Getting Started');
    if (width === 390) {
      const size = await current.boundingBox();
      expect(size.height).toBeGreaterThanOrEqual(44);
      await page.keyboard.press('Escape');
      await expect(page.getByRole('button', { name: 'Toggle navigation' })).toHaveAttribute('aria-expanded', 'false');
    }
    await assertFits(page);
  });

  test(`message review shows progress and keeps the draft when editing at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    await login(page, 'owner@browser.test', 'Owner-pass1!');
    await expect(page.getByRole('heading', { name: 'Send a Message', exact: true })).toBeVisible();
    const draft = `Review without sending ${width}`;
    await page.getByLabel('Message', { exact: true }).fill(draft);
    await expect(page.locator('#audienceEligibility')).toContainText('eligible recipient(s)');
    let resumeReview;
    const waitForReview = new Promise(resolve => { resumeReview = resolve; });
    await page.route('**/dashboard/review', async route => {
      await waitForReview;
      await route.continue();
    });
    const reviewButton = page.getByRole('button', { name: 'Review Message', exact: true });
    await reviewButton.click();
    await expect(page.getByRole('button', { name: 'Preparing Review…' })).toBeDisabled();
    await expect(page.locator('#sendBtn')).toHaveAttribute('aria-busy', 'true');
    resumeReview();
    const review = page.getByRole('dialog', { name: 'Review Message', exact: true });
    await expect(review).toBeVisible();
    await expect(review.getByRole('button', { name: 'Confirm Send', exact: true })).toBeVisible();
    await expect(review.locator('[data-review="eligible"]')).not.toHaveText('0');
    await review.getByRole('button', { name: 'Edit Message', exact: true }).click();
    await expect(review).not.toBeVisible();
    await expect(page.getByLabel('Message', { exact: true })).toHaveValue(draft);
    await expect(reviewButton).toBeFocused();
    await assertFits(page);
  });
}
