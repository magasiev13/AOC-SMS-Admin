const { test, expect } = require('@playwright/test');
const { login, confirmSendReview, installFailureDiagnostics, attachFailureDiagnostics } = require('./helpers');

test.beforeEach(async ({ page }) => installFailureDiagnostics(page));
test.afterEach(async ({ page }, info) => attachFailureDiagnostics(page, info));

for (const width of [390, 1440]) {
  for (const schedule of [false, true]) {
    test(`successful ${schedule ? 'schedule' : 'fake send'} clears only its saved draft at ${width}px`, async ({ page }, info) => {
      await page.setViewportSize({ width, height: 900 });
      const runtimeErrors = [];
      page.on('pageerror', error => runtimeErrors.push(error.message));
      await login(page, 'owner@browser.test', 'Owner-pass1!');
      await page.goto('/dashboard');
      const message = page.getByLabel('Message', { exact: true });
      const text = `Saved draft cleanup ${schedule ? 'schedule' : 'send'} ${width}`;
      await message.fill(text);
      const draftKey = await page.locator('#sendForm').getAttribute('data-draft-key');
      const anotherKey = `${draftKey}-another-workspace`;
      await page.evaluate(key => localStorage.setItem(key, JSON.stringify({ message_body: 'Retain another workspace draft' })), anotherKey);
      expect(await page.evaluate(key => JSON.parse(localStorage.getItem(key)).message_body, draftKey)).toBe(text);
      await page.locator('.advanced-options__summary').click();
      await page.getByLabel('Test Mode', { exact: true }).check();
      await page.locator('#testRecipientPhone').selectOption({ index: 1 });
      if (schedule) {
        await page.getByLabel('Schedule for Later').check();
        await page.getByLabel('Date', { exact: true }).fill('2099-12-31');
        await page.getByLabel('Time', { exact: true }).fill('23:59');
      }
      await page.getByRole('button', { name: schedule ? 'Review Schedule' : 'Review Message', exact: true }).click();
      await confirmSendReview(page);
      await expect(page).toHaveURL(schedule ? /\/scheduled$/ : /\/logs\/\d+$/);
      if (!schedule) {
        await expect.poll(async () => {
          await page.reload();
          return page.locator('body').innerText();
        }).toContain('was submitted to the provider. This is not a delivery receipt.');
      } else {
        await expect(page.locator('body')).toContainText(text);
      }
      await expect.poll(() => page.evaluate(key => localStorage.getItem(key), draftKey)).toBeNull();
      expect(await page.evaluate(key => JSON.parse(localStorage.getItem(key)).message_body, anotherKey)).toBe('Retain another workspace draft');
      await page.goto('/dashboard');
      await expect(message).toHaveValue('');
      await page.reload();
      await expect(message).toHaveValue('');
      await page.screenshot({ path: info.outputPath(`cleared-${schedule ? 'schedule' : 'send'}-${width}.png`), fullPage: false });
      expect(runtimeErrors).toEqual([]);
      if (schedule) {
        // Remove only this test's local, far-future fixture schedule.
        await page.goto('/scheduled');
        const row = (width < 992 ? page.locator('.card-list-item') : page.locator('.table-data tr')).filter({ hasText: text }).first();
        await row.getByRole('button', { name: 'Cancel', exact: true }).click();
        await page.getByRole('button', { name: 'Yes', exact: true }).click();
      }
    });
  }
}
