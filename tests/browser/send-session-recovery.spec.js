const { test, expect } = require('@playwright/test');
const { login } = require('./helpers');

test('expired session preserves and restores the workspace draft after sign-in', async ({ page, context }) => {
  await login(page, 'owner@browser.test', 'Owner-pass1!');
  await page.goto('/dashboard');
  const message = page.getByLabel('Message', { exact: true });
  await message.fill('Saved draft with {first_name}');
  const draftKey = await page.locator('#sendForm').getAttribute('data-draft-key');
  await context.clearCookies();
  await page.getByRole('button', { name: 'Review Message', exact: true }).click();
  await expect(page.locator('#sendFormError')).toContainText('You were signed out. Sign in again; your message is saved.');
  await expect(message).toHaveValue('Saved draft with {first_name}');
  expect(await page.evaluate(key => JSON.parse(localStorage.getItem(key)).message_body, draftKey)).toBe('Saved draft with {first_name}');
  await page.locator('#sendFormError').getByRole('link', { name: /Sign in/ }).click();
  await page.getByLabel('Email or username').fill('owner@browser.test');
  await page.getByLabel('Password', { exact: true }).fill('Owner-pass1!');
  await page.getByRole('button', { name: /Sign in to/i }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(message).toHaveValue('Saved draft with {first_name}');
  await page.getByRole('button', { name: 'Discard draft', exact: true }).click();
  await page.reload();
  await expect(message).toHaveValue('');
});

for (const width of [390, 1440]) {
  test(`explicit sign-out removes only that user's drafts at ${width}px`, async ({ page }, testInfo) => {
    const runtimeErrors = [];
    page.on('pageerror', error => runtimeErrors.push(error.message));
    page.on('console', message => {
      if (['error', 'warning'].includes(message.type())) runtimeErrors.push(message.text());
    });
    await page.setViewportSize({ width, height: 900 });
    await login(page, 'owner@browser.test', 'Owner-pass1!');
    await page.goto('/dashboard');
    const message = page.getByLabel('Message', { exact: true });
    await message.fill('Private unsent draft');
    const draftKey = await page.locator('#sendForm').getAttribute('data-draft-key');
    const parts = draftKey.split(':');
    const anotherWorkspaceKey = `${parts.slice(0, 3).join(':')}:another-workspace`;
    // An id sharing the same initial characters must remain a different user.
    const anotherUserKey = `twinevia:send-draft:${parts[2]}0:another-workspace`;
    await page.evaluate(({ anotherWorkspaceKey, anotherUserKey }) => {
      localStorage.setItem(anotherWorkspaceKey, JSON.stringify({ message_body: 'Same user in another workspace' }));
      localStorage.setItem(anotherUserKey, JSON.stringify({ message_body: 'Another user draft' }));
    }, { anotherWorkspaceKey, anotherUserKey });

    await page.locator('form[action="/logout"]').first().evaluate(form => form.requestSubmit());
    await expect(page).toHaveURL(/\/login$/);
    await page.waitForLoadState('load');
    await expect(page).toHaveTitle(/Twinevia/);
    await expect(page.getByRole('heading', { name: /Sign in to Twinevia/i })).toBeVisible();
    const retained = await page.evaluate(({ draftKey, anotherWorkspaceKey, anotherUserKey }) => ({
      active: localStorage.getItem(draftKey),
      sameUser: localStorage.getItem(anotherWorkspaceKey),
      otherUser: localStorage.getItem(anotherUserKey),
    }), { draftKey, anotherWorkspaceKey, anotherUserKey });
    expect(retained.active).toBeNull();
    expect(retained.sameUser).toBeNull();
    expect(JSON.parse(retained.otherUser).message_body).toBe('Another user draft');
    await page.evaluate(async () => {
      await document.fonts.ready;
      await Promise.all(document.getAnimations()
        .filter(animation => Number.isFinite(animation.effect?.getTiming().iterations))
        .map(animation => animation.finished.catch(() => {})));
    });
    await page.screenshot({ path: testInfo.outputPath('explicit-sign-out.png'), fullPage: false });

    await login(page, 'owner@browser.test', 'Owner-pass1!');
    await page.goto('/dashboard');
    await expect(message).toHaveValue('');
    expect(runtimeErrors).toEqual([]);
  });
}

test('non-JSON review failures show recovery text without a parser error', async ({ page }) => {
  await login(page, 'owner@browser.test', 'Owner-pass1!');
  await page.goto('/dashboard');
  await page.getByLabel('Message', { exact: true }).fill('Keep this draft');
  await page.route('**/dashboard/review', route => route.fulfill({ status: 502, contentType: 'text/html', body: '<!doctype html>Gateway error' }));
  await page.getByRole('button', { name: 'Review Message', exact: true }).click();
  await expect(page.locator('#sendFormError')).not.toContainText('Unexpected token');
  await expect(page.locator('#sendFormError')).toBeVisible();
  await expect(page.getByLabel('Message', { exact: true })).toHaveValue('Keep this draft');
});
