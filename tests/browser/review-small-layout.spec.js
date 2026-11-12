const { test, expect } = require('@playwright/test');
const os = require('node:os');
const path = require('node:path');
const { login, installFailureDiagnostics, attachFailureDiagnostics } = require('./helpers');

test.beforeEach(async ({ page }) => installFailureDiagnostics(page));
test.afterEach(async ({ page }, testInfo) => attachFailureDiagnostics(page, testInfo));

async function expectFilledRows(grid, expectedCount, expectedRows) {
  await expect(grid.locator('.workspace-summary__stat')).toHaveCount(expectedCount);
  const rows = await grid.evaluate(element => {
    const parent = element.getBoundingClientRect();
    const grouped = new Map();
    for (const child of element.children) {
      const box = child.getBoundingClientRect();
      const y = Math.round(box.y);
      if (!grouped.has(y)) grouped.set(y, []);
      grouped.get(y).push({ left: box.left, right: box.right });
    }
    return { left: parent.left, right: parent.right, rows: [...grouped.values()] };
  });
  expect(rows.rows.map(row => row.length)).toEqual(expectedRows);
  for (const row of rows.rows) {
    expect(Math.abs(row[0].left - rows.left)).toBeLessThanOrEqual(2);
    expect(Math.abs(row[row.length - 1].right - rows.right)).toBeLessThanOrEqual(2);
    for (let i = 1; i < row.length; i++) expect(row[i].left - row[i - 1].right).toBeLessThanOrEqual(2);
  }
}

for (const width of [390, 820, 1440]) {
  test(`P1.8g actual five-cell Billing grid fills complete rows at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await login(page, 'results-owner@browser.test', 'ResultsOwner-pass1!');
    await page.goto('/billing');
    const grid = page.locator('.workspace-summary__stats--5');
    await expectFilledRows(grid, 5, width < 768 ? [1, 1, 1, 1, 1] : width < 992 ? [2, 2, 1] : [3, 2]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await grid.scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(os.tmpdir(), `twinevia-phase1-billing-five-${width}.png`), fullPage: false });
  });
}

test('P1.8g actual six-cell billed summary fills two desktop rows', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await login(page, 'owner@browser.test', 'Owner-pass1!');
  await page.goto('/billing');
  await expectFilledRows(page.locator('.workspace-summary__stats--6'), 6, [3, 3]);
});

for (const width of [390, 1440]) {
  test(`P1.8h long status text wraps inside a real workspace badge at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await login(page, 'results-owner@browser.test', 'ResultsOwner-pass1!');
    await page.goto('/billing');
    const badge = page.locator('.workspace-summary__badges .badge').first();
    const label = 'Accepted by provider; waiting for the carrier delivery receipt before confirming delivery. A provider acknowledgement confirms submission only; the carrier has not reported a final delivery result.';
    // Exercise the existing component with a longer provider-status description.
    // No test-only component, CSS, route or application state is introduced.
    await badge.evaluate((element, text) => { element.textContent = text; }, label);
    await expect(badge).toHaveText(label);
    const dimensions = await badge.evaluate(element => {
      const box = element.getBoundingClientRect();
      const rail = element.closest('.workspace-summary__rail').getBoundingClientRect();
      const range = document.createRange();
      range.selectNodeContents(element);
      const text = range.getBoundingClientRect();
      return {
        height: box.height, width: box.width, right: box.right, railRight: rail.right,
        scrollWidth: element.scrollWidth, clientWidth: element.clientWidth,
        scrollHeight: element.scrollHeight, clientHeight: element.clientHeight,
        textTop: text.top, textBottom: text.bottom, top: box.top, bottom: box.bottom,
        whiteSpace: getComputedStyle(element).whiteSpace,
      };
    });
    expect(dimensions.whiteSpace).toBe('normal');
    expect(dimensions.height).toBeGreaterThan(30);
    expect(dimensions.right).toBeLessThanOrEqual(dimensions.railRight + 1);
    expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.clientWidth + 1);
    expect(dimensions.scrollHeight).toBeLessThanOrEqual(dimensions.clientHeight + 1);
    expect(dimensions.textTop).toBeGreaterThanOrEqual(dimensions.top);
    expect(dimensions.textBottom).toBeLessThanOrEqual(dimensions.bottom);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await badge.scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(os.tmpdir(), `twinevia-phase1-long-status-${width}.png`), fullPage: false });
  });
}
