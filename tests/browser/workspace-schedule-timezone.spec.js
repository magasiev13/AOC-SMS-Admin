const { test, expect } = require('@playwright/test');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const { login, confirmSendReview, installFailureDiagnostics, attachFailureDiagnostics } = require('./helpers');

test.use({ timezoneId: 'Asia/Tokyo', viewport: { width: 390, height: 900 } });
test.beforeEach(async ({ page }) => installFailureDiagnostics(page));
test.afterEach(async ({ page }, info) => attachFailureDiagnostics(page, info));

test('a different browser zone schedules 6 PM workspace time throughout review and confirmation', async ({ page }, info) => {
  await login(page, 'owner@browser.test', 'Owner-pass1!');
  await page.goto('/dashboard');
  const workspaceZone = await page.locator('#workspaceScheduleTimezone').getAttribute('data-workspace-timezone');
  expect(workspaceZone).not.toBe('Asia/Tokyo');
  await page.locator('.advanced-options__summary').click();
  await page.getByLabel('Schedule for Later').check();
  await expect(page.locator('#browserTimezoneNotice')).toBeVisible();
  await expect(page.locator('#browserTimezoneNotice')).toContainText('Your browser uses Asia/Tokyo.');
  await expect(page.locator('#browserTimezoneNotice')).toContainText('workspace time');
  if (workspaceZone === 'America/Denver') {
    await expect(page.locator('#workspaceScheduleTimezone')).toHaveText('Times are in Mountain Time (Denver).');
  }

  // Independent stdlib calculation and read-only query of the fake stack's
  // local fixture database. No test endpoint, provider or external service.
  const python = path.resolve(__dirname, '../../venv/bin/python');
  const expected = JSON.parse(execFileSync(python, ['-c', `
import json, sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
local = datetime(2099, 12, 31, 18, 0, tzinfo=ZoneInfo(sys.argv[1]))
print(json.dumps({'stored': local.astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=' ', timespec='seconds'),
                  'label': 'Thu, Dec 31, 2099 · 6:00 PM ' + local.tzname()}))
`, workspaceZone], { encoding: 'utf8' }));
  const message = `Workspace schedule from Tokyo ${info.testId}`;
  await page.getByLabel('Message', { exact: true }).fill(message);
  await page.getByLabel('Date', { exact: true }).fill('2099-12-31');
  await page.getByLabel('Time', { exact: true }).fill('18:00');
  await page.getByRole('button', { name: 'Review Schedule', exact: true }).click();
  const review = page.getByRole('dialog', { name: 'Review Message', exact: true });
  await expect(review.locator('[data-review="timing"]')).toHaveText(expected.label);
  await page.screenshot({ path: info.outputPath('workspace-schedule-review.png'), fullPage: false });
  await confirmSendReview(page);
  await expect(page).toHaveURL(/\/scheduled$/);
  await expect(page.locator('.app-toast[role="status"]')).toContainText(`Message scheduled for ${expected.label}.`);
  const card = page.locator('.card-list-item').filter({ hasText: message }).first();
  await expect(card).toContainText(expected.label);
  expect(new URL(page.url()).hostname).toMatch(/^(127\.0\.0\.1|localhost)$/);
  const databasePath = process.env.PLAYWRIGHT_DB_PATH || path.resolve(__dirname, '../../.playwright/browser-tests.db');
  const stored = execFileSync(python, ['-c', `
import sqlite3, sys
from pathlib import Path
with sqlite3.connect(Path(sys.argv[1]).resolve().as_uri() + '?mode=ro', uri=True) as db:
    row = db.execute('SELECT scheduled_at FROM scheduled_messages WHERE message_body LIKE ?', (sys.argv[2] + '%',)).fetchone()
    assert row is not None, 'The reviewed schedule must have been committed.'
    print(row[0])
`, databasePath, message], { encoding: 'utf8' }).trim();
  expect(stored.replace(/\.0+$/, '')).toBe(expected.stored);
  await page.screenshot({ path: info.outputPath('workspace-schedule-confirmed.png'), fullPage: false });
  await card.getByRole('button', { name: 'Cancel', exact: true }).click();
  await page.getByRole('button', { name: 'Yes', exact: true }).click();
});
