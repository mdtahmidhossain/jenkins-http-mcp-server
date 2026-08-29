# Security Policy

## Supported Version

Security fixes are applied to the latest released version.

## Reporting a Vulnerability

Do not publish credentials, Jenkins output, or vulnerability details in a public issue.

Use GitHub private vulnerability reporting from the repository's **Security** tab. If that option
is unavailable, contact the repository owner privately through their GitHub profile before sharing
details.

Include the affected version, impact, reproduction steps, and a minimal redacted example. Never
include a real Jenkins URL, API token, Authorization header, cookie, console log secret, or
downloaded workspace/artifact content.

## Scope

Relevant reports include credential exposure, permission-gate bypasses, unsafe URL/path handling,
unsafe archive extraction, unbounded responses, unintended Jenkins writes, and local file writes
outside configured download directories.
