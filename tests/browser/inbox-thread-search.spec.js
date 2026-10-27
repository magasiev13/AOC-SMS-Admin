const { test, expect } = require('@playwright/test');
const { login, installFailureDiagnostics, attachFailureDiagnostics } = require('./helpers');

test('mobile back-to-threads search and refresh retain the conversation list', async ({ page }, testInfo) => {
  installFailureDiagnostics(page);
  try {
    await page.setViewportSize({ width: 390, height: 844 });
    await login(page, 'owner@browser.test', 'Owner-pass1!');
    await page.goto('/inbox');
    await expect(page.getByLabel('Send reply')).toBeVisible();
    await page.getByRole('link', { name: 'Back to threads', exact: true }).click();
    const search = page.getByRole('textbox', { name: 'Search inbox threads' });
    await expect(search).toBeVisible();
    await search.fill('Maya');
    await search.press('Enter');
    await expect(page).toHaveURL(/search=Maya/);
    expect(new URL(page.url()).searchParams.get('view')).toBe('threads');
    await expect(search).toBeVisible();
    await expect(page.getByLabel('Send reply')).toHaveCount(0);
    await expect(page.locator('#inboxThreadList')).toContainText('Maya');
    await page.reload();
    await expect(search).toHaveValue('Maya');
    await expect(page.getByLabel('Send reply')).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
    await page.screenshot({ path: testInfo.outputPath('mobile-thread-search.png'), fullPage: false });
    await search.fill('');
    await search.press('Enter');
    expect(new URL(page.url()).searchParams.get('view')).toBe('threads');
    await expect(search).toBeVisible();
    await expect(page.getByLabel('Send reply')).toHaveCount(0);
    await page.locator('#inboxThreadList a[href*="thread="]').first().click();
    await expect(page.getByLabel('Send reply')).toBeVisible();
  } finally {
    await attachFailureDiagnostics(page, testInfo);
  }
});
