const { test, expect } = require('@playwright/test');
const {
  attachFailureDiagnostics,
  installFailureDiagnostics,
} = require('./helpers');

test.beforeEach(async ({ page }) => {
  installFailureDiagnostics(page);
});

test.afterEach(async ({ page }, testInfo) => {
  await attachFailureDiagnostics(page, testInfo);
});

async function openHomeAt(page, width, height) {
  await page.setViewportSize({ width, height });
  await page.goto('/');
  await page.evaluate(() => document.fonts.ready);
  await expect(page.getByRole('heading', { name: /Reach them/i })).toBeVisible();
}

async function layoutMetrics(page) {
  return page.evaluate(() => {
    const rect = (selector) => {
      const element = document.querySelector(selector);
      if (!element) {
        throw new Error(`Missing layout target: ${selector}`);
      }
      const bounds = element.getBoundingClientRect();
      return {
        bottom: bounds.bottom,
        height: bounds.height,
        left: bounds.left,
        right: bounds.right,
        top: bounds.top,
        width: bounds.width,
      };
    };

    const promise = rect('.dispatch-promise');
    const textBounds = (element) => {
      const range = document.createRange();
      range.selectNodeContents(element);
      return range.getBoundingClientRect();
    };
    const heroLines = Array.from(document.querySelectorAll('.dispatch-promise h1 > *')).map((element) => {
      const bounds = textBounds(element);
      return {
        left: bounds.left,
        right: bounds.right,
        text: element.textContent,
      };
    });
    const offer = document.querySelector('.offer-ledger');
    const offerNote = document.querySelector('.offer-docket__note');
    const offerNoteBounds = offerNote.getBoundingClientRect();
    const offerNoteStyle = getComputedStyle(offerNote);
    const attempts = document.querySelector('.attempts-region');
    const footer = document.querySelector('.site-footer');
    const identity = rect('.dispatch-promise__identity');
    const docketPanel = rect('.dispatch-docket');
    const replyPanel = rect('.reply-margin');
    const replyResponse = rect('.reply-thread__response');
    const replyResponseIcon = rect('.reply-thread__response i');
    const replyConnector = getComputedStyle(
      document.querySelector('.reply-thread__response'),
      '::before'
    );
    const replyConnectorDot = getComputedStyle(
      document.querySelector('.reply-thread__response'),
      '::after'
    );
    const replyConnectorLineCenter = (
      -Number.parseFloat(replyConnector.right)
      - Number.parseFloat(replyConnector.width)
      + (Number.parseFloat(replyConnector.borderLeftWidth) / 2)
    );
    const replyConnectorDotCenter = (
      -Number.parseFloat(replyConnectorDot.right)
      - (Number.parseFloat(replyConnectorDot.width) / 2)
    );
    const replyItems = Array.from(document.querySelectorAll('.reply-thread span, .reply-thread time')).map((element) => {
      const bounds = textBounds(element);
      const style = getComputedStyle(element);
      return {
        clipped: ['clip', 'hidden'].includes(style.overflowX) && element.scrollWidth > element.clientWidth + 1,
        left: bounds.left,
        right: bounds.right,
        text: element.textContent,
      };
    });

    return {
      attemptsClientWidth: attempts.clientWidth,
      attemptsScrollWidth: attempts.scrollWidth,
      documentScrollWidth: document.documentElement.scrollWidth,
      footerBottom: footer.getBoundingClientRect().bottom + window.scrollY,
      footerHeight: footer.getBoundingClientRect().height,
      desktopActionsDisplay: getComputedStyle(document.querySelector('.desktop-actions')).display,
      desktopActions: rect('.desktop-actions'),
      headerBackground: getComputedStyle(document.querySelector('.site-header')).backgroundColor,
      header: rect('.site-header'),
      hero: rect('.dispatch-hero'),
      heroLines,
      identity,
      offerClientWidth: offer.clientWidth,
      offerItemCount: offer.children.length,
      offerNote: {
        fontSize: Number.parseFloat(offerNoteStyle.fontSize),
        ledgerBottom: offer.getBoundingClientRect().bottom,
        text: offerNote.textContent.trim(),
        top: offerNoteBounds.top,
      },
      offerScrollWidth: offer.scrollWidth,
      offerTerms: Array.from(document.querySelectorAll('.offer-ledger > div')).map((element) => {
        const metric = element.querySelector('strong').getBoundingClientRect();
        const descriptionElement = element.querySelector('dd');
        const description = descriptionElement.getBoundingClientRect();
        const descriptionStyle = getComputedStyle(descriptionElement);
        return {
          descriptionFontSize: Number.parseFloat(descriptionStyle.fontSize),
          descriptionFontWeight: Number.parseInt(descriptionStyle.fontWeight, 10),
          descriptionLeft: description.left,
          descriptionTop: description.top,
          metricBottom: metric.bottom,
          metricLeft: metric.left,
        };
      }),
      promise,
      docketPanel,
      mobileMenuDisplay: getComputedStyle(document.querySelector('.mobile-menu')).display,
      mobileMenuSummary: rect('.mobile-menu summary'),
      replyConnectorDisplay: getComputedStyle(
        document.querySelector('.reply-thread__response'),
        '::before'
      ).display,
      replyConnectorCenterError: Math.abs(
        replyConnectorLineCenter - replyConnectorDotCenter
      ),
      replyIconClearance: replyResponseIcon.left - replyResponse.left,
      replyItems,
      replyPanel,
      scrollHeight: document.documentElement.scrollHeight,
      viewportWidth: window.innerWidth,
    };
  });
}

function expectHorizontalBounds(items, container) {
  for (const item of items) {
    expect(item.left, `${item.text} starts outside its panel`).toBeGreaterThanOrEqual(container.left - 1);
    expect(item.right, `${item.text} ends outside its panel`).toBeLessThanOrEqual(container.right + 1);
  }
}

test('home geometry remains intact across the responsive breakpoint seams', async ({ page }) => {
  const viewports = [
    { width: 320, height: 844 },
    { width: 390, height: 844 },
    { width: 641, height: 900 },
    { width: 768, height: 1024 },
    { width: 960, height: 900 },
    { width: 961, height: 900 },
    { width: 1024, height: 900 },
    { width: 1180, height: 900 },
    { width: 1181, height: 900 },
    { width: 1200, height: 900 },
    { width: 1226, height: 900 },
    { width: 1600, height: 1000 },
  ];

  for (const viewport of viewports) {
    await openHomeAt(page, viewport.width, viewport.height);
    const metrics = await layoutMetrics(page);

    expect(metrics.documentScrollWidth).toBeLessThanOrEqual(metrics.viewportWidth + 1);
    expect(metrics.attemptsScrollWidth).toBeLessThanOrEqual(metrics.attemptsClientWidth + 1);
    expect(metrics.offerScrollWidth).toBeLessThanOrEqual(metrics.offerClientWidth + 1);
    expect(metrics.offerItemCount).toBe(4);
    expect(metrics.offerNote.text).toBe('* Provider approval is not guaranteed.');
    expect(metrics.offerNote.top).toBeGreaterThanOrEqual(metrics.offerNote.ledgerBottom - 1);
    expect(metrics.offerNote.fontSize).toBeLessThanOrEqual(14);
    expect(metrics.scrollHeight - metrics.footerBottom).toBeLessThanOrEqual(2);
    expect(metrics.footerHeight).toBeLessThan(850);
    expectHorizontalBounds(metrics.heroLines, metrics.promise);
    expectHorizontalBounds(metrics.replyItems, metrics.replyPanel);
    expect(metrics.replyItems.some((item) => item.clipped)).toBe(false);
    expect(metrics.replyIconClearance).toBeGreaterThanOrEqual(8);
    expect(metrics.identity.left).toBeGreaterThanOrEqual(metrics.promise.left - 1);
    expect(metrics.identity.right).toBeLessThanOrEqual(metrics.promise.right + 1);

    if (viewport.width <= 1180) {
      expect(metrics.desktopActionsDisplay).toBe('none');
      expect(metrics.mobileMenuDisplay).not.toBe('none');
      expect(metrics.mobileMenuSummary.left).toBeGreaterThanOrEqual(metrics.header.left - 1);
      expect(metrics.mobileMenuSummary.right).toBeLessThanOrEqual(metrics.header.right + 1);
    } else {
      expect(metrics.desktopActionsDisplay).not.toBe('none');
      expect(metrics.mobileMenuDisplay).toBe('none');
      expect(metrics.desktopActions.left).toBeGreaterThanOrEqual(metrics.header.left - 1);
      expect(metrics.desktopActions.right).toBeLessThanOrEqual(metrics.header.right + 1);
      expect(Math.abs(metrics.docketPanel.top - metrics.hero.top)).toBeLessThanOrEqual(1);
      expect(Math.abs(metrics.docketPanel.bottom - metrics.hero.bottom)).toBeLessThanOrEqual(1);
      expect(Math.abs(metrics.replyPanel.top - metrics.hero.top)).toBeLessThanOrEqual(1);
      expect(Math.abs(metrics.replyPanel.bottom - metrics.hero.bottom)).toBeLessThanOrEqual(1);
      expect(metrics.replyConnectorCenterError).toBeLessThanOrEqual(0.1);
    }

    for (const term of metrics.offerTerms) {
      expect(term.descriptionTop).toBeGreaterThanOrEqual(term.metricBottom - 1);
      expect(term.descriptionTop - term.metricBottom).toBeLessThanOrEqual(8);
      expect(Math.abs(term.descriptionLeft - term.metricLeft)).toBeLessThanOrEqual(1);
      expect(term.descriptionFontSize).toBeGreaterThanOrEqual(14);
      expect(term.descriptionFontWeight).toBeGreaterThanOrEqual(600);
    }

    if (viewport.width <= 1180) {
      expect(metrics.replyConnectorDisplay).toBe('none');
    } else {
      expect(metrics.replyConnectorDisplay).not.toBe('none');
    }
  }
});

test('wide marketing chrome spans the viewport without creating a blank right rail', async ({ page }) => {
  await openHomeAt(page, 2560, 1200);
  const metrics = await layoutMetrics(page);

  expect(metrics.header.left).toBe(0);
  expect(metrics.header.right).toBe(2560);
  expect(metrics.header.width).toBe(2560);
  expect(metrics.headerBackground).toBe('rgb(0, 24, 57)');
  expect(metrics.documentScrollWidth).toBeLessThanOrEqual(metrics.viewportWidth + 1);
  expect(metrics.scrollHeight - metrics.footerBottom).toBeLessThanOrEqual(2);
});

test('automated survey flows are a discoverable public capability', async ({ page }) => {
  await page.goto('/');
  await page.evaluate(() => document.fonts.ready);
  await expect(page.getByRole('heading', { name: 'Automated survey flows' })).toBeVisible();
  await expect(page.getByText(/A keyword can start a guided question sequence/)).toBeVisible();

  await page.goto('/features');
  await page.evaluate(() => document.fonts.ready);
  await expect(page.getByRole('heading', { name: 'Automated survey flows' })).toBeVisible();
  await expect(page.getByText(/Up to 10 sequential prompts/)).toBeVisible();
  await expect(page.getByText(/Searchable and exportable submissions/)).toBeVisible();
  await expect(page.getByText(/Optional event-registration sync/)).toBeVisible();
});

test('supporting public pages keep text and records inside their responsive planes', async ({ page }) => {
  test.setTimeout(120_000);

  const routes = [
    '/features',
    '/pricing',
    '/security',
    '/request-a-pilot',
    '/contact',
    '/privacy',
    '/terms',
    '/acceptable-use',
    '/sms-a2p-policy',
    '/billing-cancellation-refund-policy',
  ];
  const viewports = [
    { width: 320, height: 844 },
    { width: 390, height: 844 },
    { width: 641, height: 900 },
    { width: 960, height: 900 },
    { width: 961, height: 900 },
    { width: 1321, height: 900 },
  ];

  for (const route of routes) {
    for (const viewport of viewports) {
      await page.setViewportSize(viewport);
      await page.goto(route);
      await page.evaluate(() => document.fonts.ready);
      await expect(page.locator('main h1').first()).toBeVisible();

      const metrics = await page.evaluate(() => {
        const footer = document.querySelector('.site-footer');
        const header = document.querySelector('.site-header');
        if (!footer || !header) {
          throw new Error('Public page chrome is missing');
        }

        const isVisible = (element) => {
          const style = getComputedStyle(element);
          return style.display !== 'none' && style.visibility !== 'hidden';
        };
        const textTargets = Array.from(
          document.querySelectorAll(
            'main h1, main h2, main h3, main p, main dt, main dd, main legend, main label, main .button'
          )
        )
          .filter((element) => isVisible(element) && element.textContent.trim())
          .map((element) => {
            const elementBounds = element.getBoundingClientRect();
            const range = document.createRange();
            range.selectNodeContents(element);
            const textBounds = range.getBoundingClientRect();
            return {
              containerLeft: elementBounds.left,
              containerRight: elementBounds.right,
              left: textBounds.left,
              right: textBounds.right,
              text: element.textContent.trim().replace(/\s+/g, ' ').slice(0, 80),
            };
          });
        const clippedTargets = Array.from(document.querySelectorAll('main *'))
          .filter((element) => {
            if (
              element.matches('.visually-hidden, .form-honeypot, .form-honeypot *, .skip-link') ||
              !isVisible(element) ||
              !element.textContent.trim()
            ) {
              return false;
            }
            const style = getComputedStyle(element);
            return (
              ['clip', 'hidden'].includes(style.overflowX) &&
              element.scrollWidth > element.clientWidth + 1
            );
          })
          .map((element) => element.textContent.trim().replace(/\s+/g, ' ').slice(0, 80));

        return {
          clippedTargets,
          documentScrollWidth: document.documentElement.scrollWidth,
          footerBottom: footer.getBoundingClientRect().bottom + window.scrollY,
          footerHeight: footer.getBoundingClientRect().height,
          headerLeft: header.getBoundingClientRect().left,
          headerRight: header.getBoundingClientRect().right,
          scrollHeight: document.documentElement.scrollHeight,
          textTargets,
          viewportWidth: window.innerWidth,
        };
      });

      expect(metrics.documentScrollWidth, `${route} overflows at ${viewport.width}px`).toBeLessThanOrEqual(
        metrics.viewportWidth + 1
      );
      expect(metrics.headerLeft).toBe(0);
      expect(metrics.headerRight).toBe(viewport.width);
      expect(metrics.scrollHeight - metrics.footerBottom).toBeLessThanOrEqual(2);
      expect(metrics.footerHeight).toBeLessThan(850);
      expect(metrics.clippedTargets, `${route} clips text at ${viewport.width}px`).toEqual([]);

      for (const target of metrics.textTargets) {
        expect(target.left, `${route}: ${target.text} starts outside its box at ${viewport.width}px`).toBeGreaterThanOrEqual(
          target.containerLeft - 2
        );
        expect(target.right, `${route}: ${target.text} ends outside its box at ${viewport.width}px`).toBeLessThanOrEqual(
          target.containerRight + 2
        );
      }
    }
  }
});
