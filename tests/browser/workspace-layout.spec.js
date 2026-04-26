const { test, expect } = require('@playwright/test');
const {
  attachFailureDiagnostics,
  installFailureDiagnostics,
  login,
} = require('./helpers');

async function elementHeight(locator) {
  return locator.evaluate((element) => Math.round(element.getBoundingClientRect().height));
}

async function elementTop(locator) {
  return locator.evaluate((element) => Math.round(element.getBoundingClientRect().top));
}

test.beforeEach(async ({ page }) => {
  installFailureDiagnostics(page);
});

test.afterEach(async ({ page }, testInfo) => {
  await attachFailureDiagnostics(page, testInfo);
});

test('workspace desktop surfaces use the shared summary and collection shells', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  await login(page, 'owner@browser.test', 'Owner-pass1!');

  await page.goto('/dashboard');
  await expect(page.locator('.workspace-summary')).toBeVisible();
  await expect(page.locator('.workspace-summary__meta')).toHaveCount(0);
  await expect(page.locator('body')).not.toContainText('Sending enabled');
  await expect(page.locator('body')).not.toContainText('Trial active');
  await expect(page.locator('.workspace-command-layout')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Send messages and review replies in one workspace.' })).toHaveCount(0);

  await page.goto('/billing');
  await expect(page.locator('.workspace-summary')).toBeVisible();
  await expect(page.locator('.billing-summary-card.workspace-panel')).toBeVisible();
  await expect(page.locator('.billing-onboarding-card.workspace-panel')).toBeVisible();

  await page.goto('/community');
  await expect(page.locator('.collection-shell')).toBeVisible();
  await expect(page.locator('.collection-panel--search')).toBeVisible();
  await expect(page.locator('.collection-panel--results')).toBeVisible();
  expect(await elementHeight(page.getByRole('link', { name: 'Add Member' }))).toBeGreaterThanOrEqual(44);
  const firstCommunityRow = page.locator('#communityTable tbody.table-data tr').first();
  await expect(firstCommunityRow).toBeVisible();
  const firstCommunityPreview = firstCommunityRow.getByRole('button', { name: 'Preview member' });
  const firstCommunityEdit = firstCommunityRow.getByRole('link', { name: 'Edit member' });
  const firstCommunityMore = firstCommunityRow.getByRole('button', { name: 'More actions' });
  await expect(firstCommunityPreview).toBeVisible();
  await expect(firstCommunityEdit).toBeVisible();
  await expect(firstCommunityMore).toBeVisible();
  expect(await elementHeight(firstCommunityRow)).toBeLessThanOrEqual(96);
  expect(Math.abs((await elementTop(firstCommunityPreview)) - (await elementTop(firstCommunityEdit)))).toBeLessThanOrEqual(1);
  expect(Math.abs((await elementTop(firstCommunityPreview)) - (await elementTop(firstCommunityMore)))).toBeLessThanOrEqual(1);
  await firstCommunityMore.click();
  await expect(firstCommunityRow.getByRole('button', { name: 'Delete member' })).toBeVisible();

  await page.goto('/events');
  await expect(page.locator('.collection-shell')).toBeVisible();
  await expect(page.locator('.collection-panel--search')).toBeVisible();
  await expect(page.locator('.collection-panel--results')).toBeVisible();
  expect(await elementHeight(page.getByRole('link', { name: 'Create Event' }))).toBeGreaterThanOrEqual(44);

  await page.goto('/users');
  await expect(page.locator('.collection-shell')).toBeVisible();
  await expect(page.locator('.collection-panel--results').first()).toBeVisible();
  await expect(page.getByText('Add another platform admin to share platform access.')).toHaveCount(0);
  expect(await elementHeight(page.getByRole('link', { name: 'Invite Team Member' }))).toBeGreaterThanOrEqual(44);

  await page.goto('/scheduled');
  await expect(page.locator('.collection-shell')).toBeVisible();
  await expect(page.locator('.collection-panel--search')).toBeVisible();
  await expect(page.locator('.collection-panel').first()).toBeVisible();
  expect(
    await elementHeight(page.locator('.app-page-actions').getByRole('link', { name: 'Schedule from Dashboard' }))
  ).toBeGreaterThanOrEqual(44);

  await page.goto('/inbox');
  await expect(page.locator('.collection-panel--search')).toBeVisible();
  await expect(page.locator('.inbox-threads-card.collection-panel--results')).toBeVisible();
  await expect(page.locator('.workspace-panel').first()).toBeVisible();
  expect(await elementHeight(page.getByRole('link', { name: 'Survey Flows' }))).toBeGreaterThanOrEqual(44);
});

test('workspace mobile surfaces keep the shared action hierarchy and tap targets', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page, 'owner@browser.test', 'Owner-pass1!');

  await page.goto('/dashboard');
  await expect(page.locator('.workspace-summary')).toBeVisible();
  expect(await elementHeight(page.locator('#sendBtn'))).toBeGreaterThanOrEqual(44);

  await page.goto('/community');
  await expect(page.locator('.collection-panel--search')).toBeVisible();
  expect(await elementHeight(page.getByRole('link', { name: 'Add Member' }))).toBeGreaterThanOrEqual(44);
  const firstCommunityAction = page.locator('.card-list-item .row-actions .btn').first();
  if (await firstCommunityAction.count()) {
    expect(await elementHeight(firstCommunityAction)).toBeGreaterThanOrEqual(44);
  }

  await page.goto('/events');
  await expect(page.locator('.collection-panel--search')).toBeVisible();
  expect(await elementHeight(page.getByRole('link', { name: 'Create Event' }))).toBeGreaterThanOrEqual(44);
  const firstEventAction = page.locator('.card-list-item .row-actions .btn').first();
  if (await firstEventAction.count()) {
    expect(await elementHeight(firstEventAction)).toBeGreaterThanOrEqual(44);
  }

  await page.goto('/users');
  await expect(page.locator('.collection-shell')).toBeVisible();
  expect(await elementHeight(page.getByRole('link', { name: 'Invite Team Member' }))).toBeGreaterThanOrEqual(44);

  await page.goto('/scheduled');
  await expect(page.locator('.collection-panel--search')).toBeVisible();
  expect(
    await elementHeight(page.locator('.app-page-actions').getByRole('link', { name: 'Schedule from Dashboard' }))
  ).toBeGreaterThanOrEqual(44);

  await page.goto('/inbox');
  await expect(page.locator('.collection-panel--search')).toBeVisible();
  await expect(page.locator('.inbox-threads-card.collection-panel--results')).toBeVisible();
  expect(await elementHeight(page.getByRole('link', { name: 'Survey Flows' }))).toBeGreaterThanOrEqual(44);
});

test('lower-frequency workspace desktop pages use the shared detail and filter primitives', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  await login(page, 'owner@browser.test', 'Owner-pass1!');

  await page.goto('/events');
  await page.getByRole('link', { name: 'Acme Spring Launch' }).click();
  await expect(page.locator('.workspace-summary')).toBeVisible();
  await expect(page.locator('.workspace-detail-layout')).toBeVisible();
  expect(await elementHeight(page.getByRole('button', { name: 'Add registration' }))).toBeGreaterThanOrEqual(44);

  await page.goto('/inbox/surveys');
  await expect(page.locator('.workspace-summary')).toBeVisible();
  await expect(page.locator('.collection-shell')).toBeVisible();
  expect(
    await elementHeight(page.locator('.app-page-actions').getByRole('link', { name: 'Add Survey Flow' }))
  ).toBeGreaterThanOrEqual(44);

  await page.goto('/inbox/surveys/add');
  await expect(page.locator('.workspace-summary')).toBeVisible();
  await expect(page.locator('.workspace-form-layout')).toBeVisible();
  await expect(page.getByText('When a contact sends this keyword, the survey starts.')).toHaveCount(0);

  await page.goto('/unsubscribed');
  await expect(page.locator('.workspace-summary')).toBeVisible();
  await expect(page.locator('.collection-shell')).toBeVisible();

  await page.goto('/security/events');
  await expect(page.locator('.workspace-summary')).toBeVisible();
  await expect(page.locator('.collection-panel--filters')).toBeVisible();
  expect(await elementHeight(page.getByRole('button', { name: 'Apply' }))).toBeGreaterThanOrEqual(44);

  await page.goto('/settings/test-recipients');
  await expect(page.locator('.workspace-summary')).toBeVisible();
  await expect(page.locator('.workspace-detail-layout')).toBeVisible();
  await expect(page.getByText('These recipients are available in dashboard test mode')).toHaveCount(0);

  await page.goto('/team/invite');
  await expect(page.locator('.workspace-summary')).toBeVisible();
  await expect(page.locator('.workspace-form-layout')).toBeVisible();
});

test('lower-frequency workspace mobile pages keep primary actions tappable', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page, 'owner@browser.test', 'Owner-pass1!');

  await page.goto('/events/1');
  await expect(page.locator('.workspace-detail-layout')).toBeVisible();
  expect(await elementHeight(page.getByRole('button', { name: 'Add registration' }))).toBeGreaterThanOrEqual(44);

  await page.goto('/inbox/surveys/add');
  await expect(page.locator('.workspace-form-layout')).toBeVisible();
  expect(await elementHeight(page.getByRole('button', { name: 'Save survey flow' }))).toBeGreaterThanOrEqual(44);

  await page.goto('/unsubscribed');
  await expect(page.locator('.collection-shell')).toBeVisible();
  expect(await elementHeight(page.getByRole('button', { name: 'Select all' }))).toBeGreaterThanOrEqual(44);

  await page.goto('/security/events');
  await expect(page.locator('.collection-panel--filters')).toBeVisible();
  expect(await elementHeight(page.getByRole('button', { name: 'Apply' }))).toBeGreaterThanOrEqual(44);

  await page.goto('/settings/test-recipients');
  await expect(page.locator('.workspace-detail-layout')).toBeVisible();
  expect(await elementHeight(page.getByRole('button', { name: 'Add recipient' }))).toBeGreaterThanOrEqual(44);
  expect(await elementHeight(page.getByRole('button', { name: 'Save changes' }))).toBeGreaterThanOrEqual(44);

  await page.goto('/team/invite');
  await expect(page.locator('.workspace-form-layout')).toBeVisible();
  const inviteAction = page.locator('.workspace-form-layout form button[type="submit"]').first();
  expect(await elementHeight(inviteAction)).toBeGreaterThanOrEqual(44);
});
