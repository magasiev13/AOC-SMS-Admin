const { test, expect } = require('@playwright/test');
const { login, confirmSendReview, installFailureDiagnostics, attachFailureDiagnostics } = require('./helpers');

test('local assets support mobile navigation, charts and a reviewed fake send with external hosts blocked', async ({ page, baseURL }, testInfo) => {
  installFailureDiagnostics(page);
  const externalAttempts = [];
  const runtimeErrors = [];
  const failedAssets = [];
  const appOrigin = new URL(baseURL).origin;
  page.on('pageerror', error => runtimeErrors.push(error.message));
  page.on('console', message => {
    if (message.type() === 'error') runtimeErrors.push(message.text());
  });
  page.on('response', response => {
    if (response.url().includes('/static/') && !response.ok()) failedAssets.push(response.status());
  });
  await page.route('**/*', async route => {
    if (new URL(route.request().url()).origin !== appOrigin) {
      externalAttempts.push(new URL(route.request().url()).origin);
      await route.abort('blockedbyclient');
      return;
    }
    await route.continue();
  });
  await page.setViewportSize({ width: 390, height: 844 });
  try {
    await login(page, 'owner@browser.test', 'Owner-pass1!');
    await page.goto('/dashboard');
    await expect(page).toHaveTitle(/Twinevia/);
    await expect(page.locator('#sendForm')).toBeVisible();
    const navigation = page.getByRole('button', { name: 'Toggle navigation', exact: true });
    await navigation.click();
    await expect(navigation).toHaveAttribute('aria-expanded', 'true');
    await expect(page.locator('#appMobileNav')).toBeVisible();
    await expect(page.locator('#appMobileNav')).toHaveClass(/\bshow\b/);
    await navigation.click();
    await expect(navigation).toHaveAttribute('aria-expanded', 'false');
    await expect.poll(() => page.evaluate(() => Boolean(window.Chart?.getChart('audienceChart')))).toBe(true);
    await page.locator('#audienceChart').scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath('local-assets-mobile-chart.png'), fullPage: false });

    await page.locator('.advanced-options__summary').click();
    await page.getByLabel('Test Mode', { exact: true }).check();
    await page.locator('#testRecipientPhone').selectOption({ index: 1 });
    await page.getByLabel('Message', { exact: true }).fill('Local asset browser verification');
    await page.getByRole('button', { name: 'Review Message', exact: true }).click();
    await expect(page.getByRole('dialog', { name: 'Review Message', exact: true })).toBeVisible();
    await confirmSendReview(page);
    await expect(page).toHaveURL(/\/logs\/\d+$/);
    await expect(page.locator('body')).toContainText('Local asset browser verification');
    await expect.poll(async () => {
      await page.reload();
      return page.locator('body').innerText();
    }).toContain('was submitted to the provider. This is not a delivery receipt.');
    expect(externalAttempts).toEqual([]);
    expect(runtimeErrors).toEqual([]);
    expect(failedAssets).toEqual([]);
  } finally {
    await attachFailureDiagnostics(page, testInfo);
  }
});
