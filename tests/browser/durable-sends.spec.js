const { test, expect } = require('@playwright/test');
const { login, confirmSendReview, installFailureDiagnostics, attachFailureDiagnostics } = require('./helpers');

const runtimeErrors = new WeakMap();
test.beforeEach(async ({ page }) => {
  installFailureDiagnostics(page);
  const errors = [];
  runtimeErrors.set(page, errors);
  page.on('pageerror', error => errors.push(error.message));
});
test.afterEach(async ({ page }, testInfo) => {
  await attachFailureDiagnostics(page, testInfo);
  expect(runtimeErrors.get(page)).toEqual([]);
  if (process.env.QA_CAPTURE_DIR && testInfo.status === 'passed') {
    await page.screenshot({ path: `${process.env.QA_CAPTURE_DIR}/${testInfo.title.replace(/[^a-z0-9]+/gi, '-')}.png` });
  }
});

async function noOverflow(page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBeTruthy();
}

async function repeatForm(page, url, form) {
  // Keep the same browser user agent/session binding as the original submit.
  return page.evaluate(async ({ url, form }) => {
    const response = await fetch(url, { method: 'POST', body: new URLSearchParams(form) });
    return { status: response.status, url: response.url, text: await response.text() };
  }, { url, form });
}

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
  test(`durable broadcast survives replay and supports a deliberate new send at ${viewport.width}px`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await login(page, 'owner@browser.test', 'Owner-pass1!');
    await page.goto('/dashboard');
    await expect(page.locator('#audienceEligibility')).toContainText('eligible recipient(s)');
    const body = `Durable browser broadcast ${viewport.width}`;
    await page.getByLabel('Message', { exact: true }).fill(body);
    const key = await page.locator('#sendForm input[name="request_idempotency_key"]').inputValue();
    const csrf = await page.locator('#sendForm input[name="csrf_token"]').inputValue();
    await page.getByRole('button', { name: 'Send Now', exact: true }).click();
    await confirmSendReview(page);
    await expect(page).toHaveURL(/\/logs\/\d+$/);
    const logUrl = page.url();
    await expect.poll(async () => {
      await page.reload();
      return page.locator('body').innerText();
    }).toContain('was submitted to the provider. This is not a delivery receipt.');
    await noOverflow(page);

    // Reuse the original form payload after a refresh/lost redirect. It must
    // return the same persisted log, even after the worker has completed.
    const repeated = await repeatForm(page, '/dashboard', {
      message_body: body, target: 'community', include_unsubscribe: 'on',
      request_idempotency_key: key, csrf_token: csrf,
    });
    expect(repeated.status).toBe(200);
    expect(repeated.url).toBe(logUrl);
    const changed = await repeatForm(page, '/dashboard', {
      message_body: body + ' changed', target: 'community', include_unsubscribe: 'on',
      request_idempotency_key: key, csrf_token: csrf,
    });
    expect(changed.text).toContain('already belongs to different content');

    await page.goto('/dashboard');
    expect(await page.locator('#sendForm input[name="request_idempotency_key"]').inputValue()).not.toBe(key);
    await page.getByLabel('Message', { exact: true }).fill(body);
    await page.getByRole('button', { name: 'Send Now', exact: true }).click();
    await confirmSendReview(page);
    await expect(page).toHaveURL(/\/logs\/\d+$/);
    expect(page.url()).not.toBe(logUrl);
    await expect.poll(async () => {
      await page.reload();
      return page.locator('body').innerText();
    }).toContain('was submitted to the provider. This is not a delivery receipt.');
  });

  test(`durable reply records one bubble on retry at ${viewport.width}px`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await login(page, 'owner@browser.test', 'Owner-pass1!');
    await page.goto('/inbox');
    const form = page.locator('.inbox-reply-form');
    await expect(form).toBeVisible();
    const action = await form.getAttribute('action');
    const key = await form.locator('[name="request_idempotency_key"]').inputValue();
    const csrf = await form.locator('[name="csrf_token"]').inputValue();
    const body = `Durable keyboard reply ${viewport.width}`;
    await page.getByLabel('Send reply').fill(body);
    await page.getByLabel('Send reply').press('Tab');
    await expect(form.getByRole('button', { name: 'Send', exact: true })).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(page.locator('body')).toContainText('not a delivery receipt');
    await expect(page.locator('.inbox-message-bubble').filter({ hasText: body })).toHaveCount(1);
    await noOverflow(page);
    const repeated = await repeatForm(page, action, {
      body, request_idempotency_key: key, csrf_token: csrf,
    });
    expect(repeated.status).toBe(200);
    await page.reload();
    await expect(page.locator('.inbox-message-bubble').filter({ hasText: body })).toHaveCount(1);
    if (viewport.width < 500) {
      await page.getByRole('link', { name: 'Back to threads' }).click();
      await expect(page.locator('#inboxThreadsPanel')).toBeVisible();
      await page.locator('.inbox-thread-link').filter({ hasText: 'Maya Chen' }).click();
      await expect(page.getByLabel('Send reply')).toBeVisible();
    }
  });
}
