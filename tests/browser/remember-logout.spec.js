const { test, expect } = require('@playwright/test');

test('remembered login is removed on logout and stays signed out in a fresh context', async ({ page, browser }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/login');
  await page.getByLabel('Email or username').fill('owner@browser.test');
  await page.getByLabel('Password', { exact: true }).fill('Owner-pass1!');
  await page.getByLabel('Remember me on this device').check();
  await page.getByRole('button', { name: /Sign in to/i }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  expect((await page.context().cookies()).some(cookie => cookie.name === 'remember_token')).toBeTruthy();
  await page.getByRole('button', { name: 'Toggle navigation', exact: true }).click();
  const mobileNavigation = page.locator('#appMobileNav');
  await expect(mobileNavigation).toBeVisible();
  const signOut = mobileNavigation.getByRole('button', { name: 'Sign out everywhere', exact: true });
  await expect(signOut).toBeVisible();
  await Promise.all([
    page.waitForURL(/\/login$/),
    signOut.click(),
  ]);
  await expect(page).toHaveURL(/\/login$/);
  expect((await page.context().cookies()).some(cookie => cookie.name === 'remember_token')).toBeFalsy();
  const fresh = await browser.newContext({ storageState: await page.context().storageState() });
  try {
    const restoredPage = await fresh.newPage();
    await restoredPage.goto('/dashboard');
    await expect(restoredPage).toHaveURL(/\/login/);
    await expect(restoredPage.getByRole('button', { name: /Sign in to/i })).toBeVisible();
  } finally {
    await fresh.close();
  }
});
