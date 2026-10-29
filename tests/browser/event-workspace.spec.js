const { test, expect } = require('@playwright/test');
const { login, installFailureDiagnostics, attachFailureDiagnostics } = require('./helpers');

test.beforeEach(async ({ page }) => installFailureDiagnostics(page));
test.afterEach(async ({ page }, info) => attachFailureDiagnostics(page, info));

for (const width of [1440, 390]) {
  test(`company event bookings and deliberate invitation at ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await login(page, 'owner@browser.test', 'Owner-pass1!');
    await page.goto('/events/add');
    await page.getByLabel('Event title').fill(`Company workshop ${width}`);
    await page.getByLabel('Date (optional)').fill('2030-12-04');
    await page.getByRole('button', { name: 'Create Event', exact: true }).click();
    const eventPath = new URL(page.url()).pathname;
    const phone = width === 390 ? '+17205558639' : '+17205558644';
    for (const [name, spaces, number] of [['Family booking', '10', phone], ['Second transaction', '3', phone], ['Guests without contact', '2', '']]) {
      await page.getByLabel('Name (optional)', { exact: true }).fill(name);
      await page.getByLabel('Phone number (optional)', { exact: true }).fill(number);
      await page.getByLabel('Reserved places', { exact: true }).fill(spaces);
      await page.getByRole('button', { name: 'Add registration', exact: true }).click();
    }
    const metrics = page.locator('.workspace-summary__stat');
    await expect(metrics.filter({ hasText: 'Separate active registrations' }).locator('.workspace-summary__stat-value')).toHaveText('3');
    await expect(metrics.filter({ hasText: 'Booked places;' }).locator('.workspace-summary__stat-value')).toHaveText('15');
    await expect(page.getByText('10 reserved places', { exact: false })).toBeVisible();
    await expect(page.getByText('Unverified coverage', { exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.screenshot({ path: info.outputPath(`event-workspace-${width}.png`), fullPage: false });
    // Cancel a transaction through the real mobile/desktop control. The other
    // booking keeps the shared messaging contact, and cancellation stays visible.
    const second = page.locator('#registrations article').filter({ hasText: 'Second transaction' });
    page.once('dialog', dialog => dialog.accept());
    await second.getByRole('button', { name: 'Cancel booking', exact: true }).click();
    await expect(metrics.filter({ hasText: 'Booked places;' }).locator('.workspace-summary__stat-value')).toHaveText('12');
    await expect(page.locator('#registrations article').filter({ hasText: 'Second transaction' })).toContainText('cancelled');
    await expect(page.locator('.table-responsive tbody tr')).toHaveCount(1);
    const compare = page.locator('details').filter({ has: page.getByText('Registration source comparison', { exact: true }) });
    await compare.locator('summary').click();
    await compare.getByRole('checkbox', { name: /I compared these bookings/ }).check();
    await compare.getByRole('button', { name: 'Approve local roster', exact: true }).click();
    await expect(page.getByText('Verified roster', { exact: true })).toBeVisible();
    const sections = page.getByRole('navigation', { name: 'Event workspace sections' });
    await sections.getByRole('link', { name: 'Messages', exact: true }).focus();
    await page.keyboard.press('Enter');
    await expect(page).toHaveURL(/#messages$/);
    await page.getByRole('link', { name: 'Announce to community', exact: true }).click();
    await expect(page.getByLabel('Community Blast', { exact: false })).toBeChecked();
    await expect(page.getByText(`Company workshop ${width}`, { exact: true }).first()).toBeVisible();
    await expect(page.getByText('No related schedules.', { exact: true })).toBeVisible();
    await page.goto(`${eventPath}/feedback/new`);
    await page.getByLabel('Feedback name', { exact: true }).fill(`Workshop feedback ${width}`);
    await page.getByLabel('Reply keyword').fill(`WORKSHOP${width}`);
    await page.getByRole('button', { name: 'Create feedback', exact: true }).click();
    await expect(page.getByText('No invitation has been scheduled', { exact: false })).toBeVisible();
    await page.getByRole('link', { name: 'Review invitation', exact: true }).click();
    await page.getByLabel('Registered contacts', { exact: true }).check();
    await page.getByLabel('Choose a date and time', { exact: true }).check();
    await page.getByLabel('Date', { exact: true }).fill('2030-12-05');
    await page.getByLabel('Time', { exact: true }).fill('10:30');
    await page.getByLabel('Timezone', { exact: true }).fill('America/Denver');
    await page.getByRole('button', { name: 'Review invitation', exact: true }).click();
    await expect(page.locator('[data-review="timing"]')).toContainText('2030-12-05');
    await expect(page.locator('[data-review="timing"]')).toContainText('America/Denver');
    await page.getByRole('button', { name: 'Confirm invitation schedule', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Invitations', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: /Invitation #.*Registered contacts/ }).first()).toBeVisible();
    await page.goto(eventPath);
    await expect(page.locator('#messages')).toContainText('Feedback invitation');
    await expect(page.locator('#overview')).toContainText('Feedback invitation');
    await login(page, 'staff@browser.test', 'Staff-pass1!');
    await page.goto(eventPath);
    expect(await page.content()).not.toContain(phone);
    await expect(page.getByText('Family booking', { exact: true }).first()).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    expect(errors).toEqual([]);
    await page.screenshot({ path: info.outputPath(`event-workspace-staff-${width}.png`), fullPage: false });
  });
}
