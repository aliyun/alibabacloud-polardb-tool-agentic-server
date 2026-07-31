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

## Public snapshot commits

Apply a verified `develop` snapshot to public `main` as a new incremental
commit; never amend or replace existing public history. Its subject must use
an allowed Conventional Commit type and describe the user-visible behavior or
fix, rather than only the version or publication action. For example:

```text
fix: harden resource pool networking and endpoint selection

Release-Version: vMAJOR.MINOR.PATCH
Source-Develop: 0123456789abcdef0123456789abcdef01234567
```

The required trailers record the semantic release version and exact internal
source commit without reducing the subject to `publish v0.0.x` or
`port develop`.

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
expiry in the public
[`dependency-vulnerability-exceptions.yaml`](../../../security/dependency-vulnerability-exceptions.yaml)
registry. Expired exceptions fail the dependency security policy test and must
be removed, renewed after review, or replaced by a dependency fix. Do not hide
or silently waive a scanner finding.

## Container `latest` alias

Publishing a GitHub Release may promote its verified container image digest
to the mutable `latest` alias. Promotion runs only when the candidate is the
highest published semantic version, so a delayed older Release cannot move
the alias backward. The alias applies only to the container image; it does
not create or replace a Chart version.

Use `latest` only for evaluation and discovery. Production and reproducible
deployments must continue to pin an exact semantic version or, preferably,
the verified image digest.

## Immutability

Never replace a published tag, image, Chart, archive, checksum, or Release
asset. If a defect is found, create a new patch version. A rerun fails when a
Release for the tag already exists. Keep `prerelease` enabled throughout the
`v0.0.x` line.

## Recovering an incomplete Release

If an immutable tag, image, and Chart exist but the GitHub Release was not
created, use the manual recovery workflow. It validates the exact tag commit,
its reachability from public `main`, all tagged source versions, image labels
and platform digests, Chart readability, and the absence of a Release. The
workflow never rebuilds or republishes the versioned image or Chart.

Set `RELEASE_TAG` and `EXPECTED_COMMIT` to the incomplete Release's exact
values, then run the read-only validation first:

```bash
RELEASE_TAG="${RELEASE_TAG:?set the existing vMAJOR.MINOR.PATCH tag}"
EXPECTED_COMMIT="${EXPECTED_COMMIT:?set the exact 40-character tag commit}"

gh workflow run recover-release.yml \
  -f tag="${RELEASE_TAG}" \
  -f expected_commit="${EXPECTED_COMMIT}" \
  -f dry_run=true
```

Review the JSON evidence in the job summary. Only then may a maintainer start
the mutating job by changing `dry_run` to `false`. That job requires approval
through the `release` Environment and creates a **Draft, Pre-release** for
manual inspection. It does not publish the draft.
