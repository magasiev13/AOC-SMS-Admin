const { test, expect } = require('@playwright/test');
const {
  acceptInvitation,
  attachFailureDiagnostics,
  completeFakeCheckout,
  expectOrganizationRowState,
  fillOwnerSetupCompliance,
  installFailureDiagnostics,
  login,
  organizationRow,
  startFakeCheckoutFromSetup,
  submitOwnerOnboarding,
} = require('./helpers');

test.describe.configure({ mode: 'serial' });

test.beforeEach(async ({ page }) => {
  installFailureDiagnostics(page);
});

test.afterEach(async ({ page }, testInfo) => {
  await attachFailureDiagnostics(page, testInfo);
});

test('golden owner journey covers signup billing onboarding staff invite and platform review', async ({ page }) => {
  const organizationName = 'Golden Path Bakery';
  const ownerEmail = 'golden-owner@browser.test';

  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.goto('/signup');
  await page.getByLabel('Business name').fill(organizationName);
  await page.getByLabel('Full name').fill('Golden Owner');
  await page.getByLabel('Business email').fill(ownerEmail);
  await page.getByLabel('Username').fill('golden-owner');
  await page.getByLabel('Mobile phone').fill('+15550001999');
  await page.getByLabel('Password', { exact: true }).fill('GoldenOwner-pass1!');
  await page.getByLabel('Confirm password').fill('GoldenOwner-pass1!');
  await page.getByRole('button', { name: 'Create workspace' }).click();

  await expect(page).toHaveURL(/\/setup$/);
  await expect(page.getByRole('heading', { name: organizationName })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Activate billing' })).toBeVisible();

  await startFakeCheckoutFromSetup(page);
  await expect(page.getByText('Browser-only Stripe stub')).toBeVisible();
  await completeFakeCheckout(page);
  await expect(page.locator('.setup-pill').filter({ hasText: 'Trial active' })).toBeVisible();
  await expect(page.getByText('Billing is active and sending is unlocked during the trial.')).toBeVisible();

  await fillOwnerSetupCompliance(page, {
    organizationName,
    businessEmail: ownerEmail,
  });
  await submitOwnerOnboarding(page);
  await expect(page.getByRole('heading', { name: 'Await approval and sender assignment' })).toBeVisible();

  await page.goto('/dashboard');
  await expect(page.getByText('Live SMS is paused.')).toBeVisible();
  await expect(page.getByText(/Submitted to Twilio|Carrier review in progress/).first()).toBeVisible();
  await expect(page.locator('#sendBtn')).toBeDisabled();

  await page.goto('/team/invite');
  await expect(page.getByRole('heading', { name: 'Invite a Team Member' })).toBeVisible();
  await page.getByLabel('Email').fill('golden-staff@browser.test');
  await page.getByLabel('Role').selectOption('staff');
  await page.getByRole('button', { name: 'Send Invite' }).click();

  await expect(page.getByText('Team invitation created.')).toBeVisible();
  const staffInviteHref = await page.getByRole('link', { name: 'Open invite' }).first().getAttribute('href');
  expect(staffInviteHref).toBeTruthy();

  await acceptInvitation(page, {
    invitePath: staffInviteHref,
    fullName: 'Golden Staff',
    username: 'golden-staff',
    phone: '+15550001990',
    password: 'GoldenStaff-pass1!',
    expectedUrl: /\/dashboard$/,
  });
  await expect(page.getByRole('heading', { name: 'Workspace' })).toBeVisible();

  let response = await page.goto('/billing');
  expect(response).not.toBeNull();
  expect(response.status()).toBe(403);

  response = await page.goto('/platform/organizations');
  expect(response).not.toBeNull();
  expect(response.status()).toBe(403);

  await login(page, 'platform@browser.test', 'Platform-pass1!');
  await page.goto('/platform/organizations');
  const goldenRow = organizationRow(page, organizationName);
  await expectOrganizationRowState(goldenRow, {
    headlinePattern: /Workspace ready while SMS approval is pending/,
    billingTitle: 'Trial active',
    messagingTitle: 'Pending',
  });
  await expect(goldenRow.getByRole('link', { name: 'Manage provider' })).toBeVisible();
});

test('platform owner invite journey lands invited owners on setup', async ({ page }) => {
  await login(page, 'platform@browser.test', 'Platform-pass1!');
  await page.goto('/platform/organizations');

  const onboardingRow = organizationRow(page, 'Onboarding Bakery');
  await expectOrganizationRowState(onboardingRow, {
    headlinePattern: /core steps complete/,
    billingTitle: 'Billing setup needed',
    messagingTitle: 'Pending',
    ownerInviteVisible: true,
    ownerInviteToken: /browser-owner-invite-token/,
  });

  const ownerInviteHref = await onboardingRow.getByRole('link', { name: 'Open invite' }).getAttribute('href');
  expect(ownerInviteHref).toBeTruthy();

  await acceptInvitation(page, {
    invitePath: ownerInviteHref,
    fullName: 'Onboarding Owner',
    username: 'onboarding-owner',
    phone: '+15550001993',
    password: 'OnboardingOwner-pass1!',
    expectedUrl: /\/setup$/,
  });

  await expect(page.getByRole('heading', { name: 'Onboarding Bakery' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Activate billing' })).toBeVisible();
});

test('blocked states and tenant isolation hold across seeded organizations', async ({ page }) => {
  await login(page, 'pending-a2p-owner@browser.test', 'PendingA2P-pass1!');
  await page.goto('/dashboard');
  await expect(page.getByText(/Carrier review in progress/)).toBeVisible();
  await expect(page.getByText('Live SMS is paused.')).toBeVisible();
  await expect(page.locator('#sendBtn')).toBeDisabled();

  await login(page, 'past-due-owner@browser.test', 'PastDue-pass1!');
  await page.goto('/billing');
  await expect(page.locator('.billing-summary-card .badge').filter({ hasText: 'Payment issue' })).toBeVisible();
  await expect(page.locator('.billing-summary-card .billing-summary-chip').filter({ hasText: 'Sending paused' })).toBeVisible();
  await page.goto('/dashboard');
  await expect(page.getByText('Live SMS is paused.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Activate Subscription' })).toBeVisible();

  await page.goto('/login');
  await page.getByLabel('Email or username').fill('suspended-owner@browser.test');
  await page.getByLabel('Password').fill('Suspended-pass1!');
  await page.getByRole('button', { name: /Sign in to/i }).click();
  await expect(page.getByText('Your organization is currently suspended. Contact your platform admin.')).toBeVisible();

  await login(page, 'isolation-owner@browser.test', 'Isolation-pass1!');
  await page.goto('/events');
  await expect(page.getByRole('link', { name: 'Northstar Bootcamp' }).first()).toBeVisible();
  const isolationEventHref = await page.getByRole('link', { name: 'Northstar Bootcamp' }).first().getAttribute('href');
  expect(isolationEventHref).toBeTruthy();

  await login(page, 'owner@browser.test', 'Owner-pass1!');
  await page.goto('/events');
  await expect(page.getByRole('link', { name: 'Acme Spring Launch' }).first()).toBeVisible();
  await expect(page.getByRole('link', { name: 'Northstar Bootcamp' })).toHaveCount(0);
  const isolationResponse = await page.goto(isolationEventHref);
  expect(isolationResponse).not.toBeNull();
  expect(isolationResponse.status()).toBe(404);
});

test('mobile sanity covers setup billing and platform primary actions', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });

  await login(page, 'trial-owner@browser.test', 'TrialOwner-pass1!');
  await page.goto('/setup?step=billing');
  await expect(page.getByRole('button', { name: 'Start subscription' })).toBeVisible();
  await startFakeCheckoutFromSetup(page);
  await page.getByRole('button', { name: 'Cancel' }).click();
  await expect(page).toHaveURL(/\/setup\?step=billing$/);

  await login(page, 'owner@browser.test', 'Owner-pass1!');
  await page.goto('/billing');
  await expect(page.getByRole('heading', { name: 'Billing' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Update Subscription' })).toBeVisible();

  await login(page, 'platform@browser.test', 'Platform-pass1!');
  await page.goto('/platform/organizations');
  await expect(page.locator('.card-list-item.platform-org-card').first()).toBeVisible();
  await expect(page.getByRole('link', { name: 'Add Organization' })).toBeVisible();
});
