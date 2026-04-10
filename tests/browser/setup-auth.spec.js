const { test, expect } = require('@playwright/test');
const {
  attachFailureDiagnostics,
  installFailureDiagnostics,
  login,
} = require('./helpers');

test.beforeEach(async ({ page }) => {
  installFailureDiagnostics(page);
});

test.afterEach(async ({ page }, testInfo) => {
  await attachFailureDiagnostics(page, testInfo);
});

test('workspace and platform login surfaces are clearly separated', async ({ page }) => {
  await page.goto('/login');
  await expect(page.getByText('Workspace access')).toBeVisible();
  await expect(page.getByLabel('Email or username')).toBeVisible();
  await expect(page.getByRole('link', { name: 'Create one here' })).toBeVisible();

  await page.goto('/platform/login');
  await expect(page.getByText('Platform control')).toBeVisible();
  await expect(page.getByText('Platform admins manage organizations')).toBeVisible();
  await expect(page.getByRole('link', { name: 'Use the workspace login' })).toBeVisible();
});

test('owner with incomplete setup lands on the setup runway', async ({ page }) => {
  await login(page, 'trial-owner@browser.test', 'TrialOwner-pass1!');

  await expect(page).toHaveURL(/\/setup$/);
  await expect(page.getByText('Owner setup')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Setup Runway Bakery' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Activate billing' })).toBeVisible();
});

test('self-serve signup creates a workspace and opens setup', async ({ page }) => {
  await page.goto('/signup');
  await page.getByLabel('Business name').fill('Signup Browser Bakery');
  await page.getByLabel('Full name').fill('Signup Owner');
  await page.getByLabel('Business email').fill('signup-owner@browser.test');
  await page.getByLabel('Username').fill('signup-owner');
  await page.getByLabel('Mobile phone').fill('+15550001999');
  await page.getByLabel('Password', { exact: true }).fill('Signup-pass1!');
  await page.getByLabel('Confirm password').fill('Signup-pass1!');
  await page.getByRole('button', { name: 'Create workspace' }).click();

  await expect(page).toHaveURL(/\/setup$/);
  await expect(page.getByText('Owner setup')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Signup Browser Bakery' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Activate billing' })).toBeVisible();
});
