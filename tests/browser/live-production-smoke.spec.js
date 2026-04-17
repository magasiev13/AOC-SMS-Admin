const { test, expect } = require('@playwright/test');
const {
  attachFailureDiagnostics,
  installFailureDiagnostics,
  login,
  organizationRow,
} = require('./helpers');

const controlOrganizationName = 'IT Wingman LLC';
const ownerUsername = process.env.TWINEVIA_OWNER_USERNAME;
const ownerPassword = process.env.TWINEVIA_OWNER_PASSWORD;
const platformUsername = process.env.TWINEVIA_PLATFORM_USERNAME;
const platformPassword = process.env.TWINEVIA_PLATFORM_PASSWORD;

test.describe.configure({ mode: 'serial' });

test.beforeEach(async ({ page }) => {
  installFailureDiagnostics(page);
});

test.afterEach(async ({ page }, testInfo) => {
  await attachFailureDiagnostics(page, testInfo);
});

test('public auth surfaces and health respond on the live host', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1100 });

  let response = await page.goto('/health');
  expect(response).not.toBeNull();
  expect(response.status()).toBe(200);
  await expect(page.locator('body')).toContainText('OK');

  response = await page.goto('/login');
  expect(response).not.toBeNull();
  expect(response.status()).toBe(200);
  await expect(page.getByText('Workspace access')).toBeVisible();
  await expect(page.getByRole('link', { name: 'Create a workspace' })).toBeVisible();

  response = await page.goto('/signup');
  expect(response).not.toBeNull();
  expect(response.status()).toBe(200);
  await expect(page.getByRole('button', { name: 'Continue' })).toBeVisible();

  response = await page.goto('/platform/login');
  expect(response).not.toBeNull();
  expect(response.status()).toBe(200);
  await expect(page.getByText('Platform access')).toBeVisible();
});

test('control owner account reaches the live readiness surfaces without mutating state', async ({ page }) => {
  await login(page, ownerUsername, ownerPassword);
  await expect(page).toHaveURL(/\/(setup|dashboard)(\?|$)/);

  let response = await page.goto('/billing');
  expect(response).not.toBeNull();
  expect(response.status()).toBe(200);
  await expect(page.getByRole('heading', { name: 'Billing' })).toBeVisible();

  response = await page.goto('/dashboard');
  expect(response).not.toBeNull();
  expect(response.status()).toBe(200);
  await expect(page.getByRole('heading', { name: 'Workspace' })).toBeVisible();
  await expect(page.getByText('Live SMS is paused.')).toBeVisible();
  await expect(page.getByText(/Submitted to Twilio|Carrier review in progress|Await Twilio review/).first()).toBeVisible();
});

test('platform admin can inspect the control org without changing live state', async ({ page }) => {
  await login(page, platformUsername, platformPassword, '/platform/login');

  let response = await page.goto('/platform');
  expect(response).not.toBeNull();
  expect(response.status()).toBe(200);

  response = await page.goto('/platform/organizations');
  expect(response).not.toBeNull();
  expect(response.status()).toBe(200);

  const organization = organizationRow(page, controlOrganizationName);
  await expect(organization).toBeVisible();

  const messagingHref = await organization.locator('a[href*="/messaging"]').filter({ hasText: /Manage provider/i }).first().getAttribute('href');
  expect(messagingHref).toBeTruthy();

  response = await page.goto(messagingHref);
  expect(response).not.toBeNull();
  expect(response.status()).toBe(200);
  await expect(page.locator('.platform-shell__summary')).toBeVisible();
  await expect(page.getByText(/Platform-managed Twilio|Customer-managed Twilio/).first()).toBeVisible();
  await expect(page.getByLabel('Service Address Line 1')).toBeVisible();

  response = await page.goto(`${messagingHref}/onboarding`);
  expect(response).not.toBeNull();
  expect(response.status()).toBe(200);
  await expect(page.locator('.platform-shell__summary')).toBeVisible();
  await expect(page.getByText('Registration settings')).toBeVisible();
  await expect(page.getByText('Recent Twilio activity')).toBeVisible();
});
