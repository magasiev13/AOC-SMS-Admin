const { test, expect } = require('@playwright/test');
const { login, acceptInvitation, installFailureDiagnostics, attachFailureDiagnostics } = require('./helpers');

test.beforeEach(async ({ page }) => installFailureDiagnostics(page));
test.afterEach(async ({ page }, info) => attachFailureDiagnostics(page, info));

async function statusFor(page) {
  // Use the browser's existing session and User-Agent. A separate API client
  // would correctly trigger this application's strong session protection.
  return page.evaluate(async () => {
    const response = await fetch('/setup/status', { credentials: 'same-origin' });
    if (!response.ok || response.redirected) throw new Error('Setup status was not available to this session.');
    return response.json();
  });
}

test('unchanged waiting status keeps the page even when another setup step is selected', async ({ page }) => {
  await login(page, 'customer-managed-owner@browser.test', 'CustomerManaged-pass1!');
  const original = await statusFor(page);
  let checks = 0;
  await page.route('**/setup/status', async route => {
    checks += 1;
    await route.fulfill({ json: original });
  });
  await page.clock.install();
  await page.goto('/setup?step=launch');
  await page.evaluate(() => { window.setupVisit = 'retained'; });
  let navigations = 0;
  page.on('framenavigated', frame => { if (frame === page.mainFrame()) navigations += 1; });
  await page.clock.fastForward(8100);
  await expect.poll(() => checks).toBe(1);
  await page.clock.fastForward(8100);
  await expect.poll(() => checks).toBe(2);
  expect(await page.evaluate(() => window.setupVisit)).toBe('retained');
  expect(navigations).toBe(0);
  await expect(page.locator('[data-setup-status-update]')).toBeHidden();
  await expect(page.locator('[data-setup-technical-details]')).not.toHaveAttribute('open');
});

test('a changed status waits while a real setup form has focus', async ({ page }) => {
  await login(page, 'pending-a2p-owner@browser.test', 'PendingA2P-pass1!');
  const original = await statusFor(page);
  const changed = { ...original, onboarding: { ...original.onboarding, last_error: 'Updated review information' } };
  let checks = 0;
  await page.route('**/setup/status', async route => {
    checks += 1;
    await route.fulfill({ json: changed });
  });
  await page.clock.install();
  await page.goto('/setup');
  await page.evaluate(() => { window.setupVisit = 'retained'; });
  const refreshForm = page.locator('form').filter({ has: page.locator('input[value="refresh_onboarding"]') });
  await refreshForm.getByRole('button', { name: 'Refresh Status', exact: true }).focus();
  await page.clock.fastForward(8100);
  await expect.poll(() => checks).toBe(1);
  await expect(page.locator('[data-setup-status-message]')).toHaveText(/Setup status changed/);
  expect(await page.evaluate(() => window.setupVisit)).toBe('retained');
  await expect(refreshForm.getByRole('button', { name: 'Refresh Status', exact: true })).toBeFocused();
  await expect(page.locator('[data-setup-status-message]')).toHaveAttribute('aria-live', 'polite');
  await Promise.all([
    page.waitForEvent('framenavigated', frame => frame === page.mainFrame()),
    page.locator('[data-setup-status-refresh]').click(),
  ]);
  await page.waitForLoadState('load');
  expect(await page.evaluate(() => window.setupVisit)).toBeUndefined();
});

test('an idle waiting page refreshes when setup status actually changes', async ({ page }) => {
  await login(page, 'customer-managed-owner@browser.test', 'CustomerManaged-pass1!');
  const original = await statusFor(page);
  await page.route('**/setup/status', route => route.fulfill({ json: {
    ...original, onboarding: { ...original.onboarding, last_error: 'New connection information' },
  } }));
  await page.clock.install();
  await page.goto('/setup');
  await page.evaluate(() => { window.setupVisit = 'first'; });
  await Promise.all([
    page.waitForEvent('framenavigated', frame => frame === page.mainFrame()),
    page.clock.fastForward(8100),
  ]);
  await page.waitForLoadState('load');
  expect(await page.evaluate(() => window.setupVisit)).toBeUndefined();
  await expect(page.locator('[data-setup-shell]')).toBeVisible();
});

test('editing organization details and review never polls or discards work', async ({ page }) => {
  await login(page, 'pending-a2p-owner@browser.test', 'PendingA2P-pass1!');
  let checks = 0;
  await page.route('**/setup/status', route => {
    checks += 1;
    return route.fulfill({ json: {} });
  });
  await page.clock.install();
  await page.goto('/setup?step=compliance');
  await page.getByLabel('Legal business name', { exact: true }).fill('Unsaved Organization Details');
  await page.clock.fastForward(17000);
  await expect(page.getByLabel('Legal business name', { exact: true })).toHaveValue('Unsaved Organization Details');
  expect(checks).toBe(0);
  await page.goto('/setup?step=review');
  await page.locator('input[name="declaration_accepted"]').check();
  await page.clock.fastForward(17000);
  await expect(page.locator('input[name="declaration_accepted"]')).toBeChecked();
  expect(checks).toBe(0);
});

test('staff waiting screen offers a working workspace path and a setup recheck', async ({ page }) => {
  await login(page, 'pending-a2p-owner@browser.test', 'PendingA2P-pass1!');
  await page.goto('/team/invite');
  await page.getByLabel('Email', { exact: true }).fill('setup-status-staff@browser.test');
  await page.getByLabel('Role', { exact: true }).selectOption('staff');
  await page.getByRole('button', { name: /Send Invite/i }).click();
  const row = page.locator('tr, .card-list-item').filter({ hasText: 'setup-status-staff@browser.test' }).first();
  const path = await row.getByRole('link', { name: /Open Invite/i }).getAttribute('href');
  await acceptInvitation(page, { invitePath: path, fullName: 'Setup Status Staff', username: 'setup-status-staff',
    phone: '+15550002998', password: 'SetupStatus-pass1!', expectedUrl: /\/setup\/pending$/ });
  await expect(page.getByText('Your workspace is available. Live messaging is waiting for setup to finish.')).toBeVisible();
  await page.getByRole('link', { name: 'Check Setup Status', exact: true }).click();
  await expect(page).toHaveURL(/\/setup\/pending$/);
  await expect(page.getByRole('link', { name: 'Help & Getting Started', exact: true })).toHaveAttribute('href', '/help');
  await page.getByRole('link', { name: 'Open Workspace', exact: true }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.getByText('Live SMS is paused.', { exact: false })).toBeVisible();
  await expect(page.locator('#sendForm button[type="submit"]')).toBeDisabled();
});
