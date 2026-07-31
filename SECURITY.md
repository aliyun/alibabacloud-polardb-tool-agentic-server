# Security Policy

## Supported versions

`v0.0.x` is a pre-release line intended for early adopters. Until `v0.1.0` is
released, only the latest published `v0.0.x` patch receives security fixes.
After a new patch is published, earlier `v0.0.x` releases are unsupported.

## Private vulnerability reporting

Use [GitHub private vulnerability reporting](https://github.com/aliyun/alibabacloud-polardb-tool-agentic-server/security/advisories/new)
to report a suspected vulnerability. Do not open a public issue for a security
report.

Include the affected version, impact, prerequisites, and minimal reproduction
steps. Remove passwords, tokens, access keys, database connection strings,
customer data, and production endpoints. If a value is needed to explain the
problem, replace it with a clearly marked placeholder.

The maintainers will acknowledge the report, investigate it, and coordinate
disclosure and a fixed release when appropriate. Response times vary with
severity and reproducibility; this pre-release project does not promise a
service-level agreement.

## Accepted dependency vulnerability exceptions

Time-bounded dependency vulnerability exceptions are recorded in
[`security/dependency-vulnerability-exceptions.yaml`](security/dependency-vulnerability-exceptions.yaml).
Each exception identifies the affected scope, rationale, mitigation, owner,
acceptance date, and expiry date. An exception must be removed, renewed after
review, or replaced by a dependency fix before it expires.

## Public issues

Use public issues only for non-sensitive bugs and feature requests. Before
submitting logs or screenshots, redact secrets, passwords, tokens, cookies,
authorization headers, connection strings, SQL parameters, and customer data.
