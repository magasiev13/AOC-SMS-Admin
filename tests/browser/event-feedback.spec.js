const { test, expect, request: requestFactory } = require('@playwright/test');
const { createHmac } = require('node:crypto');
const { login, installFailureDiagnostics, attachFailureDiagnostics, waitForRenderedDocument } = require('./helpers');

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
  await page.locator('.inbox-thread-link').filter({ hasText: `Feedback respondent ${width}` }).click();
  await expect(page).not.toHaveURL(/\/login(?:\?|$)/);
  await expect.poll(async () => {
    await page.reload();
    return (await page.locator('.inbox-message-bubble').allTextContents()).some(body => body.includes(text));
  }, { timeout: 15000 }).toBe(true);
}

test('event feedback keeps its help aligned and separated when the viewport changes', async ({ page }) => {
  await login(page, 'owner@browser.test', 'Owner-pass1!');
  await page.goto('/events/1/feedback');
  await page.getByRole('link', { name: 'Create Feedback', exact: true }).first().click();
  await expect(page.getByRole('heading', { name: 'Create Event Feedback', exact: true })).toBeVisible();
  const guidance = page.locator('[data-context-help]');
  await page.getByLabel('Feedback Name').fill('Feedback draft');
  for (const width of [390, 930, 1440]) {
    await page.setViewportSize({ width, height: 840 });
    for (const open of [true, false]) {
      await guidance.locator('summary').press('Enter');
      // Keyboard focus can scroll the page during resize. Read all viewport
      // coordinates in one browser task so they share the same scroll position.
      const layout = await guidance.evaluate(details => {
        const main = details.closest('main');
        const form = main.querySelector('.workspace-form-layout .workspace-panel').getBoundingClientRect();
        const header = main.querySelector('.app-page-header').getBoundingClientRect();
        const help = details.getBoundingClientRect();
        return {
          open: details.open,
          gap: help.top - form.bottom,
          form: { left: form.left, right: form.right },
          aligned: [header, help].map(box => ({ left: box.left, right: box.right })),
        };
      });
      expect(layout.open).toBe(open);
      expect(layout.gap).toBeGreaterThanOrEqual(16);
      for (const box of layout.aligned) {
        expect(box.left).toBeCloseTo(layout.form.left, 0);
        expect(box.right).toBeCloseTo(layout.form.right, 0);
      }
    }
  }
  await expect(page.getByLabel('Feedback Name')).toHaveValue('Feedback draft');
});

test('long event names and feedback keywords keep phone controls reachable', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 760 });
  await login(page, 'owner@browser.test', 'Owner-pass1!');
  const title = 'AutumnCommunityFoundationPartnerAppreciationGathering';
  const feedbackName = 'CommunityFoundationPartnerAppreciationGatheringFeedback';
  const keyword = 'PARTNERSANDCOMMUNITYRESEARCHSURVEY2026';
  await page.goto('/events/add');
  await page.getByLabel('Event Title').fill(title);
  await page.getByRole('button', { name: 'Create Event', exact: true }).click();
  const eventPath = new URL(page.url()).pathname;
  await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();
  await waitForRenderedDocument(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(320);
  await page.goto('/events');
  const card = page.locator('.card-list-item').filter({ hasText: title });
  const view = card.getByRole('link', { name: 'View', exact: true });
  const fits = await view.evaluate(link => link.getBoundingClientRect().right <= window.innerWidth);
  expect(fits).toBe(true);
  await view.click();
  await expect(page).toHaveURL(url => url.pathname === eventPath);
  await page.goto(eventPath + '/feedback/new');
  await page.getByLabel('Feedback Name').fill(feedbackName);
  await page.getByLabel('Reply Keyword').fill(keyword);
  await page.getByRole('button', { name: 'Create Feedback', exact: true }).click();
  await expect(page.getByRole('heading', { name: feedbackName, exact: true })).toBeVisible();
  await expect(page.locator('code').filter({ hasText: keyword })).toBeVisible();
  await waitForRenderedDocument(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(320);
  for (const label of await page.locator('[aria-label="Feedback progress"] dt').all()) {
    expect(await label.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
  }
  const detailPath = new URL(page.url()).pathname;
  await login(page, 'staff@browser.test', 'Staff-pass1!');
  await page.goto('/inbox/surveys');
  await waitForRenderedDocument(page);
  const surveyCard = page.locator('.survey-flows-cards .card-list-item').filter({ hasText: feedbackName });
  await expect(surveyCard.locator('code')).toHaveText(keyword);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(320);
  await surveyCard.getByRole('link', { name: 'Submissions' }).click();
  await expect(page).toHaveURL(url => url.pathname === detailPath);
});

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
    await page.getByRole('navigation', { name: 'Event workspace sections' }).getByRole('link', { name: 'Registrations', exact: true }).click();
    const registrationCount = await page.locator('.table-responsive tbody tr').count();
    const registrationNames = await page.locator('.table-responsive tbody tr td:first-child').allTextContents();
    await page.getByRole('navigation', { name: 'Event workspace sections' }).getByRole('link', { name: 'Feedback', exact: true }).click();
    await page.getByRole('link', { name: 'Manage Feedback', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Feedback Forms', exact: true })).toBeVisible();
    await page.goto(`${eventPath}/feedback/new`);
    await page.getByLabel('Feedback Name', { exact: true }).fill(`Community celebration feedback ${width}`);
    const keyword = `EVENTFB${width}`;
    await page.getByLabel('Reply Keyword').fill(keyword);
    await page.getByLabel('Questions', { exact: true }).fill('Rate this event from 1 to 5?\nWhat would you improve?');
    await page.getByRole('button', { name: 'Create Feedback', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Responses', exact: true })).toBeVisible();
    const feedbackPath = new URL(page.url()).pathname;
    await page.getByRole('link', { name: 'Review Invitation', exact: true }).focus();
    await page.keyboard.press('Enter');
    await expect(page.getByLabel('Event Registrants', { exact: true })).toBeChecked();
    await expect(page.getByLabel('Invitation Message', { exact: true })).toHaveValue(new RegExp(keyword));
    await expect(page.getByLabel('Send Now', { exact: true })).not.toBeChecked();
    await expect(page.getByLabel('Choose a Date and Time', { exact: true })).not.toBeChecked();
    // This fixture intentionally has unverified historical roster coverage.
    // A community invitation is a separate, explicit audience decision.
    await page.getByLabel('Contact List', { exact: true }).check();
    await page.getByLabel('Send Now', { exact: true }).check();
    await page.getByRole('button', { name: 'Review Invitation', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Review Invitation', exact: true })).toBeVisible();
    await expect(page.locator('[data-review="audience"]')).toHaveText('Contact List');
    await expect(page.locator('[data-review="timing"]')).toHaveText('Send now');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    // Exercise keyboard return to the editable composer without approving a send.
    await page.getByRole('button', { name: 'Edit Invitation', exact: true }).focus();
    await page.keyboard.press('Enter');
    await expect(page.getByLabel('Invitation Message', { exact: true })).toHaveValue(new RegExp(keyword));

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
    await page.getByRole('button', { name: 'Save Survey', exact: true }).click();
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
    await page.getByRole('navigation', { name: 'Event workspace sections' }).getByRole('link', { name: 'Registrations', exact: true }).click();
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


for (const width of [1440, 390]) {
  for (const role of ['owner', 'staff']) {
    test(`expired held invitation requires reviewed send-now choice for ${role} at ${width}px`, async ({ page }, info) => {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      await login(page, `${role}@browser.test`, role === 'owner' ? 'Owner-pass1!' : 'Staff-pass1!');
      await page.goto('/events/1/feedback');
      await page.getByRole('link', { name: `Expired held recovery ${role} ${width}`, exact: true }).click();
      await page.getByRole('link', { name: 'Review Held Invitation', exact: true }).click();
      await expect(page.getByText('The original send window has expired.', { exact: false })).toBeVisible();
      await expect(page.getByRole('button', { name: 'Confirm Send Now', exact: true })).toHaveCount(0);
      await expect(page.locator('input[name="review_token"]')).toHaveCount(0);
      await page.getByRole('link', { name: 'Review Sending Now', exact: true }).focus();
      await page.keyboard.press('Enter');
      await expect(page.locator('[data-review="timing"]')).toHaveText('Send now after this confirmation');
      await expect(page.getByRole('button', { name: 'Confirm Send Now', exact: true })).toBeVisible();
      if (role === 'staff') {
        const html = await page.content();
        for (const phone of ['+17205550101', '+17205550102', '+15550001111']) expect(html).not.toContain(phone);
      }
      await page.waitForLoadState('load');
      await page.evaluate(async () => { await document.fonts.ready; });
      await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth-innerWidth)).toBeLessThanOrEqual(1);
      await page.screenshot({ path: info.outputPath(`expired-held-${role}-${width}.png`), fullPage: false });
      await page.getByRole('button', { name: 'Confirm Send Now', exact: true }).click();
      await expect(page).toHaveURL(/\/events\/1\/feedback\/\d+$/);
      await expect(page.getByText('Invitation reviewed for sending now.', { exact: false })).toBeVisible();
      await expect(page.getByText('Pending', { exact: true })).toBeVisible();
      await expect(page.getByRole('link', { name: 'Review Held Invitation', exact: true })).toHaveCount(0);
    });
  }
}
