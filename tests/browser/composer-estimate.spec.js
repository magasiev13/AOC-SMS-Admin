const { test, expect } = require('@playwright/test');
const { login, installFailureDiagnostics, attachFailureDiagnostics } = require('./helpers');

const runtimeErrors = new WeakMap();

test.beforeEach(async ({ page }) => {
  installFailureDiagnostics(page);
  const errors = [];
  runtimeErrors.set(page, errors);
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => {
    if (['error', 'warning'].includes(message.type())) errors.push(message.text());
  });
});

test.afterEach(async ({ page }, testInfo) => {
  await attachFailureDiagnostics(page, testInfo);
  expect(runtimeErrors.get(page)).toEqual([]);
});

async function authoritativeTotal(page) {
  const estimate = page.locator('#messageUnitEstimate');
  await expect(estimate).toHaveAttribute('data-total-segments', /^[1-9]\d*$/);
  await expect(estimate).toContainText(/segments each × \d+ recipients = \d+/);
  return Number(await estimate.getAttribute('data-total-segments'));
}

async function checkReviewTotal(page, total) {
  await page.getByRole('button', { name: 'Review Message', exact: true }).click();
  const review = page.getByRole('dialog', { name: 'Review Message', exact: true });
  await expect(review).toBeVisible();
  await expect(review.locator('[data-review="segments"]')).toHaveText(String(total));
  await expect(review.locator('[data-review="sample"]')).not.toContainText('{first_name}');
  return review;
}

for (const width of [390, 1440]) {
  test(`personalized composer and review agree and special-character replacement is explicit at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    await login(page, 'owner@browser.test', 'Owner-pass1!');
    const response = await page.goto('/dashboard');
    expect(response.status()).toBe(200);
    await expect(page).toHaveURL(/\/dashboard$/);
    await expect(page).toHaveTitle(/Send a Message.*Twinevia/);
    await expect(page.locator('#sendForm')).toBeVisible();

    const message = page.getByLabel('Message', { exact: true });
    const draft = `Hi {first_name}, ${'A'.repeat(150)}🙂`;
    await message.fill(draft);
    const total = await authoritativeTotal(page);
    const warning = page.locator('#specialCharacterWarning');
    await expect(warning).toBeVisible();
    await expect(page.locator('#specialCharacterWarningText')).toContainText(/SLIGHTLY SMILING FACE.*U\+1F642/i);
    await expect(message).toHaveValue(draft);
    await warning.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath(`composer-unicode-${width}.png`), fullPage: false });

    const review = await checkReviewTotal(page, total);
    await expect(message).toHaveValue(draft);
    await page.screenshot({ path: testInfo.outputPath(`composer-review-${width}.png`), fullPage: false });
    await review.getByRole('button', { name: 'Edit Message', exact: true }).click();
    await expect(review).toBeHidden();
    await expect(message).toHaveValue(draft);

    await page.getByRole('button', { name: 'Replace special characters', exact: true }).click();
    await expect(message).toHaveValue(draft.replace('🙂', ''));
    const replacementTotal = await authoritativeTotal(page);
    expect(replacementTotal).toBeLessThan(total);
    await expect(warning).toBeHidden();
    const undo = page.getByRole('button', { name: 'Undo', exact: true });
    await expect(undo).toBeVisible();
    await undo.click();
    await expect(message).toHaveValue(draft);
    await expect(undo).toBeHidden();
    await expect(warning).toBeVisible();
    await page.getByRole('button', { name: 'Replace special characters', exact: true }).click();
    await expect(message).toHaveValue(draft.replace('🙂', ''));
    await authoritativeTotal(page);
    await checkReviewTotal(page, replacementTotal);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  });
}

test('emoji-only replacement can be undone; an edit clears Undo; Armenian text is preserved', async ({ page }, testInfo) => {
  await login(page, 'owner@browser.test', 'Owner-pass1!');
  await page.goto('/dashboard');
  const message = page.getByLabel('Message', { exact: true });
  const replace = page.getByRole('button', { name: 'Replace special characters', exact: true });
  const undo = page.getByRole('button', { name: 'Undo', exact: true });
  await message.fill('🙂🎉👨‍👩‍👧‍👦🇦🇲');
  await replace.click();
  await expect(message).toHaveValue('');
  await undo.click();
  await expect(message).toHaveValue('🙂🎉👨‍👩‍👧‍👦🇦🇲');
  await replace.click();
  await message.fill('A new draft');
  await expect(undo).toBeHidden();
  const armenian = 'Բարև աշխարհ 🙂 — հանդիպում';
  await message.fill(armenian);
  await expect(page.locator('#specialCharacterWarning')).toBeVisible();
  await expect(page.locator('#specialCharacterWarningText')).toContainText('This message uses Unicode letters');
  await expect(replace).toBeHidden();
  await expect(message).toHaveValue(armenian);
  await expect(undo).toBeHidden();
  await page.locator('#specialCharacterWarning').scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('armenian-preserved.png') });
});

test('saved-test-recipient composer and review use the same personalized total', async ({ page }) => {
  await login(page, 'owner@browser.test', 'Owner-pass1!');
  await page.goto('/dashboard');
  await page.locator('.advanced-options__summary').click();
  await page.getByLabel('Test Mode', { exact: true }).check();
  await page.getByLabel('Send to all saved test recipients', { exact: true }).check();
  await page.getByLabel('Message', { exact: true }).fill(`Hi {first_name}, ${'B'.repeat(150)}🙂`);
  const total = await authoritativeTotal(page);
  await expect(page.locator('#audienceEligibility')).toContainText('Internal test recipients only.');
  const review = await checkReviewTotal(page, total);
  await expect(review.locator('[data-review="eligible"]')).toHaveText('2');
});

test('editing after a large sealed review keeps the preview within ordinary form limits', async ({ page }) => {
  await login(page, 'owner@browser.test', 'Owner-pass1!');
  await page.goto('/dashboard');
  await page.getByLabel('Message', { exact: true }).fill('Large review draft');
  await page.route('**/dashboard/review', async route => {
    const response = await route.fetch();
    const review = await response.json();
    // Model a supported large frozen audience without creating any send.
    review.review_token = 'synthetic-sealed-review-'.repeat(16000);
    await route.fulfill({ response, json: review });
  });
  await page.getByRole('button', { name: 'Review Message', exact: true }).click();
  const review = page.getByRole('dialog', { name: 'Review Message', exact: true });
  await expect(review).toBeVisible();
  await expect(review).toBeFocused();
  await review.press('Escape');
  await expect(review).toBeHidden();
  const preview = page.waitForResponse(response => response.url().includes('/dashboard/audience-preview') && response.request().method() === 'POST');
  await page.getByLabel('Message', { exact: true }).fill('Edited large review draft');
  const response = await preview;
  expect(response.status()).toBe(200);
  const body = response.request().postData() || '';
  expect(body.includes('name="review_token"')).toBe(false);
  expect(body.length).toBeLessThan(50_000);
  await expect(page.locator('#messageUnitEstimate')).toContainText('segments each');
  await expect(page.getByLabel('Message', { exact: true })).toHaveValue('Edited large review draft');
});
