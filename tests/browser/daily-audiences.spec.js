const { test, expect } = require('@playwright/test');
const { login, installFailureDiagnostics, attachFailureDiagnostics } = require('./helpers');

test.beforeEach(async ({ page }) => installFailureDiagnostics(page));
test.afterEach(async ({ page }, testInfo) => attachFailureDiagnostics(page, testInfo));

async function noOverflow(page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
}

for (const width of [1440, 390]) {
  test(`past attendance stays reachable for deliberate feedback at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    await login(page, 'owner@browser.test', 'Owner-pass1!');
    await page.goto('/events');
    if (width === 390) {
      await page.locator('.card-list-item').filter({ hasText: 'Acme Spring Launch' }).getByRole('link', { name: 'View', exact: true }).click();
    } else {
      await page.getByRole('link', { name: 'Acme Spring Launch', exact: true }).click();
    }
    const detail = new URL(page.url()).pathname;
    if (width === 390) await page.locator('[data-page-actions-toggle]').click();
    await page.getByRole('link', { name: 'Edit', exact: true }).click();
    const originalDate = await page.getByLabel('Date (optional)').inputValue();
    try {
      await page.getByLabel('Date (optional)').fill('2000-01-01');
      await page.getByRole('button', { name: 'Update Event', exact: true }).click();
      await expect(page.locator('[data-page-actions]').getByRole('link', { name: 'Message registrants' })).toBeVisible();
      await page.goto('/events');
      await expect(page.locator('.collection-panel--results')).not.toContainText('Acme Spring Launch');
      const history = page.getByRole('navigation', { name: 'Event view' }).getByRole('link', { name: 'Past events', exact: true });
      await history.focus();
      await page.keyboard.press('Enter');
      await expect(history).toHaveAttribute('aria-current', 'page');
      await expect(page.locator('.collection-panel--results')).toContainText('Acme Spring Launch');
      await noOverflow(page);
      await page.screenshot({ path: testInfo.outputPath(`past-events-${width}.png`), fullPage: false });
      await page.goto(detail);
      await page.getByRole('link', { name: 'Message registrants' }).last().click();
      await expect(page.getByLabel('Event Blast', { exact: false })).toBeChecked();
      await expect(page.getByLabel('Select Event')).toHaveValue(detail.split('/').pop());
      await expect(page.getByText('Past event follow-up:', { exact: false })).toBeVisible();
      await page.getByLabel('Message', { exact: true }).fill(`Please share feedback about the gathering (${width}).`);
      await page.getByRole('button', { name: /^Send (Now|Message)$/ }).click();
      const review = page.getByRole('dialog', { name: 'Review send', exact: true });
      await expect(review).toBeVisible();
      await expect(review.locator('[data-review="audience"]')).toHaveText('Acme Spring Launch');
      await expect(review.locator('[data-review="eligible"]')).toHaveText('1');
      await noOverflow(page);
      await page.screenshot({ path: testInfo.outputPath(`feedback-review-${width}.png`), fullPage: false });
      await page.keyboard.press('Escape');
    } finally {
      // Restore only the synthetic fixture's date for other browser scenarios.
      await page.goto(`${detail}/edit`);
      await page.getByLabel('Date (optional)').fill(originalDate);
      await page.getByRole('button', { name: 'Update Event', exact: true }).click();
    }
  });

  test(`opting out leaves contactable view and preserves history at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    await login(page, 'owner@browser.test', 'Owner-pass1!');
    const name = `Retained opt-out ${width}`;
    const phone = width === 390 ? '+17205551939' : '+17205551944';
    await page.goto('/community/add');
    await page.getByLabel('Name (optional)').fill(name);
    await page.getByLabel('Phone number').fill(phone);
    await page.getByRole('button', { name: 'Add Member', exact: true }).click();
    const row = (width === 390 ? page.locator('.card-list-item') : page.locator('#communityTable tbody.table-data tr')).filter({ hasText: name });
    await expect(row).toBeVisible();
    await row.getByRole('button', { name: 'More actions', exact: true }).click();
    page.once('dialog', dialog => dialog.accept());
    await row.getByRole('button', { name: /Unsubscribe/ }).click();
    await expect(page.locator('.collection-panel--results')).not.toContainText(name);
    const all = page.getByRole('navigation', { name: 'Community view' }).getByRole('link', { name: 'All contacts' });
    await all.focus();
    await page.keyboard.press('Enter');
    await expect(all).toHaveAttribute('aria-current', 'page');
    await expect(row).toContainText('Unsubscribed');
    await page.getByRole('textbox', { name: 'Search community members' }).fill(phone);
    await page.getByRole('textbox', { name: 'Search community members' }).press('Enter');
    await expect(page).toHaveURL(/view=all/);
    await expect(row).toContainText(name);
    await noOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`retained-optout-${width}.png`), fullPage: false });
    await page.getByRole('navigation', { name: 'Community view' }).getByRole('link', { name: 'Contactable', exact: true }).click();
    await expect(page.locator('.collection-panel--results')).not.toContainText(name);
  });
}
