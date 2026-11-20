const { test, expect } = require('@playwright/test');
const { login } = require('./helpers');

test.use({ viewport: { width: 390, height: 844 } });

test('public navigation closes with Escape and keeps keyboard focus on Menu', async ({ page }) => {
  await page.goto('/');
  const menu = page.locator('.mobile-menu summary');
  await expect(menu).toHaveText('Menu');
  await menu.focus();
  await menu.press('Enter');
  const navigation = page.getByRole('navigation', { name: 'Mobile navigation' });
  await page.keyboard.press('Tab');
  await expect(navigation.getByRole('link', { name: 'Features' })).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(navigation).toBeHidden();
  await expect(menu).toBeFocused();
});

test('workspace navigation releases the page on tab exit or a switch to desktop', async ({ page }) => {
  await login(page, 'owner@browser.test', 'Owner-pass1!');
  const toggle = page.getByRole('button', { name: 'Toggle navigation' });
  const menu = page.locator('#appMobileNav');
  await toggle.focus();
  await toggle.press('Enter');
  const last = menu.getByRole('button', { name: 'Sign Out Everywhere' });
  await expect(last).toBeVisible();
  await last.focus();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('link', { name: 'New message', exact: true })).toBeFocused();
  await expect(menu).toBeHidden();

  await toggle.press('Enter');
  await expect(last).toBeVisible();
  await page.setViewportSize({ width: 1440, height: 900 });
  await expect(page.locator('[aria-controls="appMobileNav"]')).toHaveAttribute('aria-expanded', 'false');
  await expect(page.locator('body')).not.toHaveClass(/app-mobile-nav-open/);
});

test('sign-in errors expose a keyboard-operable dismiss action', async ({ page }) => {
  await page.goto('/login');
  await page.getByLabel('Email or username').fill('unknown-keyboard-test@browser.test');
  await page.getByLabel('Password', { exact: true }).fill('invalid');
  await page.getByRole('button', { name: /Sign in to/ }).press('Enter');
  const alert = page.getByRole('alert');
  await expect(alert).toBeVisible();
  await alert.getByRole('button', { name: 'Dismiss notification' }).press('Enter');
  await expect(alert).toBeHidden();
});
