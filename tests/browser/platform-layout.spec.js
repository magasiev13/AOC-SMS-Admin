const { test, expect } = require('@playwright/test');

async function login(page, username, password) {
  await page.goto('/login');
  await page.getByLabel('Email or username').fill(username);
  await page.getByLabel('Password').fill(password);
  await page.getByRole('button', { name: /Sign in to/i }).click();
}

async function elementHeight(locator) {
  return locator.evaluate((element) => Math.round(element.getBoundingClientRect().height));
}

async function elementTop(locator) {
  return locator.evaluate((element) => Math.round(element.getBoundingClientRect().top));
}

async function elementBottom(locator) {
  return locator.evaluate((element) => Math.round(element.getBoundingClientRect().bottom));
}

async function elementLeft(locator) {
  return locator.evaluate((element) => Math.round(element.getBoundingClientRect().left));
}

async function backgroundColor(locator) {
  return locator.evaluate((element) => getComputedStyle(element).backgroundColor);
}

test('platform admin desktop surfaces use the shared shell and aligned actions', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  await login(page, 'platform@browser.test', 'Platform-pass1!');

  await page.goto('/platform/organizations');
  await expect(page.locator('.platform-shell__summary')).toBeVisible();
  await expect(page.locator('.platform-directory__row').first()).toBeVisible();
  await expect(page.getByText('Manage organizations, onboarding, and platform access from a dedicated control plane.')).toHaveCount(0);
  await expect(page.getByText('Use this directory to spot organizations that still need billing, onboarding, or messaging work.')).toHaveCount(0);

  const pageActionHeight = await elementHeight(page.getByRole('link', { name: 'Add Organization' }));
  expect(pageActionHeight).toBeGreaterThanOrEqual(44);

  const firstRow = page.locator('.platform-directory__row').first();
  await expect(firstRow.getByText('Primary contact:')).toHaveCount(0);
  await expect(firstRow.getByText('Owner status')).toHaveCount(0);

  const accessHref = await firstRow.locator('a[href*="/access"]').first().getAttribute('href');
  const messagingHref = await firstRow.locator('a[href*="/messaging"]').first().getAttribute('href');
  expect(accessHref).toBeTruthy();
  expect(messagingHref).toBeTruthy();

  await expect(firstRow.getByRole('link', { name: 'Access' })).toBeVisible();
  await expect(firstRow.getByRole('link', { name: /Messaging|Set up provider/ })).toBeVisible();
  await expect(firstRow.getByRole('button', { name: 'More actions' })).toBeVisible();
  await expect(firstRow.getByText('Manage Access')).toHaveCount(0);
  await expect(firstRow.getByText('Manage provider')).toHaveCount(0);
  await expect(firstRow.getByText('View checklist')).toHaveCount(0);

  const suspendedRow = page.locator('.platform-directory__row').filter({ hasText: 'Suspended Bakery' }).first();
  await expect(suspendedRow).toBeVisible();
  await expect(suspendedRow.getByText(/^3\/6 core steps complete$/)).toHaveCount(1);

  const checklistSummaries = page.locator('.platform-inline-details summary');
  if (await checklistSummaries.count()) {
    await expect(checklistSummaries.first()).toHaveText('Checklist');
  }

  const primaryActionHeight = await elementHeight(firstRow.locator('.row-actions__primary .btn').first());
  const overflowToggleHeight = await elementHeight(firstRow.locator('.row-actions__overflow-toggle').first());
  expect(Math.abs(primaryActionHeight - overflowToggleHeight)).toBeLessThanOrEqual(1);

  await page.getByRole('link', { name: 'Add Organization' }).click();
  await expect(page.locator('.platform-shell__summary')).toBeVisible();
  await expect(page.getByText('Create the next business account and keep telecom provisioning on the managed path.')).toHaveCount(0);
  await expect(page.getByText('Name the workspace and send the first owner invite.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Create Business Account' })).toBeVisible();
  expect(await elementHeight(page.getByRole('button', { name: 'Create Business Account' }))).toBeGreaterThanOrEqual(44);

  await page.goto(accessHref);
  await expect(page.locator('.platform-shell__summary')).toBeVisible();
  await expect(page.locator('.platform-shell__summary-meta')).toBeVisible();
  await expect(page.getByText('Platform support stays invite-only. Use staff invites for team help, and reissue the owner invite only when initial onboarding is blocked.')).toHaveCount(0);
  await expect(page.locator('.platform-shell__summary-copy')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Create Staff Invite' })).toBeVisible();
  expect(await elementLeft(page.getByRole('button', { name: 'Create Staff Invite' }))).toBeLessThan(
    await elementLeft(page.getByRole('button', { name: 'Grant Complimentary Billing' })),
  );
  expect(await elementHeight(page.getByRole('button', { name: 'Create Staff Invite' }))).toBeGreaterThanOrEqual(44);

  await page.goto(messagingHref);
  await expect(page.locator('.platform-shell__summary')).toBeVisible();
  await expect(page.locator('.platform-shell__summary-meta')).toBeVisible();
  await expect(page.getByText('Provision the platform-managed Twilio account first, then let Twinevia validate the service address, attach the sender, sync emergency registration, and unlock live sending.')).toHaveCount(0);
  await expect(page.getByText('Provision the provider, save the service address, then finalize the sender.')).toBeVisible();
  await expect(page.getByText('Platform-managed Twilio', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Finalize Sender Setup' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Save Provider Settings' })).toBeVisible();
  await expect(page.getByLabel('Service Address Line 1')).toBeVisible();
  await expect(page.getByLabel('Number Strategy')).toBeVisible();
  await expect(page.locator('.platform-key-label', { hasText: 'Emergency address sync' })).toBeVisible();
  expect(await elementTop(page.getByLabel('Service Address Line 1'))).toBeLessThan(780);
  expect(await elementTop(page.getByLabel('Service Address Line 1'))).toBeLessThan(
    await elementTop(page.getByText('Recent Twilio activity').first()),
  );

  await page.getByRole('link', { name: 'Manage A2P Onboarding' }).click();
  await expect(page.locator('.platform-shell__summary')).toBeVisible();
  await expect(page.locator('.platform-shell__summary-meta')).toBeVisible();
  await expect(page.getByText('Submit and monitor the registration package here. Keep messaging setup separate, then return once carrier review is complete.')).toHaveCount(0);
  await expect(page.getByText('Submit the registration packet here, then return to messaging when review completes.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Submit A2P Onboarding' })).toBeVisible();
  await expect(page.getByText('Guidance')).toHaveCount(0);
  expect(await elementTop(page.getByLabel('Legal Business Name'))).toBeLessThan(780);
  expect(await elementTop(page.getByLabel('Legal Business Name'))).toBeLessThan(
    await elementTop(page.getByText('Recent Twilio activity').first()),
  );
  expect(await elementHeight(page.getByRole('button', { name: 'Submit A2P Onboarding' }))).toBeGreaterThanOrEqual(44);
});

test('platform admin mobile surfaces keep 44px action targets', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page, 'platform@browser.test', 'Platform-pass1!');

  await page.goto('/platform');
  await expect(page.locator('.platform-home-summary-strip')).toBeVisible();
  await expect(page.locator('.platform-home-kicker')).toHaveCount(0);
  await expect(page.locator('.platform-home-worklist__item').first()).toBeVisible();
  await expect(page.locator('[aria-labelledby="platform-home-needs-attention"]').getByRole('link', { name: 'Organizations' })).toHaveCount(0);
  await expect(page.locator('.platform-home-worklist__item').first().locator('.platform-home-worklist__headline')).toHaveText(
    'Open the billing portal and resolve the payment issue.',
  );

  await page.goto('/platform/organizations');
  await expect(page.locator('.card-list-item.platform-org-card').first()).toBeVisible();
  await expect(page.getByText('Use this directory to spot organizations that still need billing, onboarding, or messaging work.')).toHaveCount(0);

  const firstCard = page.locator('.card-list-item.platform-org-card').first();
  await expect(firstCard.getByRole('link', { name: 'Access' })).toBeVisible();
  await expect(firstCard.getByRole('link', { name: /Messaging|Set up provider/ })).toBeVisible();

  const firstMobileButton = firstCard.locator('.btn').first();
  expect(await elementHeight(firstMobileButton)).toBeGreaterThanOrEqual(44);

  await page.getByRole('link', { name: 'Add Organization' }).click();
  await expect(page.locator('.platform-shell__summary')).toBeVisible();
  expect(await elementHeight(page.getByRole('button', { name: 'Create Business Account' }))).toBeGreaterThanOrEqual(44);
});

test('platform mobile navigation opens as one consistent surface', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page, 'platform@browser.test', 'Platform-pass1!');
  await page.goto('/platform');

  await page.getByRole('button', { name: 'Toggle navigation' }).click();

  const topbar = page.locator('.app-topbar');
  const mobileNav = page.locator('[data-mobile-nav]');
  const mobileNavPanel = page.locator('[data-mobile-nav-panel]');

  await expect(page.locator('body')).toHaveClass(/app-mobile-nav-open/);
  await expect(mobileNav).toHaveClass(/show/);
  await expect(mobileNavPanel).toBeVisible();
  expect(await backgroundColor(topbar)).toContain('255, 255, 255');
  expect((await elementTop(mobileNavPanel)) - (await elementBottom(topbar))).toBeLessThanOrEqual(12);
  expect(await elementHeight(mobileNavPanel.locator('.app-nav-link').first())).toBeGreaterThanOrEqual(44);
});

test('workspace mobile navigation keeps the shared shell treatment', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page, 'owner@browser.test', 'Owner-pass1!');
  await page.goto('/dashboard');

  await page.getByRole('button', { name: 'Toggle navigation' }).click();

  const topbar = page.locator('.app-topbar');
  const mobileNav = page.locator('[data-mobile-nav]');
  const mobileNavPanel = page.locator('[data-mobile-nav-panel]');

  await expect(page.locator('body')).toHaveClass(/app-mobile-nav-open/);
  await expect(mobileNav).toHaveClass(/show/);
  await expect(mobileNavPanel.getByPlaceholder('Search contacts')).toBeVisible();
  expect(await backgroundColor(topbar)).toContain('255, 255, 255');
  expect((await elementTop(mobileNavPanel)) - (await elementBottom(topbar))).toBeLessThanOrEqual(12);
  expect(await elementHeight(mobileNavPanel.locator('.app-nav-link').first())).toBeGreaterThanOrEqual(44);
});
