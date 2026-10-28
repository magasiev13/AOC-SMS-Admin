const { test, expect, request: apiRequest } = require('@playwright/test');
const { createHmac } = require('node:crypto');
const { inflateSync } = require('node:zlib');
const { login, confirmSendReview, installFailureDiagnostics, attachFailureDiagnostics } = require('./helpers');

test.setTimeout(90_000);
test.beforeEach(async ({ page }) => installFailureDiagnostics(page));
test.afterEach(async ({ page }, testInfo) => attachFailureDiagnostics(page, testInfo));

const fixturePhones = [
  '+17205550101', '+17205550102', '+17205550103', '+17205550121', '+17205550122',
  '+17205550201', '+17205550202', '+17205550203', '+15550001111', '+15550001002', '+15550001003',
];

function publicReviewPayload(token) {
  const compressed = token.startsWith('.');
  const encoded = token.split('.')[compressed ? 1 : 0];
  const decoded = Buffer.from(encoded, 'base64url');
  return JSON.parse((compressed ? inflateSync(decoded) : decoded).toString());
}

async function privateRenderedPage(page, phones = fixturePhones) {
  await expect(page).toHaveTitle(/Twinevia/);
  await expect(page.locator('main')).not.toBeEmpty();
  await expect(page.locator('vite-error-overlay, nextjs-portal, #webpack-dev-server-client-overlay')).toHaveCount(0);
  const source = await page.content();
  for (const phone of phones) {
    expect(source).not.toContain(phone);
    expect(source).not.toContain(phone.slice(1));
    expect(source).not.toContain(phone.slice(-10));
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
}

async function noExportControls(page) {
  await expect(page.getByRole('link', { name: /export|download csv/i })).toHaveCount(0);
  await expect(page.getByRole('button', { name: /export|download csv/i })).toHaveCount(0);
}

async function cookieFreeFakeInbound(api, base, keyword, width, ordinal) {
  const url = new URL('/webhooks/twilio/inbound', base).toString();
  expect(new URL(url).hostname).toBe('127.0.0.1');
  const payload = {
    From: '+17205550101', To: '+15550001111', AccountSid: 'ACactive0001',
    MessagingServiceSid: 'MGactive0001', ProfileName: 'Maya Chen', Body: keyword,
    MessageSid: `SM-staff-private-${width}-${ordinal}`,
  };
  const material = url + Object.keys(payload).sort().map(key => key + payload[key]).join('');
  const signature = createHmac('sha1', 'playwright-platform-token').update(material).digest('base64');
  const result = await api.post(url, { form: payload, headers: { 'X-Twilio-Signature': signature } });
  expect(result.status()).toBe(200);
}

async function visibleSurveyRow(page, width, name) {
  return (width === 390 ? page.locator('.card-list-item') : page.getByRole('row'))
    .filter({ hasText: name }).first();
}

for (const width of [1440, 390]) {
  test(`staff operates named contacts and sealed test sends without phone disclosure at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    const runtimeErrors = [];
    page.on('pageerror', error => runtimeErrors.push(String(error)));
    page.on('console', message => {
      if (message.type() === 'error' && !/status of 403/.test(message.text())) runtimeErrors.push(message.text());
    });
    await login(page, 'owner@browser.test', 'Owner-pass1!');
    expect(new URL(page.url()).hostname).toBe('127.0.0.1');
    const base = new URL(page.url()).origin;
    const inbound = await apiRequest.newContext(); // Provider requests must not carry an operator's session cookie.
    const suppressionPhone = width === 390 ? '+17205558939' : '+17205558944';
    const privatePhones = [...fixturePhones, suppressionPhone];
    try {
      const surveyName = `Staff privacy survey ${width}`;
      const keyword = `STAFFPRIV${width}`;
      const question = `Which celebration activity did you prefer (${width})?`;
      await page.goto('/inbox/surveys/add');
      await page.getByLabel('Survey name', { exact: true }).fill(surveyName);
      await page.getByLabel('Trigger keyword', { exact: true }).fill(keyword);
      await page.getByLabel('Questions (one per line)', { exact: true }).fill(question);
      await page.getByRole('button', { name: 'Save survey flow', exact: true }).click();
      const surveyRow = await visibleSurveyRow(page, width, surveyName);
      await expect(surveyRow).toBeVisible();
      const submissions = await surveyRow.getByRole('link', { name: /submissions/i }).getAttribute('href');
      expect(submissions).toMatch(/^\/inbox\/surveys\/\d+\/submissions$/);

      await cookieFreeFakeInbound(inbound, base, keyword, width, 1);
      await expect.poll(async () => {
        await page.goto('/inbox?view=threads&search=Maya');
        await page.locator('#inboxThreadList .inbox-thread-link').filter({ hasText: 'Maya Chen' }).click();
        return page.locator('.inbox-message-bubble').filter({ hasText: question }).count();
      }, { timeout: 15_000 }).toBeGreaterThan(0);
      const threadPath = new URL(page.url()).pathname + new URL(page.url()).search;
      const threadID = new URL(page.url()).searchParams.get('thread');
      expect(threadID).toMatch(/^\d+$/);
      await cookieFreeFakeInbound(inbound, base, 'Music was great. Call +17205550101 for details.', width, 2);
      await page.goto(submissions);
      await expect(page.locator('.survey-answer-preview:visible').filter({ hasText: 'Music was great. Call +17205550101 for details.' })).toBeVisible();

      await page.goto('/unsubscribed/add');
      await page.getByLabel('Name (optional)').fill(`Privacy suppression ${width}`);
      await page.getByLabel('Phone', { exact: true }).fill(suppressionPhone);
      await page.getByLabel('Reason (optional)').fill(`Request received from ${suppressionPhone}`);
      await page.getByRole('button', { name: 'Add to Suppression', exact: true }).click();
      await expect(page.locator('body')).toContainText(`Privacy suppression ${width}`);

      await login(page, 'staff@browser.test', 'Staff-pass1!');
      await page.goto('/community');
      await privateRenderedPage(page, privatePhones);
      await noExportControls(page);
      const memberSearch = page.getByRole('textbox', { name: 'Search community members' });
      await memberSearch.fill('Maya');
      await memberSearch.press('Enter');
      await expect(page.locator('.collection-panel--results')).toContainText('Maya Chen');
      await page.getByRole('navigation', { name: 'Community view' }).getByRole('link', { name: 'All contacts', exact: true }).focus();
      await page.keyboard.press('Enter');
      await expect(page).toHaveURL(/view=all/);
      await privateRenderedPage(page, privatePhones);
      await page.screenshot({ path: testInfo.outputPath(`staff-community-${width}.png`), fullPage: false });

      await page.goto('/events');
      if (width === 390) {
        await page.locator('.card-list-item').filter({ hasText: 'Acme Spring Launch' }).getByRole('link', { name: 'View', exact: true }).focus();
      } else {
        await page.getByRole('link', { name: 'Acme Spring Launch', exact: true }).focus();
      }
      await page.keyboard.press('Enter');
      await expect(page).toHaveURL(/\/events\/\d+$/);
      const eventPath = new URL(page.url()).pathname;
      expect(eventPath).toMatch(/^\/events\/\d+$/);
      await expect(page.locator('body')).toContainText('Acme RSVP');
      await privateRenderedPage(page, privatePhones);
      await noExportControls(page);
      if (width === 390) await page.locator('[data-page-actions-toggle]').click();
      await page.getByRole('link', { name: 'Event feedback', exact: true }).focus();
      await page.keyboard.press('Enter');
      await expect(page.getByRole('heading', { name: 'Feedback forms', exact: true })).toBeVisible();
      await privateRenderedPage(page, privatePhones);

      await page.goto('/inbox?view=threads');
      const inboxSearch = page.getByRole('textbox', { name: 'Search inbox threads' });
      await inboxSearch.fill('Maya');
      await inboxSearch.press('Enter');
      await expect(inboxSearch).toBeVisible();
      expect(new URL(page.url()).searchParams.get('view')).toBe('threads');
      await page.locator('#inboxThreadList .inbox-thread-link').filter({ hasText: 'Maya Chen' }).focus();
      await page.keyboard.press('Enter');
      await expect(page).toHaveURL(new RegExp(`thread=${threadID}(?:&|$)`));
      expect(new URL(page.url()).searchParams.get('thread')).toBe(threadID);
      await privateRenderedPage(page, privatePhones);
      await page.getByLabel('Send reply', { exact: true }).fill(`Staff private reply ${width}`);
      await page.locator('.inbox-reply-form').getByRole('button', { name: 'Send', exact: true }).focus();
      await page.keyboard.press('Enter');
      await expect(page.locator('.inbox-message-bubble').filter({ hasText: `Staff private reply ${width}` })).toBeVisible();
      await privateRenderedPage(page, privatePhones);
      await page.screenshot({ path: testInfo.outputPath(`staff-inbox-${width}.png`), fullPage: false });

      await page.goto('/dashboard');
      await page.locator('.advanced-options__summary').click();
      await page.getByLabel('Test Mode', { exact: false }).check();
      const testContact = page.getByLabel('Saved test contact', { exact: true });
      await expect(testContact).toHaveAttribute('name', 'test_recipient_id');
      await testContact.selectOption({ label: 'Board Chair' });
      expect(await testContact.inputValue()).toMatch(/^\d+$/);
      await page.getByLabel('Message', { exact: true }).fill(`Staff confidential test ${width}`);
      await privateRenderedPage(page, privatePhones);
      const reviewResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/dashboard/review' && response.request().method() === 'POST');
      await page.getByRole('button', { name: /^Send (Now|Message)$/ }).click();
      const response = await reviewResponse;
      expect(response.status()).toBe(200);
      const reviewData = await response.json();
      const publicPayload = publicReviewPayload(reviewData.review_token);
      expect(Object.keys(publicPayload).sort()).toEqual(['encrypted', 'version']);
      expect(publicPayload.version).toBe(2);
      for (const phone of privatePhones) expect(JSON.stringify(reviewData)).not.toContain(phone);
      const review = page.getByRole('dialog', { name: 'Review send', exact: true });
      await expect(review).toBeVisible();
      await expect(review.locator('[data-review="eligible"]')).toHaveText('1');
      await privateRenderedPage(page, privatePhones);
      await page.screenshot({ path: testInfo.outputPath(`staff-sealed-review-${width}.png`), fullPage: false });
      await confirmSendReview(page); // Only the wrapper's fake provider/private queue can receive this loopback test.
      await expect(page).toHaveURL(/\/logs\/\d+$/);
      const logPath = new URL(page.url()).pathname;
      await expect(page.locator('#logStatusMeta')).toHaveAttribute('data-success-count', '1', { timeout: 15_000 });
      await privateRenderedPage(page, privatePhones);

      await page.goto('/dashboard');
      await page.locator('.advanced-options__summary').click();
      await page.getByLabel('Test Mode', { exact: false }).check();
      await page.getByLabel('Saved test contact', { exact: true }).selectOption({ label: 'Board Chair' });
      await page.getByLabel('Schedule for later', { exact: true }).check();
      await page.getByLabel('Message', { exact: true }).fill(`Staff private scheduled test ${width}`);
      await page.getByLabel('Date', { exact: true }).fill('2099-12-31');
      await page.getByLabel('Time', { exact: true }).fill('23:59');
      await page.getByRole('button', { name: /Schedule Message|Send Now/ }).click();
      await confirmSendReview(page);
      await expect(page).toHaveURL(/\/scheduled$/);
      await expect(page.locator('body')).toContainText(`Staff private scheduled test ${width}`);

      for (const path of [
        '/scheduled', '/unsubscribed', '/logs', logPath, '/inbox/keywords', '/inbox/keywords/add',
        '/inbox/surveys', '/inbox/surveys/add', submissions.replace('/submissions', '/edit'),
        submissions, threadPath, `${eventPath}/edit`, '/events/add',
      ]) {
        const visited = await page.goto(path);
        expect(visited.status()).toBe(200);
        await privateRenderedPage(page, privatePhones);
        await noExportControls(page);
      }
      await page.goto(submissions);
      await expect(page.locator('body')).toContainText('Music was great. Call [number hidden] for details.');
      await page.screenshot({ path: testInfo.outputPath(`staff-submissions-${width}.png`), fullPage: false });
      for (const path of [
        '/community/export?view=all', `${eventPath}/export`, `${submissions}/export`, '/unsubscribed/export',
        '/community/add', '/settings', '/billing',
      ]) {
        const denied = await page.evaluate(async target => {
          const response = await fetch(target, { credentials: 'same-origin', headers: { Accept: 'text/csv' } });
          return { status: response.status, body: await response.text(), disposition: response.headers.get('content-disposition') };
        }, path);
        expect(denied.status, path).toBe(403);
        expect(denied.disposition).toBeNull();
        for (const phone of privatePhones) expect(denied.body).not.toContain(phone);
      }
      await page.goto('/platform');
      await expect(page).toHaveURL(/\/dashboard$/);

      await page.goto('/account/security-contact');
      await expect(page.getByLabel('Phone number', { exact: true })).toHaveValue('');
      await privateRenderedPage(page, privatePhones);
      await page.goto('/dashboard'); // Hiding the saved alert number does not erase it or force the contact gate again.
      await expect(page).toHaveURL(/\/dashboard$/);
      await page.goto('/account/password');
      await expect(page.getByLabel('New password', { exact: true })).toBeVisible();
      await privateRenderedPage(page, privatePhones);
      expect(runtimeErrors).toEqual([]);
      await page.goto('/dashboard');
      await page.locator('form[action="/logout"]').first().evaluate(form => form.requestSubmit());
      await expect(page).toHaveURL(/\/login$/);
      await page.goto('/community');
      await expect(page).toHaveURL(/\/login/);
    } finally {
      await inbound.dispose();
    }
  });
}
