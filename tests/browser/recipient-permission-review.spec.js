const { test, expect } = require('@playwright/test');
const { login, installFailureDiagnostics, attachFailureDiagnostics } = require('./helpers');
const consoleByPage = new WeakMap();
const createdByPage = new WeakMap();

test.beforeEach(async ({ page }) => {
  installFailureDiagnostics(page);
  const messages = [];
  consoleByPage.set(page, messages);
  createdByPage.set(page, {});
  page.on('console', message => {
    if (['error', 'warning'].includes(message.type())) messages.push(message.text());
  });
});

test.afterEach(async ({ page }, testInfo) => {
  await attachFailureDiagnostics(page, testInfo);
  const created = createdByPage.get(page) || {};
  if (created.phone) {
    // Browser fetch preserves the authenticated browser's user agent. The
    // separate APIRequestContext can invalidate Flask's strong session binding.
    const html = await page.evaluate(async phone => (
      await fetch(`/community?search=${encodeURIComponent(phone)}`)
    ).text(), created.phone);
    const csrf = html.match(/name="csrf_token" value="([^"]+)"/);
    const member = html.match(/href="\/community\/(\d+)\/permission"/);
    const remove = async path => page.evaluate(async ({ path, token }) => {
      const response = await fetch(path, { method: 'POST', body: new URLSearchParams({ csrf_token: token }) });
      return { status: response.status, path: new URL(response.url).pathname };
    }, { path, token: csrf[1] });
    if (csrf && created.registration) {
      const result = await remove(created.registration);
      expect(result.status).toBe(200);
      expect(result.path).toMatch(/^\/events\/\d+$/);
    }
    if (csrf && member) {
      const result = await remove(`/community/${member[1]}/delete`);
      expect(result.status).toBe(200);
      expect(result.path).toBe('/community');
    }
  }
  const messages = consoleByPage.get(page) || [];
  await testInfo.attach('permission-console-health.json', { body: Buffer.from(JSON.stringify(messages)), contentType: 'application/json' });
  const unexpected = messages.filter(message => !/Failed to load resource.*status of (422|403)/i.test(message));
  expect(unexpected).toEqual([]);
});

function evidenceTime() {
  return new Date(Date.now() - 120_000).toISOString().slice(0, 16);
}

async function fillGrant(page) {
  await page.getByLabel('Permission decision').selectOption('granted');
  await page.getByLabel('Evidence source').fill('Retained browser opt-in form');
  await page.getByLabel('Evidence time (UTC)').fill(evidenceTime());
  await page.getByLabel('Evidence notes').fill('Synthetic contact explicitly agreed to these updates. Original opt-in retained in the isolated test fixture.');
}

async function noHorizontalOverflow(page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBeTruthy();
}

test('owner records verified community permission with preserved validation and keyboard flow', async ({ page }, testInfo) => {
  const pageErrors = [];
  page.on('pageerror', error => pageErrors.push(String(error)));
  await page.setViewportSize({ width: 1440, height: 1000 });
  await login(page, 'owner@browser.test', 'Owner-pass1!');
  await page.goto('/community/add');
  await page.getByLabel('Name (optional)').fill('Permission Review Desktop');
  await page.getByLabel('Phone number').fill('+17205550651');
  await page.getByRole('button', { name: 'Add Member', exact: true }).click();
  createdByPage.get(page).phone = '+17205550651';
  const row = page.locator('#communityTable tbody.table-data tr').filter({ hasText: 'Permission Review Desktop' });
  await expect(row.getByText('Unknown', { exact: true })).toBeVisible();
  await row.getByRole('link', { name: 'Review permission' }).click();
  await expect(page).toHaveURL(/\/community\/\d+\/permission$/);
  await expect(page).toHaveTitle(/Review SMS Permission/);
  await expect(page.getByRole('heading', { name: 'Record an owner review' })).toBeVisible();
  await noHorizontalOverflow(page);

  await fillGrant(page);
  await page.getByRole('button', { name: 'Save permission review' }).click();
  await expect(page.getByRole('alert').filter({ hasText: 'The review was not saved.' })).toBeVisible();
  await expect(page.getByLabel('Evidence source')).toHaveValue('Retained browser opt-in form');
  await expect(page.getByLabel('Evidence notes')).toHaveValue(/Synthetic contact explicitly agreed/);
  await expect(page.getByRole('checkbox', { name: /I verified that this person/ })).not.toBeChecked();

  await page.getByLabel('Permission decision').focus();
  await page.keyboard.press('Tab');
  await expect(page.getByLabel('Evidence source')).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(page.getByLabel('Evidence time (UTC)')).toBeFocused();
  // datetime-local has separate native date/time segments. Focus the next
  // field directly, then verify keyboard access through the confirmation.
  await page.getByLabel('Evidence notes').focus();
  await page.keyboard.press('Tab');
  const confirmation = page.getByRole('checkbox', { name: /I verified that this person/ });
  await expect(confirmation).toBeFocused();
  await page.keyboard.press('Space');
  await expect(confirmation).toBeChecked();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('button', { name: 'Save permission review' })).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page.getByText('Permission review saved. Current permission status: granted.')).toBeVisible();
  await expect(page.locator('[aria-labelledby="permission-current-title"]').getByText('Granted', { exact: true })).toBeVisible();
  await expect(page.locator('[aria-labelledby="permission-history-title"]').getByText('Retained browser opt-in form', { exact: true })).toHaveCount(1);
  await page.reload();
  await expect(page.locator('[aria-labelledby="permission-current-title"]').getByText('Granted', { exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('permission-review-desktop.png'), fullPage: true });
  await noHorizontalOverflow(page);
  expect(pageErrors).toEqual([]);
});

test('mobile event review grants only event messages while community remains unknown', async ({ page }, testInfo) => {
  const pageErrors = [];
  page.on('pageerror', error => pageErrors.push(String(error)));
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page, 'owner@browser.test', 'Owner-pass1!');
  await page.goto('/events');
  await page.locator('.card-list-item').filter({ hasText: 'Acme Spring Launch' }).getByRole('link', { name: 'View', exact: true }).click();
  await page.getByLabel('Name (optional)').fill('Permission Review Mobile');
  await page.getByLabel('Phone number').fill('+17205550652');
  await page.getByRole('button', { name: 'Add registration', exact: true }).click();
  createdByPage.get(page).phone = '+17205550652';
  const registration = page.locator('.card-list-item').filter({ hasText: 'Permission Review Mobile' });
  await expect(registration.getByText('Unknown', { exact: true })).toBeVisible();
  await registration.getByRole('link', { name: 'Review permission' }).click();
  const registrationPath = new URL(page.url()).pathname.match(/\/events\/(\d+)\/registrations\/(\d+)\/permission/);
  createdByPage.get(page).registration = `/events/${registrationPath[1]}/unregister/${registrationPath[2]}`;
  await expect(page).toHaveURL(/\/events\/\d+\/registrations\/\d+\/permission$/);
  await expect(page.getByText('updates for Acme Spring Launch', { exact: true })).toBeVisible();
  await noHorizontalOverflow(page);
  await fillGrant(page);
  await page.getByRole('checkbox', { name: /I verified that this person/ }).check();
  await page.getByRole('button', { name: 'Save permission review' }).click();
  await expect(page.locator('[aria-labelledby="permission-current-title"]').getByText('Granted', { exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('permission-review-mobile.png'), fullPage: true });
  await noHorizontalOverflow(page);
  await page.getByRole('link', { name: 'Back to event', exact: true }).click();
  await expect(registration.getByText('Granted', { exact: true })).toBeVisible();
  await page.goto('/community?search=17205550652');
  const contact = page.locator('.card-list-item').filter({ hasText: 'Permission Review Mobile' });
  await expect(contact.getByText('Unknown', { exact: true })).toBeVisible();
  await noHorizontalOverflow(page);
  expect(pageErrors).toEqual([]);
});

test('staff can see permission state but cannot open the owner review action', async ({ page }) => {
  await login(page, 'staff@browser.test', 'Staff-pass1!');
  await page.goto('/community');
  await expect(page.getByRole('link', { name: 'Review permission' })).toHaveCount(0);
  await expect(page.getByText('Granted', { exact: true }).first()).toBeVisible();
  const response = await page.goto('/community/1/permission');
  expect(response.status()).toBe(403);
});
