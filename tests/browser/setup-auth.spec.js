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

async function surfaceMetrics(page) {
  return page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    scrollHeight: document.documentElement.scrollHeight,
    innerWidth: window.innerWidth,
    innerHeight: window.innerHeight,
  }));
}

async function box(locator) {
  const value = await locator.boundingBox();
  expect(value).not.toBeNull();
  return value;
}

test('workspace and platform login surfaces are clearly separated', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.goto('/login');
  await expect(page.getByText('Workspace access')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Run messaging from one calm workspace.' })).toBeVisible();
  await expect(page.getByLabel('Email or username')).toBeVisible();
  await expect(page.getByRole('link', { name: 'Create a workspace' })).toBeVisible();
  await expect(page.locator('body')).not.toContainText(/SMS Admin|Twinevia Legacy/);

  const loginMetrics = await surfaceMetrics(page);
  expect(loginMetrics.scrollWidth).toBeLessThanOrEqual(loginMetrics.innerWidth);
  expect(loginMetrics.scrollHeight).toBeLessThanOrEqual(loginMetrics.innerHeight);

  const storyBox = await box(page.locator('.auth-story').first());
  const panelBox = await box(page.locator('.auth-panel--login').first());
  expect(storyBox.x).toBeLessThan(panelBox.x);

  await page.goto('/platform/login');
  await expect(page.getByText('Platform access')).toBeVisible();
  await expect(page.getByText('Review onboarding, workspace access, and sender readiness')).toBeVisible();
  await expect(page.getByRole('link', { name: 'Open workspace login' })).toBeVisible();
  await expect(page.locator('body')).not.toContainText(/SMS Admin|Twinevia Legacy/);

  const platformMetrics = await surfaceMetrics(page);
  expect(platformMetrics.scrollWidth).toBeLessThanOrEqual(platformMetrics.innerWidth);
  expect(platformMetrics.scrollHeight).toBeLessThanOrEqual(platformMetrics.innerHeight);
});

test('owner with incomplete setup lands on the setup runway', async ({ page }) => {
  await login(page, 'trial-owner@browser.test', 'TrialOwner-pass1!');

  await expect(page).toHaveURL(/\/setup$/);
  await expect(page.getByText('Owner setup')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Setup Runway Bakery' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Activate billing' })).toBeVisible();
});

test('customer-managed owner with pending activation lands on the read-only setup runway', async ({ page }) => {
  await login(page, 'customer-managed-owner@browser.test', 'CustomerManaged-pass1!');

  await expect(page).toHaveURL(/\/setup$/);
  await expect(page.getByText('Owner setup')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Customer Managed Bakery' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'External Twilio activation' })).toBeVisible();
  await expect(page.getByText('Workspace owners are read-only here')).toBeVisible();
  await expect(page.locator('body')).not.toContainText('Legal business name');
  await expect(page.locator('body')).not.toContainText('Submit for Twilio review');
});

test('customer-managed owner with active messaging lands in the workspace', async ({ page }) => {
  await login(page, 'customer-managed-ready@browser.test', 'CustomerManagedReady-pass1!');

  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.getByRole('heading', { name: 'Workspace' })).toBeVisible();
});

test('self-serve signup creates a workspace and opens setup', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/signup');
  await expect(page.locator('body')).not.toContainText(/SMS Admin|Twinevia Legacy/);
  await expect(page.locator('[data-signup-indicator="1"]')).toHaveClass(/is-current/);
  await expect(page.getByRole('button', { name: 'Continue' })).toBeVisible();

  const signupMetrics = await surfaceMetrics(page);
  expect(signupMetrics.scrollWidth).toBeLessThanOrEqual(signupMetrics.innerWidth);

  const continueBox = await box(page.getByRole('button', { name: 'Continue' }));
  expect(continueBox.y + continueBox.height).toBeLessThanOrEqual(signupMetrics.innerHeight);

  await page.getByLabel('Business name').fill('Signup Browser Bakery');
  await page.getByLabel('Full name').fill('Signup Owner');
  await page.getByLabel('Business email').fill('signup-owner@browser.test');
  await page.getByLabel('Mobile phone').fill('+15550001999');
  await page.getByRole('button', { name: 'Continue' }).click();

  await expect(page.locator('[data-signup-indicator="2"]')).toHaveClass(/is-current/);
  await page.getByLabel('Username').fill('signup-owner');
  await page.getByLabel('Password', { exact: true }).fill('Signup-pass1!');
  await page.getByLabel('Confirm password').fill('Signup-pass1!');
  await page.getByRole('button', { name: 'Create workspace' }).click();

  await expect(page).toHaveURL(/\/setup$/);
  await expect(page.getByText('Owner setup')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Signup Browser Bakery' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Activate billing' })).toBeVisible();
});
