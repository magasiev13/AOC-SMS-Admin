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
  await expect(page.locator('.setup-panel').first().getByRole('heading', { name: heading })).toBeVisible();

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
  await expect(page.getByRole('link', { name: 'Request pilot access' })).toBeVisible();
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
  await expect(page.getByText('Owner Setup')).toBeVisible();
  await expectSetupShell(page, { step: 'billing', heading: 'Activate Billing' });
  await expect(page.getByRole('heading', { name: 'Setup Runway Bakery' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Start Subscription' })).toBeVisible();
});

test('platform-managed owner setup preserves compliance and review structure on desktop', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  await login(page, 'trial-owner@browser.test', 'TrialOwner-pass1!');

  await page.goto('/setup?step=compliance');
  await expectSetupShell(page, { step: 'compliance', heading: 'Organization Details' });
  await expect(page.locator('form[data-organization-setup]')).toBeVisible();
  await expect(page.locator('[data-organization-step]')).toHaveCount(5);
  await expect(page.locator('[data-organization-step="organization"]')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Continue', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Save Organization Details' })).toBeHidden();
  await expect(page.locator('[data-organization-step="signup"]')).toContainText('Current Submission Source');

  await page.goto('/setup?step=review');
  await expectSetupShell(page, { step: 'review', heading: 'Review and Submit' });
  await expect(page.getByRole('link', { name: 'Edit Organization Details' })).toBeVisible();
  await expect(page.locator('input[name="declaration_accepted"]')).toHaveCount(1);
});

test('platform-managed owner launch wait state keeps the setup shell on desktop', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  await login(page, 'pending-a2p-owner@browser.test', 'PendingA2P-pass1!');

  await page.goto('/setup');
  await expectSetupShell(page, { step: 'launch', heading: /review|approval/i });
  await expect(page.locator('[data-setup-technical-details]')).not.toHaveAttribute('open');
  await page.locator('[data-setup-technical-details] > summary').click();
  await expect(page.getByText('Messaging Readiness', { exact: true })).toBeVisible();
  await expect(page.getByText(/Recent Twilio Activity/i)).toBeVisible();
  await expect(page.getByRole('button', { name: 'Refresh Status' })).toBeVisible();
  await page.getByRole('link', { name: 'Open Workspace', exact: true }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.getByText('Live SMS is paused.')).toBeVisible();
  await expect(page.locator('#sendBtn')).toBeDisabled();
});

test('customer-managed owner with pending activation lands on the read-only setup runway', async ({ page }) => {
  await login(page, 'customer-managed-owner@browser.test', 'CustomerManaged-pass1!');

  await expect(page).toHaveURL(/\/setup$/);
  await expect(page.getByText('Owner Setup')).toBeVisible();
  await expectSetupShell(page, { step: 'provider', heading: 'Connect Your Messaging Account' });
  await expect(page.getByRole('heading', { name: 'Customer Managed Bakery' })).toBeVisible();
  await expect(page.getByText('What Happens Next')).toBeVisible();
  await expect(page.locator('body')).not.toContainText('Legal business name');
  await expect(page.locator('input[name="action"][value="submit_onboarding"]')).toHaveCount(0);
});

test('customer-managed owner with active messaging lands in the workspace', async ({ page }) => {
  await login(page, 'customer-managed-ready@browser.test', 'CustomerManagedReady-pass1!');

  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.getByRole('heading', { name: 'Send a Message' })).toBeVisible();
});

test('live setup launch state keeps the open-workspace action visible', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  await login(page, 'customer-managed-ready@browser.test', 'CustomerManagedReady-pass1!');

  await page.goto('/setup');
  await expectSetupShell(page, { step: 'launch', heading: 'Messaging Is Ready' });
  await expect(page.getByRole('link', { name: 'Open Workspace' })).toBeVisible();
});

test('mobile setup shows billing disclosures before checkout and keeps activation actions reachable', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });

  await login(page, 'trial-owner@browser.test', 'TrialOwner-pass1!');
  await page.goto('/setup?step=billing');
  await expectSetupShell(page, { step: 'billing', heading: 'Activate Billing' });
  const facts = page.locator('.setup-panel .billing-plan-facts');
  await expect(facts).toBeVisible();
  await expect(facts.getByText('$149.99 setup fee due at checkout', { exact: true })).toBeVisible();
  await expect(facts.getByText('$0.03 per segment', { exact: true })).toBeVisible();
  const checkout = page.getByRole('button', { name: 'Start Subscription' });
  expect((await box(facts)).y + (await box(facts)).height).toBeLessThan((await box(checkout)).y);
  await checkout.scrollIntoViewIfNeeded();
  await expect(checkout).toBeInViewport();

  await login(page, 'customer-managed-owner@browser.test', 'CustomerManaged-pass1!');
  await page.goto('/setup');
  await expectSetupShell(page, { step: 'provider', heading: 'Connect Your Messaging Account' });
  await expectWithinFirstViewport(page, page.getByRole('link', { name: 'View Billing' }));

  await login(page, 'customer-managed-ready@browser.test', 'CustomerManagedReady-pass1!');
  await page.goto('/setup');
  await expectSetupShell(page, { step: 'launch', heading: 'Messaging Is Ready' });
  await expectWithinFirstViewport(page, page.getByRole('link', { name: 'Open Workspace' }));
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
  await expect(page.locator('.setup-steps')).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Open Your Workspace' })).toBeVisible();
  await expect(page.getByText('Your workspace is available. Live messaging is waiting for setup to finish.')).toBeVisible();
  await expect(page.getByText('You do not need to change setup settings. You can use the workspace now, or check whether live messaging is ready.')).toBeVisible();
  await expect(page.locator('body')).not.toContainText('The owner is still finishing billing or compliance.');
  await expect(page.locator('body')).not.toContainText('Billing status:');
  await expect(page.locator('.setup-panel form')).toHaveCount(0);
  await expect(page.locator('body')).not.toContainText('Legal business name');
  await expect(page.locator('input[name="action"][value="submit_onboarding"]')).toHaveCount(0);
  await page.getByRole('link', { name: 'Check Setup Status', exact: true }).click();
  await expect(page).toHaveURL(/\/setup\/pending$/);
  await page.getByRole('link', { name: 'Open Workspace', exact: true }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.locator('#sendBtn')).toBeDisabled();
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
  await expect(page.locator('.setup-steps')).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Open Your Workspace' })).toBeVisible();
  await expect(page.getByText('Your workspace is available. Live messaging is waiting for setup to finish.')).toBeVisible();
  await expect(page.getByText('You do not need to change setup settings. You can use the workspace now, or check whether live messaging is ready.')).toBeVisible();
  await expect(page.locator('body')).not.toContainText('customer-managed Twilio connection');
  await expect(page.locator('body')).not.toContainText('External messaging:');
  await expect(page.locator('body')).not.toContainText('Billing status:');
  await expect(page.locator('.setup-panel form')).toHaveCount(0);
  await expect(page.locator('body')).not.toContainText('Legal business name');
  await expect(page.locator('input[name="action"][value="submit_onboarding"]')).toHaveCount(0);
  await page.getByRole('link', { name: 'Check Setup Status', exact: true }).click();
  await expect(page).toHaveURL(/\/setup\/pending$/);
  await page.getByRole('link', { name: 'Open Workspace', exact: true }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.locator('#sendBtn')).toBeDisabled();
});

test('managed pilot replaces self-serve signup with a resource-safe application', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/signup');

  await expect(page).toHaveURL(/\/request-a-pilot$/);
  await expect(page.locator('body')).not.toContainText(/SMS Admin|Twinevia Legacy/);
  await expect(page.getByRole('heading', { name: 'Tell us what the messages need to accomplish.' })).toBeVisible();
  await expect(page.getByText('This is an application for a managed pilot—not instant signup.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Submit pilot request' })).toBeVisible();

  const requestMetrics = await surfaceMetrics(page);
  expect(requestMetrics.scrollWidth).toBeLessThanOrEqual(requestMetrics.innerWidth);

  await page.getByLabel('Business or organization name').fill('Signup Browser Bakery');
  await page.getByLabel('Full name').fill('Signup Owner');
  await page.getByLabel('Business email').fill('signup-owner@browser.test');
  await page.getByLabel('Phone optional').fill('+15550001999');
  await page.getByLabel('What will you send, to whom, and how did they consent?').fill(
    'Customers opt in at checkout for requested pickup reminders and can reply STOP at any time.'
  );
  await page.getByLabel('Expected outbound segments per month optional').fill('350');
  await page.getByLabel('Current Twilio setup optional').selectOption('none');
  await page.getByRole('button', { name: 'Submit pilot request' }).click();

  await expect(page).toHaveURL(/\/request-a-pilot\?submitted=1$/);
  await expect(page.getByRole('heading', { name: 'Your pilot request is in the review queue.' })).toBeVisible();
  await expect(page.getByText('without creating an organization, billing account, sending number, or provider resource')).toBeVisible();
});
