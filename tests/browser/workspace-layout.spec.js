const { test, expect } = require('@playwright/test');
const {
  attachFailureDiagnostics,
  installFailureDiagnostics,
  login,
  renderedElementMetric,
} = require('./helpers');

async function elementHeight(locator) {
  return renderedElementMetric(locator, 'height');
}

async function elementTop(locator) {
  return renderedElementMetric(locator, 'top');
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
  await expect(page.locator('#workspaceOverview')).not.toHaveAttribute('open', '');
  await page.locator('#workspaceOverview > summary').click();
  await expect(page.locator('#workspaceOverview')).toHaveAttribute('open', '');
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
  expect(await elementHeight(page.getByRole('link', { name: 'Add Contact' }))).toBeGreaterThanOrEqual(44);
  const firstCommunityRow = page.locator('#communityTable tbody.table-data tr').first();
  await expect(firstCommunityRow).toBeVisible();
  const firstCommunityPreview = firstCommunityRow.getByRole('button', { name: 'Preview Contact' });
  const firstCommunityEdit = firstCommunityRow.getByRole('link', { name: 'Edit Contact' });
  const firstCommunityMore = firstCommunityRow.getByRole('button', { name: 'More Actions' });
  await expect(firstCommunityPreview).toBeVisible();
  await expect(firstCommunityEdit).toBeVisible();
  await expect(firstCommunityMore).toBeVisible();
  expect(await elementHeight(firstCommunityRow)).toBeLessThanOrEqual(96);
  expect(Math.abs((await elementTop(firstCommunityPreview)) - (await elementTop(firstCommunityEdit)))).toBeLessThanOrEqual(1);
  expect(Math.abs((await elementTop(firstCommunityPreview)) - (await elementTop(firstCommunityMore)))).toBeLessThanOrEqual(1);
  await firstCommunityMore.click();
  await expect(firstCommunityRow.getByRole('button', { name: 'Delete Contact' })).toBeVisible();

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
    await elementHeight(page.locator('.app-page-actions').getByRole('link', { name: 'Schedule a Message' }))
  ).toBeGreaterThanOrEqual(44);

  await page.goto('/inbox');
  if (await page.getByRole('link', { name: 'Back to Conversations' }).isVisible()) {
    await page.getByRole('link', { name: 'Back to Conversations' }).click();
  }
  await expect(page.locator('.collection-panel--search')).toBeVisible();
  await expect(page.locator('.inbox-threads-card.collection-panel--results')).toBeVisible();
  await expect(page.locator('.workspace-panel').first()).toBeVisible();
  expect(await elementHeight(page.getByRole('link', { name: 'Surveys' }))).toBeGreaterThanOrEqual(44);
});

test('workspace mobile surfaces keep the shared action hierarchy and tap targets', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page, 'owner@browser.test', 'Owner-pass1!');

  await page.goto('/dashboard');
  await expect(page.locator('#workspaceOverview')).not.toHaveAttribute('open', '');
  expect((await page.locator('#message_body').boundingBox()).y).toBeLessThan(500);
  expect(await elementHeight(page.locator('#sendBtn'))).toBeGreaterThanOrEqual(44);

  await page.goto('/community');
  await expect(page.locator('.collection-panel--search')).toBeVisible();
  expect(await elementHeight(page.getByRole('link', { name: 'Add Contact' }))).toBeGreaterThanOrEqual(44);
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
    await elementHeight(page.locator('.app-page-actions').getByRole('link', { name: 'Schedule a Message' }))
  ).toBeGreaterThanOrEqual(44);

  await page.goto('/inbox');
  if (await page.getByRole('link', { name: 'Back to Conversations' }).isVisible()) {
    await page.getByRole('link', { name: 'Back to Conversations' }).click();
  }
  await expect(page.locator('.collection-panel--search')).toBeVisible();
  await expect(page.locator('.inbox-threads-card.collection-panel--results')).toBeVisible();
  expect(await elementHeight(page.getByRole('link', { name: 'Surveys' }))).toBeGreaterThanOrEqual(44);
});

test('lower-frequency workspace desktop pages use the shared detail and filter primitives', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  await login(page, 'owner@browser.test', 'Owner-pass1!');

  await page.goto('/events');
  await page.getByRole('link', { name: 'Acme Spring Launch' }).click();
  await expect(page.locator('.event-summary')).toBeVisible();
  await page.getByRole('navigation', { name: 'Event workspace sections' }).getByRole('link', { name: 'Registrations', exact: true }).click();
  await page.locator('[data-event-registration-tool] > summary').filter({ hasText: 'Add Booking' }).click();
  await expect(page.locator('.workspace-detail-layout')).toBeVisible();
  expect(await elementHeight(page.getByRole('button', { name: 'Add Registration' }))).toBeGreaterThanOrEqual(44);

  await page.goto('/inbox/surveys');
  await expect(page.getByRole('search', { name: 'Search surveys by name, keyword or message' })).toBeVisible();
  await expect(page.locator('.collection-shell')).toBeVisible();
  expect(
    await elementHeight(page.locator('.app-page-actions').getByRole('link', { name: 'Add Survey' }))
  ).toBeGreaterThanOrEqual(44);

  await page.goto('/inbox/surveys/add');
  await expect(page.getByRole('heading', { name: 'Add Survey', exact: true })).toHaveCount(1);
  await expect(page.getByRole('form', { name: 'Add Survey', exact: true })).toBeVisible();
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
  await expect(page.getByRole('heading', { name: 'Invite a Team Member', exact: true })).toBeVisible();
  await expect(page.locator('.workspace-form-layout')).toBeVisible();
});

test('workspace forms share a title, return link and first-field alignment', async ({ page }) => {
  await login(page, 'owner@browser.test', 'Owner-pass1!');
  const routes = [
    ['/community/add', 'People', 'Name (Optional)'],
    ['/events/add', 'Events', 'Event Title'],
    ['/events/1/feedback/new', 'Acme Spring Launch · Feedback', 'Feedback Name'],
    ['/inbox/keywords/add', 'Automatic Replies', 'Keyword'],
    ['/team/invite', 'Team access', 'Email'],
    ['/unsubscribed/add', 'Opt-Outs & Blocks', 'Name (Optional)'],
    ['/unsubscribed/import', 'Opt-Outs & Blocks', 'Upload CSV'],
  ];
  for (const width of [390, 1440]) {
    await page.setViewportSize({ width, height: 844 });
    for (const [path, parent, label] of routes) {
      await page.goto(path);
      const header = page.locator('.app-page-header');
      const form = page.getByRole('main').getByRole('form');
      await expect(header.getByRole('link', { name: parent, exact: true })).toBeVisible();
      await expect(page.getByRole('main').getByRole('heading')).toHaveCount(1);
      const field = form.getByLabel(label);
      await expect(field).toBeInViewport();
      const [headerBox, panelBox, fieldBox] = await Promise.all([
        header.boundingBox(), page.locator('.workspace-panel').boundingBox(), field.boundingBox(),
      ]);
      expect(headerBox.x).toBeCloseTo(panelBox.x, 0);
      expect(headerBox.width).toBeCloseTo(panelBox.width, 0);
      expect(fieldBox.y - headerBox.y - headerBox.height).toBeLessThan(100);
      const cancel = form.getByRole('link', { name: 'Cancel', exact: true });
      const parentUrl = await header.getByRole('link').getAttribute('href');
      await expect(cancel).toHaveAttribute('href', parentUrl);
    }
  }
});

test('lower-frequency workspace mobile pages keep primary actions tappable', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page, 'owner@browser.test', 'Owner-pass1!');

  await page.goto('/events/1');
  await page.getByRole('navigation', { name: 'Event workspace sections' }).getByRole('link', { name: 'Registrations', exact: true }).click();
  await page.locator('[data-event-registration-tool] > summary').filter({ hasText: 'Add Booking' }).click();
  await expect(page.locator('.workspace-detail-layout')).toBeVisible();
  expect(await elementHeight(page.getByRole('button', { name: 'Add Registration' }))).toBeGreaterThanOrEqual(44);

  await page.goto('/inbox/surveys/add');
  await expect(page.locator('.workspace-form-layout')).toBeVisible();
  expect(await elementHeight(page.getByRole('button', { name: 'Save Survey' }))).toBeGreaterThanOrEqual(44);

  await page.goto('/unsubscribed');
  await expect(page.locator('.collection-shell')).toBeVisible();
  expect(await elementHeight(page.getByRole('button', { name: 'Select All', exact: true }))).toBeGreaterThanOrEqual(44);

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

test('compact collections and recipient settings keep controls inside their panels', async ({ page }) => {
  await login(page, 'owner@browser.test', 'Owner-pass1!');
  for (const width of [320, 1024]) {
    await page.setViewportSize({ width, height: 844 });
    await page.goto('/inbox?view=threads');
    const search = page.getByRole('textbox', { name: 'Search inbox threads' });
    const icon = page.locator('#inboxThreadsPanel .input-group-text');
    expect(Math.abs((await elementTop(icon)) - (await elementTop(search)))).toBeLessThanOrEqual(1);
    await search.fill('Maya');
    await search.press('Enter');
    await expect(page).toHaveURL(/search=Maya/);
    await expect(page.locator('.inbox-thread-link')).toContainText('Maya Chen');
  }

  await page.goto('/settings/test-recipients');
  const rows = page.locator('#recipientRows .recipient-row');
  const initialCount = await rows.count();
  await page.getByRole('button', { name: 'Add recipient' }).click();
  await expect(rows).toHaveCount(initialCount + 1);
  const added = rows.last();
  const remove = added.getByRole('button', { name: 'Remove' });
  expect(await renderedElementMetric(remove, 'right')).toBeLessThanOrEqual(await renderedElementMetric(added, 'right'));
  await remove.click();
  await expect(rows).toHaveCount(initialCount);

  await page.goto('/inbox/surveys');
  const card = page.locator('.survey-flows-cards .card-list-item').first();
  await card.getByRole('link', { name: 'Submissions' }).click();
  await expect(page).toHaveURL(/\/(submissions|feedback\/\d+)$/);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/inbox/surveys');
  await expect(page.locator('.survey-flows-table-wrapper')).toBeVisible();
  await expect(page.locator('.survey-flows-cards')).toBeHidden();
});

test('team access opens a named, editable account with adjacent access settings', async ({ page }) => {
  await page.setViewportSize({ width: 930, height: 844 });
  await login(page, 'owner@browser.test', 'Owner-pass1!');
  await page.goto('/settings');
  await page.getByRole('link', { name: /Team Access/i }).click();
  await expect(page.getByRole('heading', { name: 'Team access', exact: true })).toBeVisible();
  const member = page.locator('.team-directory-cards .card-list-item').filter({ hasText: 'staff-browser' });
  await expect(member.getByRole('heading')).toHaveText('Staff Browser');
  await member.getByRole('button', { name: 'Delete user Staff Browser' }).click();
  const confirmation = page.getByRole('dialog', { name: 'Delete user' });
  await expect(confirmation).toContainText('Staff Browser (staff-browser)');
  await confirmation.getByRole('button', { name: 'Cancel' }).click();
  await member.getByRole('link', { name: 'Edit user' }).click();
  const editUrl = page.url();
  const fullName = page.getByLabel('Full name');
  const originalName = await fullName.inputValue();
  await expect(page.getByRole('heading', { level: 1 })).toHaveText(`Edit ${originalName}`);
  await expect(page.getByRole('combobox', { name: 'Role' })).toHaveValue('social_manager');
  const username = page.getByLabel('Username');
  expect(Math.abs((await elementTop(fullName)) - (await elementTop(username)))).toBeLessThanOrEqual(1);
  expect(await elementTop(page.getByRole('button', { name: 'Save changes' }))).toBeLessThan(780);
  await page.locator('.member-password-settings > summary').click();
  await expect(page.getByLabel('New password')).toBeVisible();

  await page.setViewportSize({ width: 390, height: 844 });
  expect(await elementTop(fullName)).toBeLessThan(await elementTop(username));
  await fullName.fill('Temporary team edit');
  await username.fill('owner-browser');
  await page.getByRole('button', { name: 'Save changes' }).click();
  await expect(page.getByRole('alert')).toContainText('already exists');
  await expect(fullName).toHaveValue('Temporary team edit');
  await expect(username).toHaveValue('owner-browser');

  await fullName.fill('   ');
  await username.fill('staff-browser');
  await page.getByRole('button', { name: 'Save changes' }).click();
  await expect(page).toHaveURL(/\/users$/);
  await expect(member.getByRole('heading')).toHaveText('staff-browser');
  await page.goto(editUrl);
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Edit staff-browser');
  await fullName.fill(originalName);
  await page.getByRole('button', { name: 'Save changes' }).click();
  await expect(page).toHaveURL(/\/users$/);
  await page.setViewportSize({ width: 1440, height: 900 });
  const table = page.getByRole('table').filter({ has: page.getByRole('button', { name: 'Team member' }) });
  await expect(table.locator('tbody tr').filter({ hasText: 'staff-browser' }).locator('strong')).toHaveText(originalName);
  const byName = table.getByRole('button', { name: 'Team member' });
  await byName.click();
  const ascending = await table.locator('tbody tr td:first-child strong').allTextContents();
  await byName.click();
  expect(await table.locator('tbody tr td:first-child strong').allTextContents()).toEqual(ascending.reverse());
  await page.goto(editUrl);
  await expect(fullName).toHaveValue(originalName);
});
