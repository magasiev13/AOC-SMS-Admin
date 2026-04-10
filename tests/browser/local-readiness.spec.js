const { test, expect } = require('@playwright/test');
const {
  attachFailureDiagnostics,
  expectOrganizationRowState,
  installFailureDiagnostics,
  login,
  organizationRow,
} = require('./helpers');

test.beforeEach(async ({ page }) => {
  installFailureDiagnostics(page);
});

test.afterEach(async ({ page }, testInfo) => {
  await attachFailureDiagnostics(page, testInfo);
});

test('platform admin can review onboarding progress and owner invite access', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  await login(page, 'platform@browser.test', 'Platform-pass1!');
  await expect(page.locator('.app-page-title')).toHaveText('Platform');
  await expect(page.getByRole('link', { name: 'Platform' })).toBeVisible();
  await expect(page.locator('.app-nav .app-nav-link').filter({ hasText: /^Send$/ })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Restart SaaS Services' })).toBeVisible();
  await page.getByRole('button', { name: 'Restart SaaS Services' }).click();
  await expect(
    page.locator('.card-body .small.mt-3').filter({ hasText: 'Restart request queued. Waiting for the host processor.' }),
  ).toBeVisible();
  await expect(page.getByText('Last request: Queued')).toBeVisible();
  await page.getByRole('button', { name: 'Restart SaaS Services' }).click();
  await expect(page.getByText('Last request: Queued')).toBeVisible();

  const organizationsNavLink = page.locator('.app-nav a[href="/platform/organizations"]').first();
  await expect(organizationsNavLink).toBeVisible();
  await organizationsNavLink.click();

  const onboardingRow = organizationRow(page, 'Onboarding Bakery');
  await expectOrganizationRowState(onboardingRow, {
    headlinePattern: /core steps complete/,
    billingTitle: 'Billing setup needed',
    messagingTitle: 'Pending',
    ownerInviteVisible: true,
    ownerInviteToken: /browser-owner-invite-token/,
  });
  await expect(onboardingRow.getByRole('link', { name: 'Manage provider' })).toBeVisible();

  await page.getByRole('link', { name: 'Add Organization' }).click();
  await expect(page.getByRole('heading', { name: 'Create Business Account' })).toBeVisible();
  await expect(page.getByText(/Platform-managed Twilio by default/)).toBeVisible();
  await expect(page.getByText(/Twilio subaccounts and messaging services are provisioned later/)).toBeVisible();
  await expect(page.getByLabel('Initial Role')).toHaveCount(0);
});

test('owner sees human-readable billing state and pending invite links', async ({ page }) => {
  await login(page, 'owner@browser.test', 'Owner-pass1!');
  await expect(page.getByRole('heading', { name: 'Workspace' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Organizations' })).toHaveCount(0);
  await page.goto('/billing');

  await expect(page.locator('.badge').filter({ hasText: 'Trial active' }).first()).toBeVisible();
  await expect(page.getByText(/Ready for owner/)).toBeVisible();
  await expect(page.getByText('Sending enabled')).toBeVisible();

  await page.goto('/users');
  await expect(page.getByText('Pending Invitations')).toBeVisible();
  const staffInviteLink = page.getByRole('link', { name: 'Open invite' }).first();
  await expect(staffInviteLink).toHaveAttribute('href', /browser-staff-invite-token/);
});

test('trial owner is returned to billing overview on checkout GET', async ({ page }) => {
  await login(page, 'trial-owner@browser.test', 'TrialOwner-pass1!');
  await page.goto('/billing/checkout');

  await expect(page).toHaveURL(/\/billing$/);
  await expect(page.getByRole('heading', { name: 'Billing' })).toBeVisible();
});

test('staff is blocked from billing', async ({ page }) => {
  await login(page, 'staff@browser.test', 'Staff-pass1!');
  const response = await page.goto('/billing');

  expect(response).not.toBeNull();
  expect(response.status()).toBe(403);
});
