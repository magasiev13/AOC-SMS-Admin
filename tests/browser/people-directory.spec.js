const { test, expect } = require('@playwright/test');
const { login } = require('./helpers');

test.use({ hasTouch: true });

for (const width of [320, 390, 909]) {
  test(`People header, filters and contact actions fit at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await login(page, 'owner@browser.test', 'Owner-pass1!');
    await page.goto('/community');

    const header = page.locator('.app-page-header');
    const add = header.getByRole('link', { name: 'Add Contact' });
    const more = header.getByRole('button', { name: 'More Actions' });
    const addBounds = await add.boundingBox();
    const moreBounds = await more.boundingBox();
    expect(Math.abs(moreBounds.y - addBounds.y)).toBeLessThan(2);
    expect(addBounds.height).toBeGreaterThanOrEqual(44);
    await more.tap();
    await expect(header.getByRole('link', { name: 'Import CSV' })).toBeVisible();
    await more.tap();

    const views = page.getByRole('navigation', { name: 'Contact Views' });
    await views.getByRole('link', { name: 'All Contacts' }).tap();
    await expect(views.getByRole('link', { name: 'All Contacts' })).toHaveAttribute('aria-current', 'page');
    const search = page.getByRole('textbox', { name: 'Search Contacts' });
    const first = page.locator('.people-row').first();
    const name = await first.locator('.people-identity strong').innerText();
    await search.fill(name);
    await search.press('Enter');
    await expect(first).toContainText(name);

    const actions = first.getByRole('button', { name: 'Actions', exact: true });
    await actions.tap();
    await first.getByRole('button', { name: 'Preview' }).tap();
    await expect(page.getByRole('dialog', { name })).toBeVisible();
    await page.getByRole('button', { name: 'Close Contact Preview' }).tap();
    await actions.tap();
    await expect(first.getByRole('button', { name: 'Preview' })).toBeHidden();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
  });
}

test('selected contacts survive switching layouts and match the bulk operation', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page, 'owner@browser.test', 'Owner-pass1!');
  await page.goto('/community');
  const rowBox = page.locator('.people-row .member-checkbox').first();
  const id = await rowBox.inputValue();
  await rowBox.check();
  await expect(page.locator('#selectedCount')).toHaveText('1');
  await page.setViewportSize({ width: 1440, height: 900 });
  await expect(page.locator(`#communityTable .member-checkbox[value="${id}"]`)).toBeChecked();
  const selectedIds = () => page.locator('#bulkDeleteForm').evaluate(form => new FormData(form).getAll('member_ids'));
  expect(await selectedIds()).toEqual([id]);
  await page.getByRole('button', { name: 'Delete Selected' }).click();
  const confirmation = page.getByRole('dialog', { name: 'Delete Selected Contacts' });
  await expect(confirmation.locator('#bulkDeleteCount')).toHaveText('1');
  await confirmation.getByRole('button', { name: 'Cancel' }).click();
  await page.getByRole('button', { name: 'Clear selection' }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(rowBox).not.toBeChecked();
  await page.getByRole('checkbox', { name: 'Select This Page' }).check();
  const ids = await selectedIds();
  expect(ids.length).toEqual(await page.locator('.people-row').count());
  expect(new Set(ids).size).toEqual(ids.length);
});

test('a maximum-length contact name leaves the desktop actions in view', async ({ page }) => {
  await login(page, 'owner@browser.test', 'Owner-pass1!');
  await page.goto('/community');
  const table = page.locator('#communityTable');
  await table.locator('td.people-name').first().evaluate(cell => { cell.textContent = 'A'.repeat(100); });
  for (const width of [992, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await table.evaluate(element => element.parentElement.scrollWidth - element.parentElement.clientWidth)).toBeLessThanOrEqual(1);
    await expect(table.getByRole('button', { name: 'Preview Contact' }).first()).toBeInViewport();
  }
});
