const { expect } = require('@playwright/test');

const diagnosticsByPage = new WeakMap();

function installFailureDiagnostics(page) {
  const diagnostics = {
    consoleMessages: [],
    failedRequests: [],
    failedResponses: [],
  };

  page.on('console', (message) => {
    if (!['error', 'warning'].includes(message.type())) {
      return;
    }
    diagnostics.consoleMessages.push(`[${message.type()}] ${message.text()}`);
  });

  page.on('requestfailed', (request) => {
    diagnostics.failedRequests.push(
      `${request.method()} ${request.url()} :: ${request.failure()?.errorText || 'request failed'}`
    );
  });

  page.on('response', (response) => {
    if (response.status() < 400) {
      return;
    }
    diagnostics.failedResponses.push(
      `${response.status()} ${response.request().method()} ${response.url()}`
    );
  });

  diagnosticsByPage.set(page, diagnostics);
}

async function attachFailureDiagnostics(page, testInfo) {
  if (testInfo.status === testInfo.expectedStatus) {
    return;
  }

  const diagnostics = diagnosticsByPage.get(page);
  if (!diagnostics) {
    return;
  }

  const chunks = [];
  if (diagnostics.consoleMessages.length) {
    chunks.push(`Console\n${diagnostics.consoleMessages.join('\n')}`);
  }
  if (diagnostics.failedRequests.length) {
    chunks.push(`Failed Requests\n${diagnostics.failedRequests.join('\n')}`);
  }
  if (diagnostics.failedResponses.length) {
    chunks.push(`HTTP >= 400 Responses\n${diagnostics.failedResponses.join('\n')}`);
  }
  if (!chunks.length) {
    return;
  }

  await testInfo.attach('browser-diagnostics.txt', {
    body: Buffer.from(chunks.join('\n\n')),
    contentType: 'text/plain',
  });
}

async function login(page, username, password, path = '/login') {
  await page.goto(path);
  await page.getByLabel('Email or username').fill(username);
  await page.getByLabel('Password', { exact: true }).fill(password);
  await page.getByRole('button', { name: /Sign in to/i }).click();
}

function organizationRow(page, organizationName) {
  return page.locator('.platform-directory__row').filter({ hasText: organizationName }).first();
}

async function expectOrganizationRowState(
  row,
  {
    headlinePattern,
    billingTitle,
    messagingTitle,
    ownerInviteToken,
    ownerInviteVisible = false,
  } = {}
) {
  await expect(row).toBeVisible();
  if (headlinePattern) {
    await expect(row.getByText(headlinePattern).first()).toBeVisible();
  }
  if (billingTitle) {
    await expect(row.locator('.platform-directory__cell--status').nth(1).getByText(billingTitle).first()).toBeVisible();
  }
  if (messagingTitle) {
    await expect(row.locator('.platform-directory__cell--status').nth(2).getByText(messagingTitle).first()).toBeVisible();
  }
  if (ownerInviteVisible) {
    const ownerInviteLink = row.getByRole('link', { name: 'Open invite' });
    await expect(ownerInviteLink).toBeVisible();
    if (ownerInviteToken) {
      await expect(ownerInviteLink).toHaveAttribute('href', ownerInviteToken);
    }
  }
}

async function acceptInvitation(
  page,
  {
    invitePath,
    fullName,
    username,
    phone,
    password,
    expectedUrl,
  }
) {
  await page.goto(invitePath);
  if (fullName) {
    await page.getByLabel('Full Name').fill(fullName);
  }
  await page.getByLabel('Username').fill(username);
  await page.getByLabel('Phone').fill(phone);
  await page.getByLabel('Password', { exact: true }).fill(password);
  await page.getByLabel('Confirm Password').fill(password);
  await page.getByRole('button', { name: /Accept invitation/i }).click();
  if (expectedUrl) {
    await expect(page).toHaveURL(expectedUrl);
  }
}

async function startFakeCheckoutFromSetup(page) {
  await page.getByRole('button', { name: /Start subscription|Update subscription/ }).click();
  if (/\/policies\/accept/.test(page.url())) {
    await expect(page.getByRole('heading', { name: 'Required before Checkout' })).toBeVisible();
    const policyCheckboxes = page.locator('input[name="accepted_policy"]');
    const policyCount = await policyCheckboxes.count();
    for (let index = 0; index < policyCount; index += 1) {
      await policyCheckboxes.nth(index).check();
    }
    await page.getByRole('button', { name: 'Accept policies' }).click();
    await expect(page).toHaveURL(/\/setup\?step=billing$/);
    await page.getByRole('button', { name: /Start subscription|Update subscription/ }).click();
  }
  await expect(page).toHaveURL(/\/_test\/stripe\/checkout\/cs_fake_org_/);
}

async function completeFakeCheckout(page) {
  await page.getByRole('button', { name: 'Complete Test Checkout' }).click();
  await expect(page).toHaveURL(/\/setup\?step=billing&session_id=cs_fake_org_/);
}

async function fillOwnerSetupCompliance(page, { organizationName, businessEmail, notificationEmail }) {
  await page.goto('/setup?step=compliance');
  await expect(page.getByRole('heading', { name: 'Business profile and compliance' })).toBeVisible();

  await page.getByLabel('Business type').selectOption('Limited Liability Corporation');
  await page.getByLabel('Legal business name').fill(organizationName);
  await page.getByLabel('Public brand name').fill(organizationName);
  await page.getByLabel('This business has its own EIN or business tax ID').check();
  await expect(page.getByText('Using hosted fallback pages for Twilio submission.')).toBeVisible();
  await page.getByLabel('Business industry').selectOption('TECHNOLOGY');
  await page.getByLabel('Business email').fill(businessEmail);
  await page.getByLabel('Notification email').fill(notificationEmail || businessEmail);
  await page.getByLabel('Business phone').fill('+15550001991');
  await page.getByLabel('Mobile or OTP phone').fill('+15550001992');
  await page.getByLabel('Address line 1').fill('123 Browser Way');
  await page.getByLabel('City').fill('Denver');
  await page.getByLabel('State or province').fill('CO');
  await page.getByLabel('Postal code').fill('80202');
  await page.getByLabel('Registration number').fill('12-3456789');
  await page.getByLabel('Authorized rep first name').fill('Golden');
  await page.getByLabel('Authorized rep last name').fill('Owner');
  await page.getByLabel('Business title').fill('Owner');
  await page.getByLabel('Job position').selectOption('Director');
  await page.getByLabel('Campaign description').fill('Account updates and business reminders');
  await page.getByLabel('Opt-in and message flow').fill(
    'Users opt in on the website, confirm their phone number, and reply STOP to unsubscribe.'
  );
  await page.getByLabel('Message samples').fill(
    'Golden Path Bakery update 1\nGolden Path Bakery update 2'
  );
  await page.getByRole('button', { name: 'Validate and save business profile' }).click();

  await expect(page.getByText('Business profile validated and saved.')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Review and submit' })).toBeVisible();
  await expect(page.getByText('Submission source')).toBeVisible();
  await expect(page.getByText('hosted_fallback')).toBeVisible();
}

async function submitOwnerOnboarding(page) {
  await page.getByLabel(/I confirm the information provided is accurate/i).check();
  await page.getByRole('button', { name: 'Submit for Twilio review' }).click();
  await expect(page.getByText('Twilio A2P onboarding queued for review.')).toBeVisible();
}

module.exports = {
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
};
