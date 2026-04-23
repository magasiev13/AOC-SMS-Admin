const { test, expect } = require('@playwright/test');
const {
  attachFailureDiagnostics,
  acceptInvitation,
  completeFakeCheckout,
  fillOwnerSetupCompliance,
  installFailureDiagnostics,
  login,
  startFakeCheckoutFromSetup,
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

async function expectWithinFirstViewport(page, locator) {
  const bounds = await box(locator);
  const metrics = await surfaceMetrics(page);
  expect(bounds.y + bounds.height).toBeLessThanOrEqual(metrics.innerHeight);
}

async function expectSetupShell(page, { step, heading }) {
  await expect(page.locator('.setup-shell')).toBeVisible();
  await expect(page.locator('.setup-hero')).toBeVisible();
  await expect(page.locator('.setup-layout')).toBeVisible();
  await expect(page.locator('.setup-steps')).toBeVisible();
  await expect(page.locator('.setup-step.is-current')).toHaveCount(1);
  await expect(page.locator('.setup-panel').first()).toBeVisible();
  await expect(page.getByRole('heading', { name: heading })).toBeVisible();

  const setupShell = page.locator('[data-setup-shell]');
  if (await setupShell.count()) {
    await expect(setupShell).toHaveAttribute('data-current-step', step);
  }

  const metrics = await surfaceMetrics(page);
  expect(metrics.scrollWidth).toBeLessThanOrEqual(metrics.innerWidth);
}

async function createPendingStaffInvite(page, { email, username, password, ownerUsername, ownerPassword }) {
  await login(page, ownerUsername, ownerPassword);
  await page.goto('/team/invite');
  await page.getByLabel('Email').fill(email);
  await page.getByLabel('Role').selectOption('staff');
  await page.getByRole('button', { name: 'Send Invite' }).click();
  await expect(page.getByText('Team invitation created.')).toBeVisible();

  const inviteRow = page.locator('tr, .card-list-item').filter({ hasText: email }).first();
  const inviteHref = await inviteRow.getByRole('link', { name: 'Open invite' }).getAttribute('href');
  expect(inviteHref).toBeTruthy();

  await acceptInvitation(page, {
    invitePath: inviteHref,
    fullName: `${username} User`,
    username,
    phone: '+15550002999',
    password,
    expectedUrl: /\/setup\/pending$/,
  });
}

test('workspace and platform login surfaces are clearly separated', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.goto('/login');
  await expect(page.getByText('Workspace access')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Send messages and review replies in one workspace.' })).toBeVisible();
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
  await expectSetupShell(page, { step: 'billing', heading: 'Activate billing' });
  await expect(page.getByRole('heading', { name: 'Setup Runway Bakery' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Start subscription' })).toBeVisible();
});

test('platform-managed owner setup preserves compliance and review structure on desktop', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  await login(page, 'trial-owner@browser.test', 'TrialOwner-pass1!');

  await page.goto('/setup?step=compliance');
  await expectSetupShell(page, { step: 'compliance', heading: 'Business profile and compliance' });
  await expect(page.locator('form.setup-form-grid')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Save business profile' })).toBeVisible();
  await expect(page.getByText('Current submission source')).toBeVisible();

  await page.goto('/setup?step=review');
  await expectSetupShell(page, { step: 'review', heading: 'Review and submit' });
  await expect(page.getByRole('link', { name: 'Edit business profile' })).toBeVisible();
  await expect(page.locator('input[name="declaration_accepted"]')).toHaveCount(1);
});

test('platform-managed owner launch wait state keeps the setup shell on desktop', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  await login(page, 'pending-a2p-owner@browser.test', 'PendingA2P-pass1!');

  await page.goto('/setup');
  await expectSetupShell(page, { step: 'launch', heading: 'Await Twilio review' });
  await expect(page.getByText('Launch readiness')).toBeVisible();
  await expect(page.getByText('Recent Twilio activity')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Refresh status' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Open workspace' })).toHaveCount(0);
});

test('customer-managed owner with pending activation lands on the read-only setup runway', async ({ page }) => {
  await login(page, 'customer-managed-owner@browser.test', 'CustomerManaged-pass1!');

  await expect(page).toHaveURL(/\/setup$/);
  await expect(page.getByText('Owner setup')).toBeVisible();
  await expectSetupShell(page, { step: 'provider', heading: 'External Twilio activation' });
  await expect(page.getByRole('heading', { name: 'Customer Managed Bakery' })).toBeVisible();
  await expect(page.getByText('Workspace owners are read-only here')).toBeVisible();
  await expect(page.locator('body')).not.toContainText('Legal business name');
  await expect(page.locator('body')).not.toContainText('Submit for Twilio review');
});

test('customer-managed owner with active messaging lands in the workspace', async ({ page }) => {
  await login(page, 'customer-managed-ready@browser.test', 'CustomerManagedReady-pass1!');

  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.getByRole('heading', { name: 'Workspace' })).toBeVisible();
});

test('live setup launch state keeps the open-workspace action visible', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  await login(page, 'customer-managed-ready@browser.test', 'CustomerManagedReady-pass1!');

  await page.goto('/setup');
  await expectSetupShell(page, { step: 'launch', heading: 'Workspace is live' });
  await expect(page.getByRole('link', { name: 'Open workspace' })).toBeVisible();
});

test('mobile setup keeps the first primary action in the first viewport across billing provider and live states', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });

  await login(page, 'trial-owner@browser.test', 'TrialOwner-pass1!');
  await page.goto('/setup?step=billing');
  await expectSetupShell(page, { step: 'billing', heading: 'Activate billing' });
  await expectWithinFirstViewport(page, page.getByRole('button', { name: 'Start subscription' }));

  await login(page, 'customer-managed-owner@browser.test', 'CustomerManaged-pass1!');
  await page.goto('/setup');
  await expectSetupShell(page, { step: 'provider', heading: 'External Twilio activation' });
  await expectWithinFirstViewport(page, page.getByRole('link', { name: 'View billing' }));

  await login(page, 'customer-managed-ready@browser.test', 'CustomerManagedReady-pass1!');
  await page.goto('/setup');
  await expectSetupShell(page, { step: 'launch', heading: 'Workspace is live' });
  await expectWithinFirstViewport(page, page.getByRole('link', { name: 'Open workspace' }));
});

test('platform-managed staff pending setup stays read-only and points to owner setup', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1100 });

  await createPendingStaffInvite(page, {
    email: 'setup-pending-staff@browser.test',
    username: 'setup-pending-staff',
    password: 'PendingStaff-pass1!',
    ownerUsername: 'pending-a2p-owner@browser.test',
    ownerPassword: 'PendingA2P-pass1!',
  });

  await expect(page.locator('.setup-shell')).toBeVisible();
  await expect(page.locator('.setup-steps')).toBeVisible();
  await expect(page.locator('.setup-step.is-current')).toHaveCount(1);
  await expect(page.getByRole('heading', { name: 'We’ll unlock the workspace automatically.' })).toBeVisible();
  await expect(page.getByText('The owner is still finishing billing or compliance.')).toBeVisible();
  await expect(page.locator('.setup-panel form')).toHaveCount(0);
  await expect(page.locator('body')).not.toContainText('Legal business name');
  await expect(page.locator('body')).not.toContainText('Submit for Twilio review');
});

test('customer-managed staff pending setup stays read-only and points to external activation', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1100 });

  await createPendingStaffInvite(page, {
    email: 'customer-managed-pending-staff@browser.test',
    username: 'customer-managed-pending-staff',
    password: 'CustomerManagedPending-pass1!',
    ownerUsername: 'customer-managed-owner@browser.test',
    ownerPassword: 'CustomerManaged-pass1!',
  });

  await expect(page.locator('.setup-shell')).toBeVisible();
  await expect(page.locator('.setup-steps')).toBeVisible();
  await expect(page.locator('.setup-step.is-current')).toHaveCount(1);
  await expect(page.getByRole('heading', { name: 'We’ll unlock the workspace automatically.' })).toBeVisible();
  await expect(page.getByText('customer-managed Twilio connection')).toBeVisible();
  await expect(page.getByText('External messaging:')).toBeVisible();
  await expect(page.locator('.setup-panel form')).toHaveCount(0);
  await expect(page.locator('body')).not.toContainText('Legal business name');
  await expect(page.locator('body')).not.toContainText('Submit for Twilio review');
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
  await expectSetupShell(page, { step: 'billing', heading: 'Activate billing' });
  await expect(page.getByRole('heading', { name: 'Signup Browser Bakery' })).toBeVisible();
});
