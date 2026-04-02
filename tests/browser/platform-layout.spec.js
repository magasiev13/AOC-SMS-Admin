const { test, expect } = require('@playwright/test');

async function login(page, username, password) {
  await page.goto('/login');
  await page.getByLabel('Email or username').fill(username);
  await page.getByLabel('Password').fill(password);
  await page.getByRole('button', { name: 'Sign In' }).click();
}

async function elementHeight(locator) {
  return locator.evaluate((element) => Math.round(element.getBoundingClientRect().height));
}

test('platform admin desktop surfaces use the shared shell and aligned actions', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  await login(page, 'platform@browser.test', 'Platform-pass1!');

  await page.goto('/platform/organizations');
  await expect(page.locator('.platform-shell__summary')).toBeVisible();
  await expect(page.locator('.platform-directory__row').first()).toBeVisible();
  await expect(page.locator('.platform-directory__row').first().locator('.platform-readiness-item')).toHaveCount(3);

  const pageActionHeight = await elementHeight(page.getByRole('link', { name: 'Add Organization' }));
  expect(pageActionHeight).toBeGreaterThanOrEqual(44);

  const firstRow = page.locator('.platform-directory__row').first();
  const accessHref = await firstRow.locator('a[href*="/access"]').first().getAttribute('href');
  const messagingHref = await firstRow.locator('a[href*="/messaging"]').first().getAttribute('href');
  expect(accessHref).toBeTruthy();
  expect(messagingHref).toBeTruthy();

  const primaryActionHeight = await elementHeight(firstRow.locator('.row-actions__primary .btn').first());
  const overflowToggleHeight = await elementHeight(firstRow.locator('.row-actions__overflow-toggle').first());
  expect(Math.abs(primaryActionHeight - overflowToggleHeight)).toBeLessThanOrEqual(1);

  await page.getByRole('link', { name: 'Add Organization' }).click();
  await expect(page.locator('.platform-shell__summary')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Create Business Account' })).toBeVisible();
  expect(await elementHeight(page.getByRole('button', { name: 'Create Business Account' }))).toBeGreaterThanOrEqual(44);

  await page.goto(accessHref);
  await expect(page.locator('.platform-shell__summary')).toBeVisible();
  await expect(page.locator('.platform-shell__summary-meta')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Create Staff Invite' })).toBeVisible();
  expect(await elementHeight(page.getByRole('button', { name: 'Create Staff Invite' }))).toBeGreaterThanOrEqual(44);

  await page.goto(messagingHref);
  await expect(page.locator('.platform-shell__summary')).toBeVisible();
  await expect(page.locator('.platform-shell__summary-meta')).toBeVisible();
  await expect(page.getByText('Platform-managed Twilio')).toBeVisible();
  await expect(page.locator('form button.btn-primary')).toHaveCount(1);

  await page.getByRole('link', { name: 'Manage A2P Onboarding' }).click();
  await expect(page.locator('.platform-shell__summary')).toBeVisible();
  await expect(page.locator('.platform-shell__summary-meta')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Submit A2P Onboarding' })).toBeVisible();
  expect(await elementHeight(page.getByRole('button', { name: 'Submit A2P Onboarding' }))).toBeGreaterThanOrEqual(44);
});

test('platform admin mobile surfaces keep 44px action targets', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page, 'platform@browser.test', 'Platform-pass1!');

  await page.goto('/platform/organizations');
  await expect(page.locator('.card-list-item.platform-org-card').first()).toBeVisible();

  const firstMobileButton = page.locator('.card-list-item.platform-org-card .btn').first();
  expect(await elementHeight(firstMobileButton)).toBeGreaterThanOrEqual(44);

  await page.getByRole('link', { name: 'Add Organization' }).click();
  await expect(page.locator('.platform-shell__summary')).toBeVisible();
  expect(await elementHeight(page.getByRole('button', { name: 'Create Business Account' }))).toBeGreaterThanOrEqual(44);
});
