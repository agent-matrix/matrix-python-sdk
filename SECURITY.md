# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in this repository, please report it
responsibly. Do **not** open a public issue.

- Use GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability) on this repository.
- Or email the maintainers listed in `CODEOWNERS`.

We aim to acknowledge reports within 3 business days and provide a remediation
timeline within 10 business days for accepted reports.

## Supported Versions

The latest minor release is supported with security fixes. Older versions are
supported on a best-effort basis.

## Scope

In scope:

- Source code in this repository
- Build, test, and release workflows under `.github/workflows/`
- Documented APIs, CLIs, manifests, and runtime artifacts produced by this repo

Out of scope:

- Third-party dependencies (please report upstream first)
- Self-hosted misconfiguration of an operator's environment

## Coordinated Disclosure

This project follows coordinated disclosure. Please give maintainers a
reasonable window to remediate before any public disclosure.
