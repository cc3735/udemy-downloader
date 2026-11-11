# Codex Portfolio Review — Udemy Downloader

## Review identity

- Repository: `cc3735/udemy-downloader`
- Canonical root: `C:\Users\023du\Documents\Repos\udemy-downloader`
- Reviewed: 2026-07-22 19:36 America/New_York
- Branch / commit: `master` / `5212e1b`
- Working tree at review start: clean
- Scope: CLI packaging, credential hygiene, authorized-use boundaries, test/CI evidence, and documentation. This review does not recommend DRM circumvention or third-party terms violations.

## Verdict

The project is a legacy personal-use CLI with extensive setup material, but it lacks the safety and maintenance signals required for any responsible release: no automated tests/CI, unpinned dependencies, and source/configuration paths that handle bearer tokens, cookies, and key material. Its first improvement should be an explicit lawful-authorized-use and credential-safety contract, followed by non-networked regression coverage for harmless CLI behavior.

## Findings and high-value changes

1. **[High | low effort] Establish a credential-hygiene contract and audit literal application credentials.** The codebase includes source-level client-credential constants and accepts bearer tokens/cookies/key material. Verify with the service owner whether each literal is intentionally public; otherwise rotate and externalize it. Ensure key files and runtime credentials are ignored, excluded from diagnostic output, redacted from errors, and scanned before commit. Do not solicit, log, or document ways to obtain restricted keys.
2. **[High | low effort] Make the authorized-use boundary enforceable in UX and docs.** The README acknowledges third-party terms risk, but the CLI should require a clear acknowledgment and limit support/documentation to content the user is authorized to access. Document data retention/removal and a safe credential-revocation path. Remove or quarantine instructions that normalize extracting sensitive browser/network material.
3. **[Medium | medium effort] Add a harmless offline test/CI baseline.** There are no tests or workflow. Add tests for argument parsing, configuration precedence, secret redaction, output-path validation, resume/state behavior, and dependency/tool preflight using mocked clients only. Add lint/type/test jobs and pin or lock dependencies for repeatable environments.
4. **[Medium | low effort] Add a support matrix and deprecation plan.** Clearly state supported OS/Python/tool versions, maintenance status, known legal/compatibility constraints, and what failures are safely recoverable. This is more honest and useful than implying broad production support for an unverified legacy tool.

## Suggested implementation order

1. Complete the credential/public-client audit and authorized-use UX/docs.
2. Add offline safety/configuration tests and CI.
3. Lock dependencies and publish support/deprecation status.

## Validation for a follow-up implementation

- Confirm no real token, cookie, key, or sensitive client credential appears in tracked source, logs, fixtures, or CI output unless explicitly verified public.
- Run the full test suite offline; it must never invoke a course or license service.
- Verify a user can remove local credentials/state and receive an actionable safe setup message.

## Claude handoff

Please validate whether to implement the above in `cc3735/udemy-downloader` at `C:\Users\023du\Documents\Repos\udemy-downloader`. Keep all implementation and validation limited to legitimate, authorized access and standard security/reliability work. Do not add or improve DRM bypass, key acquisition, or third-party content access behavior.

No application source, tests, configuration, or docs were changed by this review.
