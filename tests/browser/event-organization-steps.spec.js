const { test, expect } = require('@playwright/test');
const fs = require('node:fs/promises');
const { login, installFailureDiagnostics, attachFailureDiagnostics } = require('./helpers');

const runtimeErrors = new WeakMap();
test.beforeEach(async ({ page }) => {
  installFailureDiagnostics(page);
  const errors = [];
  runtimeErrors.set(page, errors);
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => {
    if (message.type() === 'error' && /not focusable/i.test(message.text())) errors.push(message.text());
  });
});
test.afterEach(async ({ page }, info) => {
  await attachFailureDiagnostics(page, info);
  expect(runtimeErrors.get(page)).toEqual([]);
});

async function fits(page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
}

async function expectSection(page, id) {
  await expect(page.locator('[data-event-panel]:visible')).toHaveCount(1);
  await expect(page.locator(`[data-event-panel]#${id}`)).toBeVisible();
  await expect(page.locator('[data-event-section-link][aria-current="page"]')).toHaveAttribute('href', '#' + id);
}

async function expectStep(form, key) {
  await expect(form.locator('[data-organization-step]:visible')).toHaveCount(1);
  await expect(form.locator(`[data-organization-step="${key}"]`)).toBeVisible();
  await expect(form.locator('[data-organization-go][aria-current="step"]')).toHaveAttribute('data-organization-go', key);
}

async function fillOrganization(form, name, taxID = '98-7654321', checkboxOptions = {}) {
  await form.locator('#business_type').selectOption('Limited Liability Corporation');
  await form.locator('#legal_business_name').fill(name);
  await form.locator('#public_brand_name').fill(name);
  await form.locator('#has_business_tax_id').check(checkboxOptions);
  await form.locator('#business_industry').selectOption('TECHNOLOGY');
  await form.locator('#business_registration_number').fill(taxID);
}

async function fillContact(form) {
  await form.locator('#email').fill('steps-owner@browser.test');
  await form.locator('#notification_email').fill('steps-alerts@browser.test');
  await form.locator('#phone_number').fill('+17205550851');
  await form.locator('#mobile_number').fill('+17205550852');
  await form.locator('#address_line1').fill('123 Setup Way');
  await form.locator('#address_city').fill('Denver');
  await form.locator('#address_region').fill('CO');
  await form.locator('#address_postal_code').fill('80202');
}

async function fillRepresentative(form) {
  await form.locator('#first_name').fill('Test');
  await form.locator('#last_name').fill('Owner');
  await form.locator('#business_title').fill('Owner');
  await form.locator('#job_position').selectOption('Director');
}

async function fillSignup(form, flow = 'Customers opt in on our signup page for requested updates and reply STOP to unsubscribe.') {
  await form.locator('#has_public_website').uncheck();
  await form.locator('#message_flow').fill(flow);
}

async function fillMessaging(form) {
  await form.locator('#campaign_description').fill('Requested order updates and appointment reminders');
  await form.locator('#message_samples').fill('Your requested order is ready. Reply STOP to unsubscribe.\nYour requested appointment reminder. Reply STOP to unsubscribe.');
}

async function fillWizard(form, name) {
  await fillOrganization(form, name);
  await form.locator('[data-organization-next]').click();
  await fillContact(form);
  await form.locator('[data-organization-next]').click();
  await fillRepresentative(form);
  await form.locator('[data-organization-next]').click();
  await fillSignup(form);
  await form.locator('[data-organization-next]').click();
  await fillMessaging(form);
}

for (const width of [1440, 390]) {
  test(`event sections preserve deep links, history and an unsubmitted booking at ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    await login(page, 'owner@browser.test', 'Owner-pass1!');
    await page.goto('/events/1#registrations', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('[data-event-sections]')).toHaveAttribute('data-enhanced', 'true');
    await expect(page.getByRole('heading', { name: 'Acme Spring Launch', exact: true })).toHaveCount(1);
    await expectSection(page, 'registrations');
    const bookingTool = page.locator('[data-event-registration-tool]').filter({ has: page.locator('summary').filter({ hasText: /^Add Booking$/ }) });
    await bookingTool.locator('summary').click();
    await bookingTool.getByLabel('Name (Optional)', { exact: true }).fill(`Retained booking draft ${width}`);
    const nav = page.locator('[data-event-section-nav]');
    await nav.getByRole('link', { name: 'Messages', exact: true }).focus();
    await page.keyboard.press('Enter');
    await expectSection(page, 'messages');
    await expect(page.getByRole('heading', { name: 'Messages', exact: true })).toBeFocused();
    await page.goBack();
    await expectSection(page, 'registrations');
    await expect(bookingTool.getByLabel('Name (Optional)', { exact: true })).toHaveValue(`Retained booking draft ${width}`);
    await page.goForward();
    await expectSection(page, 'messages');
    await nav.getByRole('link', { name: 'Feedback', exact: true }).click();
    await expectSection(page, 'feedback');
    await fits(page);
    await page.screenshot({ path: info.outputPath(`event-selected-section-${width}.png`), fullPage: false });
    await page.goto('/events/1#registration_name');
    await expectSection(page, 'registrations');
    await expect(bookingTool).toHaveAttribute('open', '');
    await expect(bookingTool.getByLabel('Name (Optional)', { exact: true })).toBeVisible();
  });

  test(`staff event sections retain phone and booking-tool restrictions at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    await login(page, 'staff@browser.test', 'Staff-pass1!');
    await page.goto('/events/1#registrations');
    await expectSection(page, 'registrations');
    const contacts = page.locator(width === 390 ? '#registrations .card-list' : '#registrations .table-responsive');
    await expect(contacts.getByText('Acme RSVP', { exact: true }).first()).toBeVisible();
    await expect(page.locator('[data-event-registration-tool]')).toHaveCount(0);
    await expect(page.getByRole('link', { name: 'Export CSV', exact: true })).toHaveCount(0);
    const html = await page.content();
    for (const number of ['+17205550101', '+17205550102', '+17205550103', '+15550001111']) expect(html).not.toContain(number);
    await page.locator('[data-event-section-nav]').getByRole('link', { name: 'Overview', exact: true }).click();
    await expectSection(page, 'overview');
    await fits(page);
  });

  test(`organization steps validate, preserve drafts and recover server errors at ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    await login(page, 'trial-owner@browser.test', 'TrialOwner-pass1!');
    await page.goto('/setup?step=compliance');
    const form = page.locator('[data-organization-setup]');
    await expect(form).toHaveAttribute('data-enhanced', 'true');
    await expectStep(form, 'organization');
    const writes = [];
    page.on('request', request => {
      if (request.method() === 'POST' && new URL(request.url()).pathname === '/setup') writes.push(request.url());
    });
    await form.locator('[data-organization-next]').click();
    await expectStep(form, 'organization');
    await expect(form.locator('#business_type')).toBeFocused();
    const name = `Retained step draft ${width}`;
    const taxID = width === 1440 ? '123' : '98-7654321';
    await fillOrganization(form, name, taxID);
    await form.locator('#has_business_tax_id').uncheck();
    await form.locator('[data-organization-next]').click();
    await expectStep(form, 'organization');
    await expect(form.locator('#has_business_tax_id')).toBeFocused();
    await form.locator('#has_business_tax_id').check();
    const regions = form.locator('input[name="business_regions"]');
    for (const checkbox of await regions.all()) await checkbox.uncheck();
    await form.locator('[data-organization-next]').click();
    await expectStep(form, 'organization');
    expect(writes).toEqual([]);
    await form.locator('input[name="business_regions"][value="USA_AND_CANADA"]').check();
    await form.locator('#business_registration_number').press('Enter');
    await expectStep(form, 'contact');
    expect(writes).toEqual([]);
    await fillContact(form);
    await form.locator('[data-organization-back]').click();
    await expectStep(form, 'organization');
    await expect(form.locator('#business_registration_number')).toHaveValue(taxID);
    await expect(form.locator('#legal_business_name')).toHaveValue(name);
    await form.locator('[data-organization-next]').click();
    await expect(form.locator('#email')).toHaveValue('steps-owner@browser.test');
    await form.locator('[data-organization-next]').click();
    await fillRepresentative(form);
    await form.locator('[data-organization-next]').click();
    await expectStep(form, 'signup');
    await form.locator('#has_public_website').check();
    await form.locator('#external_privacy_policy_url').fill('invalid url');
    await form.locator('#message_flow').fill('Customers opt in on our signup page for requested updates and reply STOP to unsubscribe.');
    await form.locator('[data-organization-next]').click();
    await expectStep(form, 'signup');
    await expect(form.locator('#external_privacy_policy_url')).toBeFocused();
    await form.locator('#has_public_website').uncheck();
    await expect(form.locator('#external_privacy_policy_url')).toBeDisabled();
    await expect(form.locator('#external_privacy_policy_url')).toBeHidden();
    const invalidFlow = 'Customers opt in on our signup page for requested order updates.';
    if (width === 390) await form.locator('#message_flow').fill(invalidFlow);
    await form.locator('[data-organization-next]').click();
    await expectStep(form, 'messaging');
    await fillMessaging(form);
    expect(writes).toEqual([]);
    const stored = await page.evaluate(() => JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)]));
    expect(stored).not.toContain(taxID);
    await form.locator('[data-organization-save]').click();
    await expect(form.locator('[data-organization-error-summary]')).toBeVisible();
    expect(writes).toHaveLength(1);
    const field = width === 1440 ? 'business_registration_number' : 'message_flow';
    await expect(form).toHaveAttribute('data-initial-error-field', field);
    await expectStep(form, width === 1440 ? 'organization' : 'signup');
    await expect(form.locator('#' + field)).toBeFocused();
    await expect(form.locator('#legal_business_name')).toHaveValue(name);
    await expect(form.locator('#email')).toHaveValue('steps-owner@browser.test');
    await fits(page);
    await page.screenshot({ path: info.outputPath(`organization-error-recovery-${width}.png`), fullPage: false });
    // A deliberate new GET must still show the stored record, not a partially
    // saved fragment of the invalid form submitted above.
    await page.goto('/setup?step=compliance');
    await expect(form.locator('#legal_business_name')).toHaveValue('Setup Runway Bakery');
  });
}

test('organization Enter and final validation preserve the selected parts and reveal hidden errors', async ({ page }) => {
  await login(page, 'trial-owner@browser.test', 'TrialOwner-pass1!');
  await page.goto('/setup?step=compliance');
  const form = page.locator('[data-organization-setup]');
  await fillWizard(form, 'Final validation draft');
  const writes = [];
  page.on('request', request => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/setup') writes.push(request.url());
  });
  await form.locator('[data-organization-go="organization"]').click();
  await form.locator('#business_registration_number').press('Enter');
  await expectStep(form, 'contact');
  expect(writes).toEqual([]);
  await form.locator('[data-organization-go="messaging"]').click();
  // Autofill or a conditional rule can invalidate a previously completed
  // field. Model that state while the final step is selected.
  await form.locator('#email').evaluate(input => { input.value = 'not-an-email'; });
  await form.locator('[data-organization-save]').click();
  await expectStep(form, 'contact');
  await expect(form.locator('#email')).toBeFocused();
  await expect(form.locator('#campaign_description')).toHaveValue('Requested order updates and appointment reminders');
  expect(writes).toEqual([]);
});

test.describe('native HTML fallback', () => {
  test.use({ javaScriptEnabled: false, viewport: { width: 390, height: 844 } });

  async function nativeLogin(page, email, password, destination) {
    await page.goto('/login', { waitUntil: 'domcontentloaded' });
    await page.getByLabel('Email or username').fill(email);
    await page.getByLabel('Password', { exact: true }).fill(password);
    await page.getByRole('button', { name: /Sign in to/i }).click({ noWaitAfter: true });
    await page.waitForURL(destination, { waitUntil: 'domcontentloaded' });
  }

  test('event sections and registration disclosures remain usable without JavaScript', async ({ page }) => {
    await nativeLogin(page, 'owner@browser.test', 'Owner-pass1!', /\/dashboard$/);
    await page.goto('/events/1#registrations', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('[data-event-panel]:visible')).toHaveCount(4);
    const tools = page.locator('[data-event-registration-tool]').filter({ has: page.locator('summary').filter({ hasText: /^Add Booking$/ }) });
    await expect(tools.locator('summary')).toBeVisible();
    // With page JavaScript disabled, the utility-world animation-frame
    // stability wait cannot settle. Visibility is checked before a native click.
    await tools.locator('summary').click({ force: true });
    await expect(tools.getByLabel('Name (Optional)', { exact: true })).toBeVisible();
  });

  test('the complete organization form and server error links work without JavaScript', async ({ page }) => {
    await nativeLogin(page, 'trial-owner@browser.test', 'TrialOwner-pass1!', /\/setup$/);
    await page.goto('/setup?step=compliance', { waitUntil: 'domcontentloaded' });
    const form = page.locator('[data-organization-setup]');
    await expect(form.locator('[data-organization-step]:visible')).toHaveCount(5);
    await expect(form.locator('[data-organization-navigation]')).toBeHidden();
    await expect(form.locator('#has_business_tax_id')).toBeVisible();
    await fillOrganization(form, 'Native form recovery', '123', { force: true });
    await fillContact(form);
    await fillRepresentative(form);
    await fillSignup(form);
    await fillMessaging(form);
    await expect(form.locator('[data-organization-save]')).toBeVisible();
    await form.locator('[data-organization-save]').click({ noWaitAfter: true, force: true });
    await expect(form.locator('[data-organization-error-summary]')).toBeVisible();
    await expect(form.locator('[data-organization-step]:visible')).toHaveCount(5);
    await expect(form.locator('#business_registration_number')).toHaveValue('123');
    await expect(form.locator('[data-organization-error-link]')).toHaveAttribute('href', '#business_registration_number');
    await expect(form.locator('#legal_business_name')).toHaveValue('Native form recovery');
    await expect(form.locator('#email')).toHaveValue('steps-owner@browser.test');
  });
});

test('local event and setup controls provide selected-state feedback within a painted-frame checkpoint', async ({ page, browserName }, info) => {
  await page.setViewportSize({ width: 1440, height: 1000 });

  async function startMeasurement(kind) {
    await page.evaluate(kind => {
      const selector = kind === 'event' ? '[data-event-section-link]' : '[data-organization-go]';
      window.__localFeedback = { samples: [], kind };
      document.addEventListener('click', event => {
        const control = event.target.closest(selector);
        if (!control || !event.isTrusted) return;
        const startedAt = performance.now();
        const key = kind === 'event' ? control.hash.slice(1) : control.dataset.organizationGo;
        const panel = kind === 'event' ? document.getElementById(key)
          : Array.from(document.querySelectorAll('[data-organization-step]')).find(section => section.dataset.organizationStep === key);
        const selected = () => panel && !panel.hidden && panel.getClientRects().length > 0
          && control.getAttribute('aria-current') === (kind === 'event' ? 'page' : 'step');
        const finish = (durationMs, timedOut) => window.__localFeedback.samples.push({ key, durationMs, timedOut });
        const observe = () => {
          if (selected()) {
            // The first frame observes the new layout. The following frame
            // confirms it after the browser has had an intervening paint
            // opportunity; this is not GPU presentation or network latency.
            requestAnimationFrame(() => finish(performance.now() - startedAt, !selected()));
          } else if (performance.now() - startedAt > 2000) finish(performance.now() - startedAt, true);
          else requestAnimationFrame(observe);
        };
        requestAnimationFrame(observe);
      }, true);
    }, kind);
  }

  async function measureControls(kind, keys, locator) {
    await startMeasurement(kind);
    for (let index = 0; index < 20; index += 1) {
      await locator(keys[index % keys.length]).click();
      await expect.poll(() => page.evaluate(() => window.__localFeedback.samples.length)).toBe(index + 1);
    }
    return page.evaluate(() => window.__localFeedback.samples);
  }

  await login(page, 'owner@browser.test', 'Owner-pass1!');
  await page.goto('/events/1');
  const eventSamples = await measureControls('event', ['registrations', 'messages', 'feedback', 'overview'],
    key => page.locator(`[data-event-section-link][href="#${key}"]`));
  await login(page, 'trial-owner@browser.test', 'TrialOwner-pass1!');
  await page.goto('/setup?step=compliance');
  const form = page.locator('[data-organization-setup]');
  await fillWizard(form, 'Local feedback measurement');
  let writes = 0;
  page.on('request', request => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/setup') writes += 1;
  });
  const setupSamples = await measureControls('setup', ['organization', 'contact', 'representative', 'signup', 'messaging'],
    key => form.locator(`[data-organization-go="${key}"]`));
  function summarize(samples) {
    const values = samples.map(sample => sample.durationMs).sort((a, b) => a - b);
    return { n: values.length, p50Ms: values[Math.ceil(values.length * 0.5) - 1],
      p95Ms: values[Math.ceil(values.length * 0.95) - 1], maxMs: values.at(-1), samples };
  }
  const report = {
    scope: 'Local synthetic browser fixture only; not production, network, or exact GPU presentation latency.',
    method: 'Trusted click capture to matching visible selected panel and aria-current at requestAnimationFrame, then one following frame checkpoint.',
    host: new URL(page.url()).origin, browser: browserName, viewport: { width: 1440, height: 1000 },
    fixtures: { event: 'Seeded Acme Spring Launch event and fake providers', setup: 'Unsaved five-part Setup Runway Bakery form with synthetic values' },
    event: summarize(eventSamples), setup: summarize(setupSamples),
  };
  const evidencePath = info.outputPath('local-ui-feedback.json');
  await fs.mkdir(info.outputDir, { recursive: true });
  await fs.writeFile(evidencePath, JSON.stringify(report, null, 2));
  await info.attach('local-ui-feedback.json', { path: evidencePath, contentType: 'application/json' });
  expect(writes).toBe(0);
  for (const result of [report.event, report.setup]) {
    expect(result.n).toBe(20);
    expect(result.samples.every(sample => !sample.timedOut)).toBeTruthy();
    expect(result.p95Ms).toBeLessThan(400);
  }
});
