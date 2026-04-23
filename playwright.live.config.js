const { defineConfig } = require('@playwright/test');

const baseURL = process.env.TWINEVIA_LIVE_BASE_URL || process.env.PLAYWRIGHT_BASE_URL || 'https://www.twinevia.com';
const artifactRoot = process.env.PLAYWRIGHT_ARTIFACT_DIR || 'output/playwright-live';

module.exports = defineConfig({
  testDir: './tests/browser',
  testMatch: ['**/live-production-smoke.spec.js'],
  timeout: 45_000,
  expect: {
    timeout: 5_000,
  },
  fullyParallel: false,
  workers: 1,
  reporter: [
    ['list'],
    ['html', { open: 'never', outputFolder: `${artifactRoot}/report` }],
  ],
  outputDir: `${artifactRoot}/test-results`,
  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    headless: true,
  },
});
