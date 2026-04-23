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
  await expect(page.locator('.platform-home-summary-strip')).toBeVisible();
  await expect(page.locator('.platform-home-kicker')).toHaveCount(0);
  await expect(page.getByText('Manage organizations, onboarding, and provider readiness.')).toHaveCount(0);
  await expect(page.getByText('Use this workspace to create business accounts, review onboarding blockers, manage platform access, and finish provider setup.')).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Needs attention' })).toBeVisible();
  await expect(page.locator('[aria-labelledby="platform-home-needs-attention"]').getByRole('link', { name: 'Organizations' })).toHaveCount(0);
  const firstWorklistItem = page.locator('.platform-home-worklist__item').first();
  await expect(firstWorklistItem).toBeVisible();
  await expect(firstWorklistItem.locator('.platform-home-worklist__headline')).toHaveText('Open the billing portal and resolve the payment issue.');
  await expect(firstWorklistItem.locator('.platform-home-worklist__support')).toHaveText('3/6 core steps · Billing: Payment issue · Messaging: Suspended');
  await expect(firstWorklistItem.getByText(/^3\/6 core steps complete$/)).toHaveCount(0);
  await expect(firstWorklistItem.getByRole('link', { name: 'Access' })).toBeVisible();
  await expect(firstWorklistItem.getByRole('link', { name: /Messaging|Set up provider/ })).toBeVisible();
  await expect(page.locator('.platform-home-utilities')).toBeVisible();
  await expect(page.locator('.platform-home-utilities').getByRole('link', { name: 'Organizations' })).toBeVisible();
  await expect(page.locator('.platform-home-utilities').getByRole('link', { name: 'Users' })).toBeVisible();
  await expect(page.locator('.platform-home-utilities').getByRole('link', { name: 'Security Events' })).toBeVisible();
  const recentSuspendedItem = page.locator('.platform-home-recent-item').filter({ hasText: 'Suspended Bakery' }).first();
  await expect(recentSuspendedItem).toBeVisible();
  await expect(recentSuspendedItem.locator('.platform-home-recent-item__meta')).toHaveText(
    'Open the billing portal and resolve the payment issue.',
  );
  await expect(recentSuspendedItem.getByText(/^3\/6 core steps complete$/)).toHaveCount(0);
  await expect(page.getByText('Open Organization Directory')).toHaveCount(0);
  await expect(page.getByText('Review Users')).toHaveCount(0);
  await expect(page.getByText('Review Security Events')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Restart SaaS Services' })).toBeVisible();
  await page.getByRole('button', { name: 'Restart SaaS Services' }).click();
  await expect(
    page.locator('.platform-home-status').getByText('Restart request queued. Waiting for the host processor.'),
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
  await expect(onboardingRow.getByRole('link', { name: /Messaging|Set up provider/ })).toBeVisible();

  await page.getByRole('link', { name: 'Add Organization' }).click();
  await expect(page.getByRole('heading', { name: 'Create Business Account' })).toBeVisible();
  await expect(page.getByText('Create the next business account and keep telecom provisioning on the managed path.')).toHaveCount(0);
  await expect(page.getByText('Name the workspace and send the first owner invite.')).toBeVisible();
  await expect(page.getByText(/Platform-managed Twilio by default/)).toBeVisible();
  await expect(page.getByText(/Twilio subaccounts and messaging services are provisioned later/)).toBeVisible();
  await expect(page.getByLabel('Initial Role')).toHaveCount(0);
});

test('owner sees human-readable billing state and pending invite links', async ({ page }) => {
  await login(page, 'owner@browser.test', 'Owner-pass1!');
  await expect(page.getByRole('heading', { name: 'Workspace' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Organizations' })).toHaveCount(0);
  await page.getByRole('link', { name: 'Billing' }).first().click();
  await expect(page).toHaveURL(/\/billing$/);

  await expect(page.locator('.badge').filter({ hasText: 'Trial active' }).first()).toBeVisible();
  await expect(page.locator('.workspace-summary__stat-meta').filter({ hasText: /Ready for owner/ }).first()).toBeVisible();
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
