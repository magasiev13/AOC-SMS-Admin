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

async function waitForCurrentNavigation(page) {
  await page.waitForLoadState('domcontentloaded');
  await page.waitForLoadState('load');
}

function expectSuccessfulDocumentResponse(response) {
  expect(response).not.toBeNull();
  expect(response.status()).toBe(200);
}

async function gotoAndExpectOk(page, path) {
  await waitForCurrentNavigation(page);
  const response = await page.goto(path, { waitUntil: 'load' });
  expectSuccessfulDocumentResponse(response);
  return response;
}

async function clickWorkspaceLinkAndExpectOk(page, linkName, pathname) {
  await waitForCurrentNavigation(page);
  const origin = new URL(page.url()).origin;
  const responsePromise = page.waitForResponse((response) => {
    const responseUrl = new URL(response.url());
    return (
      responseUrl.origin === origin
      && responseUrl.pathname === pathname
      && response.request().method() === 'GET'
      && response.request().resourceType() === 'document'
    );
  });

  await page.getByRole('link', { name: linkName }).click();
  const response = await responsePromise;
  expectSuccessfulDocumentResponse(response);
  await expect(page).toHaveURL(new RegExp(`${pathname.replace('/', '\\/')}($|\\?)`));
  await waitForCurrentNavigation(page);
  return response;
}

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
  await waitForCurrentNavigation(page);

  if (/\/dashboard(\?|$)/.test(new URL(page.url()).pathname + new URL(page.url()).search)) {
    await clickWorkspaceLinkAndExpectOk(page, 'Billing', '/billing');
  } else {
    await gotoAndExpectOk(page, '/billing');
  }
  await expect(page.getByRole('heading', { name: 'Billing' })).toBeVisible();

  await gotoAndExpectOk(page, '/dashboard');
  await expect(page.getByRole('heading', { name: 'Workspace' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Send SMS Blast' })).toBeVisible();
  await expect(page.getByLabel('Message')).toBeVisible();
  await expect(page.locator('.setup-steps')).toHaveCount(0);
  await expect(page.locator('.workspace-summary__meta')).toHaveCount(0);
  await expect(page.locator('body')).not.toContainText('Sending enabled');
  await expect(page.locator('body')).not.toContainText('Trial active');
  await expect(page.locator('body')).not.toContainText('Subscription active');
});

test('platform admin can inspect the control org without changing live state', async ({ page }) => {
  await login(page, platformUsername, platformPassword, '/platform/login');

  let response = await gotoAndExpectOk(page, '/platform');

  response = await gotoAndExpectOk(page, '/platform/organizations');

  const organization = organizationRow(page, controlOrganizationName);
  await expect(organization).toBeVisible();

  const messagingHref = await organization.locator('a[href*="/messaging"]').filter({ hasText: /Manage provider/i }).first().getAttribute('href');
  expect(messagingHref).toBeTruthy();

  response = await gotoAndExpectOk(page, messagingHref);
  await expect(page.locator('.platform-shell__summary')).toBeVisible();
  await expect(page.getByText(/Platform-managed Twilio|Customer-managed Twilio/).first()).toBeVisible();
  await expect(page.getByLabel('Service Address Line 1')).toBeVisible();

  response = await gotoAndExpectOk(page, `${messagingHref}/onboarding`);
  await expect(page.locator('.platform-shell__summary')).toBeVisible();
  await expect(page.getByText('Registration settings')).toBeVisible();
  await expect(page.getByText('Recent Twilio activity')).toBeVisible();
});
