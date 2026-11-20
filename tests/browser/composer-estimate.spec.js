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

test('an unavailable audience preview leaves the audience picker quiet and review usable', async ({ page }) => {
  await login(page, 'owner@browser.test', 'Owner-pass1!');
  // A session redirect or proxy can return HTML where a preview expects JSON.
  await page.route('**/dashboard/audience-preview', route =>
    route.fulfill({ contentType: 'text/html', body: '<html></html>' }));
  await page.goto('/dashboard');
  await page.getByLabel('Message', { exact: true }).fill('Review this audience');
  await expect(page.locator('#messageUnitEstimate')).toContainText('Review the message to check its segments.');
  const audiencePicker = page.locator('#compose-who');
  await expect(audiencePicker.getByText('Permission checks', { exact: true })).toHaveCount(0);
  await expect(audiencePicker.locator('#audienceEligibility')).toBeHidden();
  await expect(audiencePicker).not.toContainText('Audience preview is unavailable');

  await page.getByRole('button', { name: 'Review Message', exact: true }).click();
  const review = page.getByRole('dialog', { name: 'Review Message', exact: true });
  await expect(review.locator('[data-review="eligible"]')).toHaveText(/^[1-9]\d*$/);
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
  let extraPreviews = 0;
  page.on('request', request => {
    if (request.url().includes('/dashboard/audience-preview')) extraPreviews += 1;
  });
  await page.getByLabel('Message', { exact: true }).fill('Edited large review draft');
  await authoritativeTotal(page);
  await page.waitForTimeout(900);
  expect(extraPreviews).toBe(0);
  await expect(page.locator('#messageUnitEstimate')).toContainText('segments each');
  await expect(page.getByLabel('Message', { exact: true })).toHaveValue('Edited large review draft');
});

test('typing 200 characters, whitespace and footer changes do not recompute the server audience', async ({ page }) => {
  const previews = [];
  page.on('request', request => {
    if (request.url().includes('/dashboard/audience-preview')) previews.push(request);
  });
  await login(page, 'owner@browser.test', 'Owner-pass1!');
  await page.goto('/dashboard');
  await expect(page.locator('#audienceEligibility')).toContainText(/\d+ eligible recipient/);
  const initialCount = previews.length;
  expect(initialCount).toBeLessThanOrEqual(2);
  const message = page.getByLabel('Message', { exact: true });
  await message.fill('');
  await message.pressSequentially('A'.repeat(200), { delay: 5 });
  const total = await authoritativeTotal(page);
  await message.fill(`  ${'A'.repeat(200)}  `);
  await expect(page.locator('#messageUnitEstimate')).toHaveAttribute('data-total-segments', String(total));
  await page.getByLabel('Include Unsubscribe Option').uncheck();
  await authoritativeTotal(page);
  await page.getByLabel('Include Unsubscribe Option').check();
  await page.waitForTimeout(900);
  expect(previews.length).toBe(initialCount);
  for (const request of previews) {
    const fields = new URLSearchParams(request.postData());
    expect(fields.get('audience_profile')).toBe('1');
    for (const field of ['message_body', 'review_token', 'schedule_time', 'include_unsubscribe']) {
      expect(fields.has(field)).toBe(false);
    }
  }
  await checkReviewTotal(page, total);
});

test('changing audience cancels an in-flight preview and uses the newer audience', async ({ page }) => {
  await login(page, 'owner@browser.test', 'Owner-pass1!');
  await page.goto('/dashboard');
  await expect(page.locator('#audienceEligibility')).toContainText(/\d+ eligible recipient/);
  // Model a slow transport without starting a real long-running server request.
  // Its promise rejects only if the composer cancels the supplied fetch signal.
  await page.evaluate(() => {
    const realFetch = window.fetch;
    window.previewStarted = false;
    window.previewAborted = false;
    window.fetch = (url, options) => {
      if (String(url).includes('/dashboard/audience-preview') && options.body.get('target') === 'event') {
        window.previewStarted = true;
        return new Promise((resolve, reject) => {
          options.signal.addEventListener('abort', () => {
            window.previewAborted = true;
            reject(new DOMException('Preview canceled', 'AbortError'));
          }, { once: true });
        });
      }
      return realFetch(url, options);
    };
  });
  const eventId = await page.locator('#event_id option').nth(1).getAttribute('value');
  await page.getByLabel('Event Registrants', { exact: false }).check();
  await page.locator('#event_id').selectOption(eventId);
  await expect.poll(() => page.evaluate(() => window.previewStarted)).toBe(true);
  await page.getByLabel('Contact List', { exact: false }).check();
  await expect.poll(() => page.evaluate(() => window.previewAborted)).toBe(true);
  await expect(page.locator('#audienceEligibility')).toContainText(/\d+ eligible recipient/);
  await page.getByLabel('Message', { exact: true }).fill('Newest audience only');
  await checkReviewTotal(page, await authoritativeTotal(page));
});

test('Unicode whitespace boundaries keep composer and review estimates equal', async ({ page }) => {
  await login(page, 'owner@browser.test', 'Owner-pass1!');
  await page.goto('/dashboard');
  await page.getByLabel('Include Unsubscribe Option').uncheck();
  for (const draft of ['A'.repeat(70) + '\uFEFF', 'A'.repeat(160) + '\u0085']) {
    await page.getByLabel('Message', { exact: true }).fill(draft);
    const review = await checkReviewTotal(page, await authoritativeTotal(page));
    await review.getByRole('button', { name: 'Edit Message', exact: true }).click();
    await expect(review).toBeHidden();
  }
});
