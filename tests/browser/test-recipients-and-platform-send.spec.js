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

test('owner can manage test recipients and use dashboard test mode states', async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await login(page, 'owner@browser.test', 'Owner-pass1!');

  const restoreBaselineRecipients = async () => {
    await page.goto('/settings/test-recipients');

    const rows = page.locator('.recipient-row');
    while (await rows.count() > 2) {
        await rows.last().getByRole('button', { name: 'Remove' }).click();
    }

    if (await rows.count() < 2) {
      await page.getByRole('button', { name: 'Add Recipient' }).click();
    }

    await rows.nth(0).getByLabel('Label').fill('Board Chair');
    await rows.nth(0).getByLabel('Phone').fill('+17205550121');
    await rows.nth(1).getByLabel('Label').fill('Ops Lead');
    await rows.nth(1).getByLabel('Phone').fill('+17205550122');
    await page.getByRole('button', { name: 'Save Changes' }).click();
    await expect(rows).toHaveCount(2);
    await expect(rows.nth(0).getByLabel('Label')).toHaveValue('Board Chair');
    await expect(rows.nth(1).getByLabel('Label')).toHaveValue('Ops Lead');
  };

  try {
    await page.goto('/dashboard');
    await page.locator('.advanced-options__summary').click();
    await expect(page.getByRole('link', { name: 'Manage test recipients' })).toBeVisible();
    await expect(page.locator('.app-nav-link').filter({ hasText: 'Test Recipients' })).toHaveCount(0);
    await page.getByLabel('Test Mode').check();
    await expect(page.getByText('Send to one saved recipient')).toBeVisible();
    await expect(page.getByText('Send to all saved test recipients')).toBeVisible();
    await expect(page.locator('#testRecipientPhone')).toBeVisible();
    await expect(page.locator('#testRecipientPhone')).toHaveValue('');

    await page.getByRole('link', { name: 'Manage test recipients' }).click();
    await expect(page).toHaveURL(/\/settings\/test-recipients$/);
    await expect(page.getByRole('heading', { name: 'Test Recipients', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Recent changes', exact: true })).toBeVisible();
    await expect(page.locator('.recipient-row')).toHaveCount(2);
    await expect(page.locator('.recipient-row').nth(0).getByLabel('Label')).toHaveValue('Board Chair');
    await expect(page.locator('.recipient-row').nth(0).getByLabel('Phone')).toHaveValue('+17205550121');
    await expect(page.locator('.recipient-row').nth(1).getByLabel('Label')).toHaveValue('Ops Lead');
    await expect(page.locator('.recipient-row').nth(1).getByLabel('Phone')).toHaveValue('+17205550122');

    await page.getByRole('button', { name: 'Add Recipient' }).click();
    const rows = page.locator('.recipient-row');
    await expect(rows).toHaveCount(3);
    const newRow = rows.nth(2);
    await newRow.getByLabel('Label').fill('Treasurer');
    await newRow.getByLabel('Phone').fill('+17205550123');
    await page.getByRole('button', { name: 'Save Changes' }).click();
    await expect(page.getByText('Internal test recipients updated.')).toBeVisible();
    await expect(page.locator('.recipient-row')).toHaveCount(3);
    await expect(page.locator('.recipient-row').nth(2).getByLabel('Label')).toHaveValue('Treasurer');

    await page.getByRole('link', { name: 'Back to Send' }).click();
    await expect(page).toHaveURL(/\/dashboard$/);
    await page.locator('.advanced-options__summary').click();
    await page.getByLabel('Test Mode').check();
    await expect(page.locator('#testRecipientPhone')).toContainText('Treasurer');

    await page.locator('#testRecipientPhone').selectOption('+17205550123');
    await page.getByLabel('Message').fill('Owner immediate test send');
    await page.getByRole('button', { name: 'Send Now' }).click();
    await expect(page).toHaveURL(/\/logs\/\d+$/);
    await expect(page.locator('body')).toContainText('Test');

    await page.goto('/dashboard');
    await page.locator('.advanced-options__summary').click();
    await page.getByLabel('Test Mode').check();
    await page.locator('#testRecipientPhone').selectOption('+17205550123');
    await page.getByLabel('Schedule for later').check();
    await page.getByLabel('Message').fill('Owner single-recipient scheduled test');
    await page.getByLabel('Date').fill('2099-12-31');
    await page.getByLabel('Time').fill('23:59');
    await page.getByRole('button', { name: /Schedule Message|Send Now/ }).click();
    await expect(page).toHaveURL(/\/scheduled$/);
    await expect(page.getByText('Message scheduled for')).toBeVisible();
    await expect(page.locator('body')).toContainText('Test');
    await page.getByRole('button', { name: /Cancel/ }).first().click();
    await page.getByRole('button', { name: 'Yes' }).click();
    await expect(page.locator('body')).toContainText('cancelled');

    await page.goto('/dashboard');
    await page.locator('.advanced-options__summary').click();
    await page.getByLabel('Test Mode').check();
    await page.getByLabel('Send to all saved test recipients').check();
    await expect(page.locator('#singleTestRecipientWrapper')).toBeHidden();

    await page.goto('/settings/test-recipients');
    await page.locator('.recipient-row').nth(0).getByRole('button', { name: 'Remove' }).click();
    await page.locator('.recipient-row').nth(0).getByRole('button', { name: 'Remove' }).click();
    await page.locator('.recipient-row').nth(0).getByRole('button', { name: 'Remove' }).click();
    await page.getByRole('button', { name: 'Save Changes' }).click();
    await expect(page.getByText('Internal test recipients updated.')).toBeVisible();
    await expect(page.locator('.recipient-row')).toHaveCount(1);
    await expect(page.locator('.recipient-row').first().getByLabel('Label')).toHaveValue('');
    await expect(page.locator('.recipient-row').first().getByLabel('Phone')).toHaveValue('');

    await page.goto('/dashboard');
    await page.locator('.advanced-options__summary').click();
    await page.getByLabel('Test Mode').check();
    await expect(page.getByText('Add at least one internal test recipient before using test mode.')).toBeVisible();
    await expect(page.getByRole('link', { name: 'Open Test Recipients' })).toBeVisible();
  } finally {
    await restoreBaselineRecipients();
  }
});

test('staff sees saved-recipient status but cannot manage recipients', async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await login(page, 'staff@browser.test', 'Staff-pass1!');

  await page.goto('/dashboard');
  await page.locator('.advanced-options__summary').click();
  await expect(page.getByRole('link', { name: 'Manage test recipients' })).toHaveCount(0);
  await expect(page.getByText('2 saved test recipients', { exact: true })).toBeVisible();

  const response = await page.goto('/settings/test-recipients');
  expect(response).not.toBeNull();
  expect(response.status()).toBe(403);
});

test('platform admin can use platform test send only on send-ready orgs', async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await login(page, 'platform@browser.test', 'Platform-pass1!', '/platform/login');

  await page.goto('/platform/organizations');
  const activeOrgLink = page.locator('.platform-directory__row').filter({ hasText: 'Acme Bakery' }).first()
    .locator('a[href*="/messaging"]').first();
  const pendingOrgLink = page.locator('.platform-directory__row').filter({ hasText: 'Onboarding Bakery' }).first()
    .locator('a[href*="/messaging"]').first();
  const activeOrgHref = await activeOrgLink.getAttribute('href');
  const pendingOrgHref = await pendingOrgLink.getAttribute('href');

  await page.goto(activeOrgHref);
  await expect(page.getByText('Platform Test Send')).toBeVisible();
  await page.getByLabel('Destination').fill('+17205550124');
  await page.getByLabel('Message').fill('Browser operational test send');
  await page.getByRole('button', { name: 'Send Operational Test' }).click();
  await expect(page.getByText('Platform operational test send completed.')).toBeVisible();

  await page.goto(pendingOrgHref);
  await expect(page.getByText('Platform Test Send')).toHaveCount(0);
});
