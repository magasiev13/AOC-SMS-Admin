const { test, expect } = require('@playwright/test');
const { login, installFailureDiagnostics, attachFailureDiagnostics } = require('./helpers');
const errors = new WeakMap();
test.beforeEach(async ({ page }) => {
  installFailureDiagnostics(page);
  const messages = [];
  errors.set(page, messages);
  page.on('pageerror', error => messages.push(error.message));
});
test.afterEach(async ({ page }, testInfo) => {
  await attachFailureDiagnostics(page, testInfo);
  expect(errors.get(page)).toEqual([]);
});
async function noOverflow(page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
}

test('mobile contact and survey validation preserve entered work', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page, 'owner@browser.test', 'Owner-pass1!');
  await page.goto('/community/add');
  await page.getByLabel('Name (optional)').fill('UX retained contact');
  await page.getByLabel('Phone number').fill('invalid phone');
  await page.getByRole('button', { name: 'Add Member', exact: true }).click();
  await expect(page.getByLabel('Name (optional)')).toHaveValue('UX retained contact');
  await expect(page.getByLabel('Phone number')).toHaveValue('invalid phone');
  await expect(page.getByLabel('Phone number')).toBeFocused();
  await expect(page.getByLabel('Phone number')).toHaveAttribute('aria-invalid', 'true');
  await page.screenshot({ path: testInfo.outputPath('contact-validation-mobile.png'), fullPage: true });
  await noOverflow(page);
  await page.goto('/inbox/surveys/add');
  await page.getByLabel('Survey name').fill('UX retained survey');
  await page.getByLabel('Trigger keyword').fill('UXKEEP');
  const questions = Array.from({ length: 11 }, (_, index) => `Question ${index + 1}`).join('\n');
  await page.getByLabel('Questions (one per line)').fill(questions);
  await page.getByRole('button', { name: /Create Survey|Create survey|Save/ }).click();
  await expect(page.getByLabel('Questions (one per line)')).toHaveValue(questions);
  await expect(page.getByText(/This survey has 11 questions/)).toBeVisible();
  await noOverflow(page);
});

for (const width of [1440, 390]) {
  test(`inbox updates refresh and failed navigation preserve drafts at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    await login(page, 'owner@browser.test', 'Owner-pass1!');
    await page.goto('/inbox');
    const reply = page.getByLabel('Send reply');
    const draft = `Unsent draft survives updates ${width}`;
    const originalKey = await page.locator('.inbox-reply-form [name="request_idempotency_key"]').inputValue();
    await reply.fill(draft);
    const baseline = await page.locator('#inboxStatusMeta').evaluate(element => ({ latest: Number(element.dataset.latestMessageId), count: Number(element.dataset.messageCount), delivery: element.dataset.deliveryVersion }));
    await page.route('**/inbox/status', route => route.fulfill({ json: { latest_message_id: baseline.latest + 1, message_count: baseline.count + 1, delivery_version: baseline.delivery } }));
    await expect(page.getByRole('link', { name: 'View updates' })).toBeVisible({ timeout: 6000 });
    await expect(reply).toHaveValue(draft);
    await expect(reply).toBeFocused();
    await page.reload();
    await expect(reply).toHaveValue(draft);
    await expect(page.locator('.inbox-reply-form [name="request_idempotency_key"]')).toHaveValue(originalKey);
    await page.unroute('**/inbox/status');
    const action = await page.locator('.inbox-reply-form').getAttribute('action');
    await page.route(`**${action}`, route => route.fulfill({ status: 503, body: 'Controlled temporary reply failure' }));
    await page.locator('.inbox-reply-form').getByRole('button', { name: 'Send', exact: true }).click();
    await expect(page.locator('body')).toContainText('Controlled temporary reply failure');
    await page.goBack();
    await expect(reply).toHaveValue(draft);
    await expect(page.locator('.inbox-reply-form [name="request_idempotency_key"]')).toHaveValue(originalKey);
    await page.unroute(`**${action}`);
    await page.locator('.inbox-reply-form').getByRole('button', { name: 'Send', exact: true }).click();
    await expect(page.locator('.inbox-message-bubble').filter({ hasText: draft })).toHaveCount(1);
    await expect(reply).toHaveValue('');
    await expect(page.locator('.inbox-reply-form [name="request_idempotency_key"]')).not.toHaveValue(originalKey);
    await page.reload();
    await expect(reply).toHaveValue('');
    await reply.fill(`Private owner draft ${width}`);
    await page.screenshot({ path: testInfo.outputPath(`draft-preserved-${width}.png`), fullPage: false });
    await noOverflow(page);
    await page.locator('form[action="/logout"]').first().evaluate(form => form.requestSubmit());
    await page.waitForURL('**/login', { waitUntil: 'domcontentloaded' });
    await login(page, 'staff@browser.test', 'Staff-pass1!');
    await page.goto('/inbox');
    await expect(page.getByLabel('Send reply')).toHaveValue('');
  });
}

test('mobile composer presents a deliberate review and named scheduled preview', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page, 'owner@browser.test', 'Owner-pass1!');
  await page.goto('/dashboard');
  expect((await page.getByLabel('Message', { exact: true }).boundingBox()).y).toBeLessThan(500);
  await page.getByLabel('Message', { exact: true }).fill('UX reviewed scheduled notice');
  await page.locator('.advanced-options__summary').click();
  await page.getByLabel('Schedule for later').check();
  await page.getByLabel('Date', { exact: true }).fill('2099-12-31');
  await page.getByLabel('Time', { exact: true }).fill('23:59');
  await page.route('**/dashboard/review', route => route.fulfill({ status: 503, json: { error: 'Controlled review interruption' } }));
  await page.getByRole('button', { name: 'Schedule Message', exact: true }).click();
  await expect(page.locator('#sendFormError')).toHaveText('Controlled review interruption');
  await expect(page.getByLabel('Message', { exact: true })).toHaveValue('UX reviewed scheduled notice');
  await page.unroute('**/dashboard/review');
  await page.getByRole('button', { name: 'Schedule Message', exact: true }).click();
  const review = page.getByRole('dialog', { name: 'Review send', exact: true });
  await expect(review).toBeVisible();
  await expect(review.locator('[data-review="audience"]')).toHaveText('Community');
  await expect(review.locator('[data-review="timing"]')).toContainText('2099-12-31');
  await expect(review.locator('[data-review="sample"]')).toContainText('UX reviewed scheduled notice');
  await expect(review).toBeFocused();
  await review.press('Escape');
  await expect(review).toBeHidden();
  await expect(page.getByRole('button', { name: 'Schedule Message', exact: true })).toBeFocused();
  await page.getByRole('button', { name: 'Schedule Message', exact: true }).press('Enter');
  await expect(review).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('send-review-mobile.png'), fullPage: false });
  await noOverflow(page);
  await review.getByRole('button', { name: 'Confirm schedule' }).focus();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/scheduled$/);
  const card = page.locator('.card-list-item').filter({ hasText: 'UX reviewed scheduled notice' }).first();
  await card.getByRole('button', { name: /Preview/ }).click();
  const preview = page.getByRole('dialog', { name: 'Scheduled preview', exact: true });
  await expect(preview).toBeVisible();
  await expect(preview.getByRole('button', { name: 'Close preview' })).toBeVisible();
  await preview.press('Escape');
  await card.getByRole('button', { name: /Cancel/ }).click();
  await page.getByRole('button', { name: 'Yes', exact: true }).click();
});

test('an accepted reply with a lost redirect retries the original send identity', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page, 'owner@browser.test', 'Owner-pass1!');
  await page.goto('/inbox');
  const reply = page.getByLabel('Send reply');
  const body = 'UX accepted response lost once';
  const originalKey = await page.locator('.inbox-reply-form [name="request_idempotency_key"]').inputValue();
  const originalCookies = await page.context().cookies();
  const action = await page.locator('.inbox-reply-form').getAttribute('action');
  await reply.fill(body);
  await page.route(`**${action}`, async route => {
    // Let the real local handler commit its mocked provider acceptance, then
    // lose both redirect/acknowledgement and its newer signed session cookie.
    const upstream = await route.fetch();
    expect(upstream.status()).toBe(200);
    await page.context().addCookies(originalCookies);
    await route.fulfill({ status: 503, body: 'Controlled accepted reply acknowledgement loss' });
  });
  await page.locator('.inbox-reply-form').getByRole('button', { name: 'Send', exact: true }).click();
  await expect(page.locator('body')).toContainText('Controlled accepted reply acknowledgement loss');
  await page.unroute(`**${action}`);
  await page.goBack();
  await expect(reply).toHaveValue(body);
  await expect(page.locator('.inbox-reply-form [name="request_idempotency_key"]')).toHaveValue(originalKey);
  await page.locator('.inbox-reply-form').getByRole('button', { name: 'Send', exact: true }).click();
  await expect(reply).toHaveValue('');
  await expect(page.locator('.inbox-message-bubble').filter({ hasText: body })).toHaveCount(1);
  await testInfo.attach('accepted-retry-original-key.json', { body: Buffer.from(JSON.stringify({ originalKey, body })), contentType: 'application/json' });
});

test('staff forbidden billing page retains workspace navigation', async ({ page }, testInfo) => {
  await login(page, 'staff@browser.test', 'Staff-pass1!');
  const response = await page.goto('/billing');
  expect(response.status()).toBe(403);
  await expect(page.getByRole('heading', { name: 'This account cannot perform that action' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Back to workspace' })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('staff-forbidden.png'), fullPage: false });
});
