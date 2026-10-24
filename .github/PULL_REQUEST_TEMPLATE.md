## What changed and why?

<!-- Describe the problem, your change and the related issue. Open this PR as a draft against main. -->

## Local checks

<!-- List exact commands and results. Run targeted tests while working. Push through the hook (python scripts/local_ci.py install, once per clone): it runs the offline secret scan on every push and no test suites. Full suites run on GitHub CI for every PR, drafts included; merge only after CI is green and reviewed. Include head-SHA CI run links. Edge/nightly publication needs a hosted main-suite pass or an attested local full run on main. -->

## Running-app checks

<!-- Say what you exercised in the app, or explicitly say it was tested only with fixtures. For visible changes, describe in text what you checked and at which widths; do not attach or link screenshots. Never include secrets or private data. -->

## Not covered here

<!-- Describe any checks you could not run, including manual-only CI (packaging, installers, macOS, Android Gradle). -->

## Contributor sign-off

- [ ] Every commit has a DCO `Signed-off-by: Your Name <your-email>` line added with `git commit -s`.
- [ ] I have read [CONTRIBUTING.md](../CONTRIBUTING.md) and kept this PR in draft for review.
