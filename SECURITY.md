# Security Policy

## Supported versions

Palmimo DevKit is **pre-release** software (`0.1.x`). Security fixes are applied
to the `main` branch only; there are no maintained release branches yet.

| Version | Supported |
|---------|-----------|
| `main` (latest) | ✅ |
| Tagged pre-releases | ❌ (upgrade to `main`) |

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately through GitHub's **[Private Vulnerability Reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)**:
open the repository's **Security** tab and choose **“Report a vulnerability.”**

If you cannot use that channel, reach the maintainers through the contact form at
<https://palmimo.dev/en/contact>. That form is a general inquiry channel, so ask
there for a private channel instead of attaching exploit details to it.

Please include:

- a description of the issue and its impact,
- the affected component (`palmimo_sdk`, an app, a script, …) and version/commit,
- steps to reproduce or a proof of concept, and
- any suggested remediation.

## What to expect

This is a pre-release project maintained by a small team, so response times are best-effort:

- **Acknowledgement** that the report arrived. We cannot commit to a fixed turnaround yet, but no report will go unanswered.
- An initial assessment and a plan (fix, mitigation, or "won't fix" with a rationale) once the report is triaged.
- **Coordinated disclosure**: we will agree on a disclosure timeline with you and credit you in the advisory unless you prefer to remain anonymous.

## Scope

This kit drives real, geared servos and runs on mains-derived power. Reports
that affect **physical safety** (for example, motion that bypasses the safe
servo range or the neutral-transition contract) are in scope and treated with
the same priority as software vulnerabilities.
