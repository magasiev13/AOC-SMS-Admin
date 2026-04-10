const { defineConfig } = require('@playwright/test');

const port = process.env.PLAYWRIGHT_PORT || '5010';
const baseURL = process.env.PLAYWRIGHT_BASE_URL || `http://127.0.0.1:${port}`;
const artifactRoot = process.env.PLAYWRIGHT_ARTIFACT_DIR || 'output/playwright';

module.exports = defineConfig({
  testDir: './tests/browser',
  timeout: 30_000,
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
  webServer: {
    command: `PLAYWRIGHT_PORT=${port} PLAYWRIGHT_BASE_URL=${baseURL} ./run/playwright_web.sh`,
    url: `${baseURL}/login`,
    timeout: 120_000,
    reuseExistingServer: false,
  },
});
