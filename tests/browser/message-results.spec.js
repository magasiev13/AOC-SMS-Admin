const { test, expect } = require('@playwright/test');
const os = require('node:os');
const path = require('node:path');
const { login, installFailureDiagnostics, attachFailureDiagnostics } = require('./helpers');

const counts = { accepted: 273, not_submitted: 3, delivered: 270, not_delivered: 2, no_receipt: 1 };

test.beforeEach(async ({ page }) => installFailureDiagnostics(page));
test.afterEach(async ({ page }, testInfo) => attachFailureDiagnostics(page, testInfo));

async function openResults(page, role = 'owner') {
  await login(page, `results-${role}@browser.test`, role === 'owner' ? 'ResultsOwner-pass1!' : 'ResultsStaff-pass1!');
  await page.goto('/logs');
  await page.getByRole('link', { name: /^(View|View log details)$/ }).first().click();
  await expect(page).toHaveURL(/\/logs\/\d+$/);
  await expect(page.getByRole('heading', { name: 'Message Details', exact: true })).toBeVisible();
}

test('390px results show all five outcomes and the first delivery failure without scrolling', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await openResults(page);
  expect(await page.evaluate(() => window.scrollY)).toBe(0);
  for (const [key, value] of Object.entries(counts)) {
    const stat = page.locator(`.message-results-summary [data-result="${key}"]`);
    await expect(stat.locator('.message-result-value')).toHaveText(String(value));
    const box = await stat.boundingBox();
    expect(box.y).toBeGreaterThanOrEqual(0);
    expect(box.y + box.height).toBeLessThanOrEqual(844);
  }
  const first = page.locator('.message-recipient-card').first();
  await expect(first).toHaveAttribute('data-result-category', 'not_delivered');
  const name = first.getByRole('heading', { name: 'Delivery attention recipient', exact: true });
  const box = await name.boundingBox();
  expect(box.y + box.height).toBeLessThanOrEqual(844);
  await expect(first).toContainText('Unreachable destination handset (30003)');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(os.tmpdir(), 'twinevia-phase1-message-results-390.png'), fullPage: false });
});

test('1440px results fit the Error column and retain filters and fifty-row pages', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await openResults(page);
  const table = page.locator('#messageRecipientsTable');
  await expect(table.locator('tbody tr')).toHaveCount(50);
  const errorHeader = table.locator('thead th').last();
  await expect(errorHeader).toContainText('Error');
  const tableBox = await table.boundingBox();
  const headerBox = await errorHeader.boundingBox();
  const errorCellBox = await table.locator('tbody tr').first().locator('td').last().boundingBox();
  expect(tableBox.x + tableBox.width).toBeLessThanOrEqual(1440);
  expect(headerBox.x + headerBox.width).toBeLessThanOrEqual(tableBox.x + tableBox.width + 1);
  expect(errorCellBox.x + errorCellBox.width).toBeLessThanOrEqual(tableBox.x + tableBox.width + 1);
  await page.screenshot({ path: path.join(os.tmpdir(), 'twinevia-phase1-message-results-1440.png'), fullPage: false });

  const filters = page.getByRole('navigation', { name: 'Recipient filters', exact: true });
  for (const [label, expected] of [['Not delivered', 2], ['No receipt yet', 1], ['Not submitted', 3]]) {
    await filters.getByRole('link', { name: label, exact: true }).click();
    await expect(table.locator('tbody tr')).toHaveCount(expected);
  }
  await filters.getByRole('link', { name: 'All', exact: true }).click();
  await page.getByRole('navigation', { name: 'Recipient Pagination', exact: true }).getByRole('link', { name: 'Next', exact: true }).click();
  await expect(page).toHaveURL(/page=2/);
  await expect(table.locator('tbody tr')).toHaveCount(50);
});

test('staff result pages retain outcomes and omit recipient phone data from HTML', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await openResults(page, 'staff');
  const html = await page.content();
  for (let index = 0; index < 276; index++) {
    const syntheticPhone = `+1555001${String(index).padStart(4, '0')}`;
    expect(html.includes(syntheticPhone)).toBe(false);
    expect(html.includes(syntheticPhone.slice(1))).toBe(false);
  }
  for (const [key, value] of Object.entries(counts)) {
    await expect(page.locator(`.message-results-summary [data-result="${key}"] .message-result-value`)).toHaveText(String(value));
  }
  await expect(page.locator('.message-recipient-card').first()).toHaveAttribute('data-result-category', 'not_delivered');
});
