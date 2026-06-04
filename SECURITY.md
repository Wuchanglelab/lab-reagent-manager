# Security Policy

## Supported Versions

This repository is early-stage open source software. Security fixes are currently made on the `main` branch.

## Reporting a Vulnerability

Please report security issues privately to the project maintainers instead of opening a public issue. If no dedicated security contact is configured on GitHub yet, contact the repository owner through the GitHub profile and include:

- A short description of the issue.
- Steps to reproduce.
- Potential impact.
- Whether any real lab data, credentials, or uploaded files may be exposed.

The maintainers will review and respond as soon as possible.

## Deployment Guidance

Lab Reagent Manager is designed for trusted lab teams and internal workflows. Before deploying it on a public URL:

- Set a strong `LAB_ADMIN_PASSWORD`.
- Put the app behind institutional SSO, a VPN, a reverse-proxy allowlist, or another access-control layer when possible.
- Use PostgreSQL or a persistent disk for production data.
- Do not commit `.env`, API keys, SMTP passwords, uploaded files, or SQLite databases.
- Review upload handling and allowed image types for your hosting environment.
- Configure SMTP and AI API keys only through environment variables.
- Treat reagent inventories, procurement records, names, emails, and uploaded labels as potentially sensitive lab data.

## Current Limitations

- The app is not a regulated LIMS and should not be used as the sole compliance system for regulated clinical, manufacturing, or safety-critical workflows.
- Some workflow endpoints are intended for trusted internal use. If you expose the app broadly, add stronger authentication and authorization before relying on it.
- AI-assisted recognition and inventory review can make mistakes. Human review is required before acting on AI-generated reagent metadata, hazard information, or procurement suggestions.
