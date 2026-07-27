# Release process

[简体中文](../../zh-cn/reference/release-process.md)

`v0.0.x` releases are pre-releases for user trials. Promote the project to
`v0.1.0` only after field feedback and defect fixes make the supported
deployment stable.

## Repository protection

Protect public `main`, require CI, require review, and disallow force pushes
and tag rewrites. Configure the `release` GitHub Environment with required
maintainer approval. The release workflow accepts only a `vMAJOR.MINOR.PATCH`
tag whose commit is reachable from public `main`, and the tag must match
Python, Web, lockfile, Chart, and app versions.

GHCR creates new packages as private in some repository configurations. After
the first controlled package push, a repository owner must make both the image
package and OCI Chart package public and confirm repository linkage. The
workflow deliberately logs out and performs anonymous image and Chart reads;
it stops before creating a Release if either package remains private.

## Draft inspection

The protected workflow produces immutable multi-architecture image and Chart
versions, per-architecture offline archives, an SPDX SBOM, checksums, and
GitHub attestations. It then creates a **Draft, Pre-release** GitHub Release.
It never publishes the Release automatically.

Before publication, the approving maintainer must inspect:

- CI, migration, image, Helm, public-export, secret, and license gates.
- The image manifest digest and the AMD64/ARM64 platform digests.
- Chart version/digest and anonymous access to both GHCR packages.
- Asset names, checksums, attestations, and SBOM vulnerabilities.
- Generated release notes, known issues, upgrade limits, and China-network
  offline instructions.

Document accepted vulnerability exceptions with scope, rationale, owner, and
expiry. Do not hide or silently waive a scanner finding.

## Immutability

Never replace a published tag, image, Chart, archive, checksum, or Release
asset. If a defect is found, create a new patch version. A rerun fails when a
Release for the tag already exists. Keep `prerelease` enabled throughout the
`v0.0.x` line.
