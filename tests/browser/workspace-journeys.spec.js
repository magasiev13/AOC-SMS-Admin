const { test, expect } = require('@playwright/test');
const { login } = require('./helpers');

for (const width of [390, 1440]) {
  test(`five workspace places and a private local draft at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    await login(page, 'owner@browser.test', 'Owner-pass1!');
    const places = page.locator(width === 390 ? '.workspace-bottom-nav' : '.workspace-places');
    const labels = ['Home', 'Messages', 'Conversations', 'People', 'Events'];
    await expect(places.getByRole('link')).toHaveText(labels);
    await expect(page.getByRole('heading', { name: 'Today' })).toBeVisible();
    for (const name of ['Messages', 'Conversations', 'People', 'Events', 'Home']) {
      await places.getByRole('link', { name, exact: true }).click();
      await expect(places.locator('[aria-current="page"]')).toHaveText(name);
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    }
    await page.getByRole('link', { name: 'New message' }).click();
    const text = `Saved locally ${width}`;
    await page.getByLabel('Message', { exact: true }).fill(text);
    await expect(page.locator('#composeSegmentTotal')).toHaveText(/[1-9]\d* SMS segments/);
    await expect(page.locator('.compose-preview')).toContainText(text);
    if (width === 390) {
      expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBeLessThan(2 * 844);
      const review = await page.locator('#sendBtn').boundingBox();
      expect(review.y + review.height).toBeLessThanOrEqual(844 - 64);
    }
    await places.getByRole('link', { name: 'Messages' }).click();
    await page.getByRole('link', { name: /Unfinished message/ }).click();
    await expect(page.getByLabel('Message', { exact: true })).toHaveValue(text);
    await page.getByRole('button', { name: 'Review Message', exact: true }).click();
    const review = page.getByRole('dialog', { name: 'Review Message' });
    await expect(review).toBeVisible();
    await review.getByRole('button', { name: 'Edit Message' }).click();
    await page.getByRole('button', { name: 'Discard draft' }).click();
    await places.getByRole('link', { name: 'Messages' }).click();
    await expect(page.getByRole('link', { name: /Unfinished message/ })).toHaveCount(0);
  });
}
