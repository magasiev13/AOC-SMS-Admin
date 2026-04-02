const { test, expect } = require('@playwright/test');

async function login(page, username, password) {
  await page.goto('/login');
  await page.getByLabel('Email or username').fill(username);
  await page.getByLabel('Password').fill(password);
  await page.getByRole('button', { name: 'Sign In' }).click();
}

async function openMessagingForOrg(page, orgName) {
  await page.goto('/platform/organizations');
  const orgCard = page.locator('article').filter({ hasText: orgName }).first();
  await expect(orgCard).toBeVisible();
  const providerLink = orgCard.getByRole('link', { name: /Manage provider|Set up provider/ });
  if (await providerLink.count()) {
    await providerLink.click();
    return;
  }

  const accessHref = await orgCard.getByRole('link', { name: 'Manage Access' }).getAttribute('href');
  expect(accessHref).not.toBeNull();
  const match = accessHref.match(/\/platform\/organizations\/(\d+)\/access$/);
  expect(match).not.toBeNull();
  await page.goto(`/platform/organizations/${match[1]}/messaging`);
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
  await expect(page.getByLabel('Business Industry')).toBeVisible();
  await expect(page.getByLabel('Registration Identifier')).toHaveValue('EIN');
  await expect(page.getByLabel('Business Email')).toBeVisible();
  await expect(page.getByLabel('Notification Email')).toBeVisible();
  await expect(page.getByLabel('Website URL')).toBeVisible();
  await expect(page.getByLabel('Rep First Name')).toBeVisible();
  await expect(page.getByLabel('Rep Last Name')).toBeVisible();
  await expect(page.getByLabel('Business Title')).toBeVisible();
  await expect(page.getByLabel('Job Position')).toBeVisible();
  await expect(page.getByLabel('Address Line 1')).toBeVisible();
  await expect(page.getByLabel('Campaign Description')).toBeVisible();
  await expect(page.getByLabel('Opt-in / Message Flow')).toBeVisible();
  await expect(page.getByLabel('Message Samples')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Refresh Status' })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Cancel' })).toBeDisabled();
  await expect(page.locator('li').filter({ hasText: 'Messaging service' })).toContainText('not provisioned yet');

  expect(await page.getByLabel('Registration Identifier').evaluate((el) => el.required)).toBe(true);
  expect(await page.getByLabel('Registration Number').evaluate((el) => el.required)).toBe(true);

  await page.getByLabel('Registration Path').selectOption('sole_proprietor');
  await expect(page.getByLabel('Business Type')).toBeDisabled();
  await expect(page.getByLabel('Business Type')).toHaveValue('Sole Proprietor');
  expect(await page.getByLabel('Registration Identifier').evaluate((el) => el.required)).toBe(false);
  expect(await page.getByLabel('Registration Number').evaluate((el) => el.required)).toBe(false);

  await page.getByLabel('Registration Path').selectOption('low_volume_standard');
  await expect(page.getByLabel('Business Type')).toBeEnabled();
  await expect(page.getByLabel('Registration Identifier')).toHaveValue('EIN');
  expect(await page.getByLabel('Registration Identifier').evaluate((el) => el.required)).toBe(true);
  expect(await page.getByLabel('Registration Number').evaluate((el) => el.required)).toBe(true);
  await page.getByLabel('Business Type').selectOption('Limited Liability Corporation');
  await page.getByLabel('Business Industry').selectOption('TECHNOLOGY');
  await page.getByLabel('Business Email').fill('ops@onboarding.test');
  await page.getByLabel('Notification Email').fill('alerts@onboarding.test');
  await page.getByLabel('Website URL').fill('https://onboarding.test');
  await page.getByLabel('Rep First Name').fill('Olivia');
  await page.getByLabel('Rep Last Name').fill('Owner');
  await page.getByLabel('Business Title').fill('Owner');
  await page.getByLabel('Job Position').selectOption('Director');
  await page.getByLabel('Address Line 1').fill('123 Main Street');
  await page.getByLabel('City').fill('Denver');
  await page.getByLabel('State / Province').fill('CO');
  await page.getByLabel('Postal Code').fill('80202');
  await page.getByLabel('USA and Canada').check();
  await page.getByLabel('Campaign Description').fill('Community updates');
  await page.getByLabel('Opt-in / Message Flow').fill('Users opt in from the website.');
  await page.getByLabel('Message Samples').fill('Onboarding Bakery reminder');
  await page.getByRole('button', { name: 'Submit A2P Onboarding' }).click();

  await expect(page).toHaveURL(/\/platform\/organizations\/1\/messaging\/onboarding$/);
  expect(await page.getByLabel('Registration Number').evaluate((el) => el.validationMessage)).not.toBe('');
  await expect(page.locator('li').filter({ hasText: 'Onboarding' })).toContainText('draft');

  await page.getByLabel('Registration Number').fill('12-3456789');
  await page.getByRole('button', { name: 'Submit A2P Onboarding' }).click();
  expect(await page.getByLabel('Message Samples').evaluate((el) => el.validationMessage)).toBe(
    'Mixed campaigns require at least two message samples.'
  );
  await expect(page.locator('li').filter({ hasText: 'Onboarding' })).toContainText('draft');

  await page.getByLabel('Message Samples').fill('Onboarding Bakery reminder 1\nOnboarding Bakery reminder 2');
  await page.getByRole('button', { name: 'Submit A2P Onboarding' }).click();

  await expect(page.getByText('Twilio A2P onboarding queued for processing.')).toBeVisible();
  await expect(page.getByText('Automatic refresh is on')).toBeVisible();
  await expect(page.locator('li').filter({ hasText: 'Onboarding' })).toContainText('queued');
  await expect(page.getByRole('button', { name: 'Refresh Status' })).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Cancel' })).toBeEnabled();

  await page.getByRole('link', { name: 'Back to Messaging' }).click();
  await expect(page.getByRole('heading', { name: 'Manage Messaging' })).toBeVisible();
  await expect(page.getByText('Automatic refresh is on')).toBeVisible();
  await expect(page.getByText('Status:')).toContainText('queued');
});

test('platform admin sees each seeded onboarding state and action availability', async ({ page }) => {
  await login(page, 'platform@browser.test', 'Platform-pass1!');

  await openMessagingForOrg(page, 'Pending Review Bakery');
  await page.getByRole('link', { name: 'Manage A2P Onboarding' }).click();
  await expect(page.getByText('Automatic refresh is on')).toBeVisible();
  await expect(page.locator('li').filter({ hasText: 'Onboarding' })).toContainText('pending');
  await expect(page.locator('li').filter({ hasText: 'Brand' })).toContainText('pending-review');
  await expect(page.locator('li').filter({ hasText: 'Campaign' })).toContainText('pending');
  await expect(page.getByLabel('Messages include links')).toBeChecked();
  await expect(page.getByLabel('Messages include phone numbers')).toBeChecked();
  await expect(page.getByRole('button', { name: 'Refresh Status' })).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Cancel' })).toBeEnabled();

  await openMessagingForOrg(page, 'Approved Bakery');
  await expect(page.locator('li').filter({ hasText: 'Live sending' })).toContainText('enabled');
  await page.getByRole('link', { name: 'Manage A2P Onboarding' }).click();
  await expect(page.locator('li').filter({ hasText: 'Onboarding' })).toContainText('approved');
  await expect(page.getByRole('button', { name: 'Refresh Status' })).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Cancel' })).toBeDisabled();

  await openMessagingForOrg(page, 'Rejected Bakery');
  await expect(page.getByText('Provider sync error')).toBeVisible();
  await page.getByRole('link', { name: 'Manage A2P Onboarding' }).click();
  await expect(page.locator('li').filter({ hasText: 'Onboarding' })).toContainText('rejected');
  await expect(page.getByText('Last onboarding error')).toBeVisible();
  await expect(page.getByText('Twilio rejected the registration because the campaign description was too vague.')).toBeVisible();

  await openMessagingForOrg(page, 'Error Bakery');
  await expect(page.getByText('Provider sync error')).toBeVisible();
  await page.getByRole('link', { name: 'Manage A2P Onboarding' }).click();
  await expect(page.locator('li').filter({ hasText: 'Onboarding' })).toContainText('error');
  await expect(page.getByText('Last onboarding error')).toBeVisible();
  await expect(page.getByText('Twilio A2P onboarding could not be queued. Check Redis/RQ and retry.')).toBeVisible();

  await openMessagingForOrg(page, 'Queued Bakery');
  await page.getByRole('link', { name: 'Manage A2P Onboarding' }).click();
  await expect(page.getByText('Automatic refresh is on')).toBeVisible();
  await expect(page.locator('li').filter({ hasText: 'Onboarding' })).toContainText('queued');
  await page.getByRole('button', { name: 'Refresh Status' }).click();
  await expect(page.getByText('Twilio A2P onboarding refresh queued.')).toBeVisible();
  await page.getByRole('button', { name: 'Cancel' }).click();
  await expect(page.getByText('Twilio A2P onboarding canceled.')).toBeVisible();
  await expect(page.locator('li').filter({ hasText: 'Onboarding' })).toContainText('canceled');
  await expect(page.getByRole('button', { name: 'Refresh Status' })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Cancel' })).toBeDisabled();

  await openMessagingForOrg(page, 'Canceled Bakery');
  await page.getByRole('link', { name: 'Manage A2P Onboarding' }).click();
  await expect(page.locator('li').filter({ hasText: 'Onboarding' })).toContainText('canceled');
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
