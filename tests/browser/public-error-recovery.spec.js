const { test, expect } = require('@playwright/test');
const { installFailureDiagnostics, attachFailureDiagnostics } = require('./helpers');

test.beforeEach(async ({ page }) => installFailureDiagnostics(page));
test.afterEach(async ({ page }, info) => attachFailureDiagnostics(page, info));

for (const width of [390, 1440]) {
  test(`public and workspace 404 recovery use the right layout at ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 900 });
    const runtimeErrors = [];
    page.on('pageerror', error => runtimeErrors.push(error.message));
    let response = await page.goto('/features/missing-page');
    expect(response.status()).toBe(404);
    await expect(page).toHaveTitle(/Page not found.*Twinevia/);
    await expect(page.locator('body')).toHaveClass(/marketing-page/);
    await expect(page.locator('[data-public-not-found]')).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Page not found', exact: true })).toBeVisible();
    await expect(page.locator('[data-public-not-found]').getByRole('link', { name: 'Sign in', exact: true })).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: info.outputPath(`public-404-${width}.png`), fullPage: false });
    await page.getByRole('link', { name: 'Back to home', exact: true }).click();
    await expect(page.getByRole('heading', { name: /Reach them/i })).toBeVisible();

    response = await page.goto('/missing-workspace-page');
    expect(response.status()).toBe(404);
    await expect(page.locator('body')).toHaveClass(/auth-body/);
    await expect(page.locator('[data-public-not-found]')).toHaveCount(0);
    await expect(page.getByRole('heading', { name: 'Page not found', exact: true })).toBeVisible();
    await page.locator('.workspace-panel').getByRole('link', { name: 'Sign in', exact: true }).click();
    await expect(page.getByLabel('Email or username')).toBeVisible();
    expect(runtimeErrors).toEqual([]);
  });
}
