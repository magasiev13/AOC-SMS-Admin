const { test, expect, request: requestFactory } = require('@playwright/test');
const { createHmac } = require('node:crypto');
const { login, installFailureDiagnostics, attachFailureDiagnostics } = require('./helpers');

test.beforeEach(async ({ page }) => installFailureDiagnostics(page));
test.afterEach(async ({ page }, testInfo) => attachFailureDiagnostics(page, testInfo));
test.setTimeout(60_000);

async function postFakeInbound(page, { phone, body, width, ordinal }) {
  const url = new URL('/webhooks/twilio/inbound', page.url()).toString();
  const payload = {
    From: phone, To: '+15550001111', AccountSid: 'ACactive0001', MessagingServiceSid: 'MGactive0001',
    Body: body, ProfileName: `Feedback respondent ${width}`, MessageSid: `SM-feedback-browser-${width}-${ordinal}`,
  };
  const material = url + Object.keys(payload).sort().map(key => key + payload[key]).join('');
  const signature = createHmac('sha1', 'playwright-platform-token').update(material).digest('base64');
  // An external provider never shares the operator's browser credentials.
  const provider = await requestFactory.newContext({ storageState: { cookies: [], origins: [] } });
  try {
    const response = await provider.post(url, { form: payload, headers: { 'X-Twilio-Signature': signature } });
    expect(response.status()).toBe(200);
  } finally {
    await provider.dispose();
  }
}

async function waitForSavedReply(page, width, text) {
  const url = '/inbox?search=' + encodeURIComponent(`Feedback respondent ${width}`);
  // Chromium supports Secure cookies on HTTP loopback; APIRequestContext
  // correctly omits them there. Exercise the real browser's signed-in path.
  await page.goto(url);
  await expect(page).not.toHaveURL(/\/login(?:\?|$)/);
  await expect.poll(async () => {
    await page.reload();
    return (await page.locator('.inbox-message-bubble').allTextContents()).some(body => body.includes(text));
  }, { timeout: 15000 }).toBe(true);
}

for (const width of [1440, 390]) {
  test(`event feedback keeps attendance separate and staff numbers private at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    await login(page, 'owner@browser.test', 'Owner-pass1!');
    await page.goto('/events');
    if (width === 390) {
      await page.locator('.card-list-item').filter({ hasText: 'Acme Spring Launch' }).getByRole('link', { name: 'View', exact: true }).click();
    } else {
      await page.getByRole('link', { name: 'Acme Spring Launch', exact: true }).click();
    }
    const eventPath = new URL(page.url()).pathname;
    const registrationCount = await page.locator('.table-responsive tbody tr').count();
    const registrationNames = await page.locator('.table-responsive tbody tr td:first-child').allTextContents();
    if (width === 390) await page.locator('[data-page-actions-toggle]').click();
    await page.getByRole('link', { name: 'Event feedback', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Feedback forms', exact: true })).toBeVisible();
    await page.goto(`${eventPath}/feedback/new`);
    await page.getByLabel('Feedback name', { exact: true }).fill(`Community celebration feedback ${width}`);
    const keyword = `EVENTFB${width}`;
    await page.getByLabel('Reply keyword').fill(keyword);
    await page.getByLabel('Questions', { exact: true }).fill('Rate this event from 1 to 5?\nWhat would you improve?');
    await page.getByRole('button', { name: 'Create feedback', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Responses', exact: true })).toBeVisible();
    const feedbackPath = new URL(page.url()).pathname;
    await page.getByRole('link', { name: 'Review invitation', exact: true }).focus();
    await page.keyboard.press('Enter');
    await expect(page.getByLabel('Event Blast', { exact: false })).toBeChecked();
    await expect(page.getByLabel('Message', { exact: true })).toHaveValue(new RegExp(keyword));
    await page.getByRole('button', { name: /^Send (Now|Message)$/ }).click();
    const review = page.getByRole('dialog', { name: 'Review send', exact: true });
    await expect(review).toBeVisible();
    await expect(review.locator('[data-review="audience"]')).toHaveText('Acme Spring Launch');
    await page.keyboard.press('Escape'); // The invitation is reviewed, not sent.

    const phone = width === 390 ? '+17205558039' : '+17205558044';
    // A recipient can still be in an old signup when the new invitation
    // arrives. Its feedback keyword must never become an attendee name.
    const oldKeyword = `RSVPPENDING${width}`;
    await page.goto('/inbox/surveys/add');
    await page.getByLabel('Survey name', { exact: true }).fill(`Interrupted RSVP ${width}`);
    await page.getByLabel('Trigger keyword', { exact: true }).fill(oldKeyword);
    await page.getByLabel('Questions (one per line)', { exact: true }).fill('What is your attendee name?');
    await page.getByLabel('Link to an existing event', { exact: true }).check();
    await page.getByLabel('Select event', { exact: true }).selectOption(eventPath.split('/').pop());
    await page.getByRole('button', { name: 'Save survey flow', exact: true }).click();
    await postFakeInbound(page, { phone, body: oldKeyword, width, ordinal: 0 });
    await waitForSavedReply(page, width, 'What is your attendee name?');
    await postFakeInbound(page, { phone, body: keyword, width, ordinal: 1 });
    await waitForSavedReply(page, width, 'Rate this event from 1 to 5?');
    await postFakeInbound(page, { phone, body: '5', width, ordinal: 2 });
    await waitForSavedReply(page, width, 'What would you improve?');
    await postFakeInbound(page, { phone, body: `Great music. Call ${phone}.`, width, ordinal: 3 });
    await page.goto(feedbackPath);
    await expect(page.getByText(`Great music. Call ${phone}.`, { exact: true })).toBeVisible();
    await expect(page.locator('.card-list-item').filter({ hasText: `Feedback respondent ${width}` })).toContainText('Completed');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`event-feedback-owner-${width}.png`), fullPage: false });
    await page.goto(eventPath);
    await expect(page.locator('.table-responsive tbody tr')).toHaveCount(registrationCount);
    expect(await page.locator('.table-responsive tbody tr td:first-child').allTextContents()).toEqual(registrationNames);

    await login(page, 'staff@browser.test', 'Staff-pass1!');
    await page.goto(feedbackPath);
    await expect(page.getByText(`Feedback respondent ${width}`, { exact: true })).toBeVisible();
    await expect(page.getByText('Great music.', { exact: false })).toBeVisible();
    expect(await page.content()).not.toContain(phone);
    await expect(page.getByRole('link', { name: /export/i })).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`event-feedback-staff-${width}.png`), fullPage: false });
  });
}
