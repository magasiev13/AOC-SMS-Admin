# Browser libraries

These files preserve the exact library versions previously loaded by the app:

- Bootstrap 5.3.2: styles and bundled JavaScript, including Popper.
- Bootstrap Icons 1.11.1: workspace and sign-in styles and fonts.
- Bootstrap Icons 1.11.3: marketing styles and fonts.
- Chart.js 4.4.1: dashboard charts.

The two icon versions are intentionally retained. Each package directory includes
its upstream license. Asset contents are unmodified; the icon fonts retain the
relative directory structure used by their stylesheets.

`manifest.json` records the pinned download URL and SHA-256 digest of every
upstream file. The browser loads these files from the app's static directory.
Updating them requires an intentional library-version change and browser checks.
