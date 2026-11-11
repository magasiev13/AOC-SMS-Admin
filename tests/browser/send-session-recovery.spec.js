const { test, expect } = require('@playwright/test');
const { login } = require('./helpers');

test('expired session preserves and restores the workspace draft after sign-in', async ({ page, context }) => {
  await login(page, 'owner@browser.test', 'Owner-pass1!');
  await page.goto('/dashboard');
  const message = page.getByLabel('Message', { exact: true });
  await message.fill('Saved draft with {first_name}');
  await context.clearCookies();
  await page.getByRole('button', { name: 'Review Message', exact: true }).click();
  await expect(page.locator('#sendFormError')).toContainText('You were signed out. Sign in again; your message is saved.');
  await expect(message).toHaveValue('Saved draft with {first_name}');
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
