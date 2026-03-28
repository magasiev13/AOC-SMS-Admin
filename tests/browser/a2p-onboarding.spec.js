const { test, expect } = require('@playwright/test');

async function login(page, username, password) {
  await page.goto('/login');
  await page.getByLabel('Username').fill(username);
  await page.getByLabel('Password').fill(password);
  await page.getByRole('button', { name: 'Sign In' }).click();
}

async function openMessagingForOrg(page, orgName) {
  await page.goto('/platform/organizations');
  const orgRow = page.locator('tr').filter({ hasText: orgName });
  await expect(orgRow).toBeVisible();
  await orgRow.getByRole('link', { name: /Manage provider|Set up provider/ }).click();
}

test('platform admin can navigate onboarding from messaging and submit deterministic local validation', async ({ page }) => {
  await login(page, 'platform@browser.test', 'Platform-pass1!');
  await openMessagingForOrg(page, 'Onboarding Bakery');

  await expect(page.getByRole('heading', { name: 'Manage Messaging' })).toBeVisible();
  await expect(page.getByText('A2P onboarding', { exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Manage A2P Onboarding' })).toBeVisible();
  await expect(page.getByText('Status:')).toContainText('draft');
  await expect(page.getByText('Twilio subaccount not provisioned yet')).toBeVisible();

  await page.getByRole('link', { name: 'Manage A2P Onboarding' }).click();

  await expect(page.getByRole('heading', { name: 'A2P Onboarding' })).toBeVisible();
  await expect(page.getByLabel('Registration Path')).toBeVisible();
  await expect(page.getByLabel('Number Strategy')).toBeVisible();
  await expect(page.getByLabel('Legal Business Name')).toHaveValue('Onboarding Bakery');
  await expect(page.getByLabel('Business Type')).toHaveValue('');
  await expect(page.getByLabel('Business Email')).toBeVisible();
  await expect(page.getByLabel('Rep First Name')).toBeVisible();
  await expect(page.getByLabel('Rep Last Name')).toBeVisible();
  await expect(page.getByLabel('Campaign Description')).toBeVisible();
  await expect(page.getByLabel('Opt-in / Message Flow')).toBeVisible();
  await expect(page.getByLabel('Message Samples')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Refresh Status' })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Cancel' })).toBeDisabled();
  await expect(page.getByText('Messaging service: not provisioned yet')).toBeVisible();

  await page.getByLabel('Business Type').fill('LLC');
  await page.getByLabel('Business Email').fill('ops@onboarding.test');
  await page.getByLabel('Rep First Name').fill('Olivia');
  await page.getByLabel('Rep Last Name').fill('Owner');
  await page.getByLabel('Campaign Description').fill('Community updates');
  await page.getByLabel('Opt-in / Message Flow').fill('Users opt in from the website.');
  await page.getByLabel('Message Samples').fill('Onboarding Bakery reminder');
  await page.getByRole('button', { name: 'Submit A2P Onboarding' }).click();

  await expect(page.getByText('Twilio A2P onboarding queued for processing.')).toBeVisible();
  await expect(page.locator('li').filter({ hasText: 'Onboarding:' })).toContainText('queued');
  await expect(page.getByRole('button', { name: 'Refresh Status' })).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Cancel' })).toBeEnabled();

  await page.getByRole('link', { name: 'Back to Messaging' }).click();
  await expect(page.getByRole('heading', { name: 'Manage Messaging' })).toBeVisible();
  await expect(page.getByText('Status:')).toContainText('queued');
});

test('platform admin sees each seeded onboarding state and action availability', async ({ page }) => {
  await login(page, 'platform@browser.test', 'Platform-pass1!');

  await openMessagingForOrg(page, 'Pending Review Bakery');
  await page.getByRole('link', { name: 'Manage A2P Onboarding' }).click();
  await expect(page.locator('li').filter({ hasText: 'Onboarding:' })).toContainText('pending');
  await expect(page.getByText('Brand: pending-review')).toBeVisible();
  await expect(page.getByText('Campaign: pending')).toBeVisible();
  await expect(page.getByLabel('Messages include links')).toBeChecked();
  await expect(page.getByLabel('Messages include phone numbers')).toBeChecked();
  await expect(page.getByRole('button', { name: 'Refresh Status' })).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Cancel' })).toBeEnabled();

  await openMessagingForOrg(page, 'Approved Bakery');
  await expect(page.locator('li').filter({ hasText: 'Live sending:' })).toContainText('enabled');
  await page.getByRole('link', { name: 'Manage A2P Onboarding' }).click();
  await expect(page.locator('li').filter({ hasText: 'Onboarding:' })).toContainText('approved');
  await expect(page.getByRole('button', { name: 'Refresh Status' })).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Cancel' })).toBeDisabled();

  await openMessagingForOrg(page, 'Rejected Bakery');
  await expect(page.getByText('Provider sync error')).toBeVisible();
  await page.getByRole('link', { name: 'Manage A2P Onboarding' }).click();
  await expect(page.locator('li').filter({ hasText: 'Onboarding:' })).toContainText('rejected');
  await expect(page.getByText('Last onboarding error')).toBeVisible();
  await expect(page.getByText('Twilio rejected the registration because the campaign description was too vague.')).toBeVisible();

  await openMessagingForOrg(page, 'Error Bakery');
  await expect(page.getByText('Provider sync error')).toBeVisible();
  await page.getByRole('link', { name: 'Manage A2P Onboarding' }).click();
  await expect(page.locator('li').filter({ hasText: 'Onboarding:' })).toContainText('error');
  await expect(page.getByText('Last onboarding error')).toBeVisible();
  await expect(page.getByText('Twilio A2P onboarding could not be queued. Check Redis/RQ and retry.')).toBeVisible();

  await openMessagingForOrg(page, 'Queued Bakery');
  await page.getByRole('link', { name: 'Manage A2P Onboarding' }).click();
  await expect(page.locator('li').filter({ hasText: 'Onboarding:' })).toContainText('queued');
  await page.getByRole('button', { name: 'Refresh Status' }).click();
  await expect(page.getByText('Twilio A2P onboarding refresh queued.')).toBeVisible();
  await page.getByRole('button', { name: 'Cancel' }).click();
  await expect(page.getByText('Twilio A2P onboarding canceled.')).toBeVisible();
  await expect(page.locator('li').filter({ hasText: 'Onboarding:' })).toContainText('canceled');
  await expect(page.getByRole('button', { name: 'Refresh Status' })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Cancel' })).toBeDisabled();

  await openMessagingForOrg(page, 'Canceled Bakery');
  await page.getByRole('link', { name: 'Manage A2P Onboarding' }).click();
  await expect(page.locator('li').filter({ hasText: 'Onboarding:' })).toContainText('canceled');
  await expect(page.getByRole('button', { name: 'Refresh Status' })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Cancel' })).toBeDisabled();
});

test('owner and staff are blocked from platform messaging and onboarding routes', async ({ page }) => {
  await login(page, 'owner@browser.test', 'Owner-pass1!');
  let response = await page.goto('/platform/organizations/1/messaging');
  expect(response).not.toBeNull();
  expect(response.status()).toBe(403);

  response = await page.goto('/platform/organizations/1/messaging/onboarding');
  expect(response).not.toBeNull();
  expect(response.status()).toBe(403);

  await login(page, 'staff@browser.test', 'Staff-pass1!');
  response = await page.goto('/platform/organizations/1/messaging');
  expect(response).not.toBeNull();
  expect(response.status()).toBe(403);

  response = await page.goto('/platform/organizations/1/messaging/onboarding');
  expect(response).not.toBeNull();
  expect(response.status()).toBe(403);
});
