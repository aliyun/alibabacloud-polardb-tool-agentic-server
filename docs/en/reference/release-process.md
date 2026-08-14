# Release process

[简体中文](../../zh-cn/reference/release-process.md)

The semantic version and GitHub release maturity are separate decisions. A
release can be published as a stable Release when its supported deployment,
upgrade, rollback, and known-issue gates have passed, including while the
project remains on the `v0.0.x` line. Use a Pre-release only when the exact
version is intentionally offered for evaluation and is not yet accepted as a
stable release.

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
fix: harden dedicated pool networking and endpoint selection

Release-Version: vMAJOR.MINOR.PATCH
Source-Develop: 0123456789abcdef0123456789abcdef01234567
```

The required trailers record the semantic release version and exact internal
source commit without reducing the subject to `publish v0.0.x` or
`port develop`.

Internal `develop` and public `main` do not share a reliable commit ancestry.
To prepare the next snapshot, export the allowlisted public tree from the exact
target `develop` commit and compare that exported tree with the current public
`main` tree. Apply only that net tree delta as the new public commit. Do not
derive the publication range from `Source-Develop..develop`, and do not
cherry-pick an internal commit range: earlier feature content may already be in
public `main` under different commit identities. Set the new `Source-Develop`
trailer to the exact internal commit whose exported tree was reviewed.

## Draft inspection

The protected workflow produces immutable multi-architecture image and Chart
versions, per-architecture offline archives, an SPDX SBOM, checksums, and
GitHub attestations. It then creates a **Draft** GitHub Release with the
Pre-release marker initially enabled. This initial marker is a safe review
default, not the final maturity decision. The workflow never publishes the
Release automatically.

Before publication, the approving maintainer must inspect:

- CI, migration, image, Helm, public-export, secret, and license gates.
- The image manifest digest and the AMD64/ARM64 platform digests.
- Chart version/digest and anonymous access to both GHCR packages.
- Asset names, checksums, attestations, and SBOM vulnerabilities.
- Generated release notes, known issues, upgrade limits, and China-network
  offline instructions.

## Publication decision

After the Draft passes inspection, publish it directly in one of these two
states. Do not publish it as a Pre-release first and then convert it merely as
an intermediate step.

For a stable release, clear the Pre-release marker and explicitly select it as
GitHub Latest:

```bash
gh release edit "${RELEASE_TAG}" \
  --draft=false \
  --prerelease=false \
  --latest \
  --verify-tag
```

For an evaluation release, retain the Pre-release marker and do not mark it as
GitHub Latest:

```bash
gh release edit "${RELEASE_TAG}" \
  --draft=false \
  --prerelease \
  --verify-tag
```

After publication, verify the Release's `draft` and `prerelease` fields, its
stable asset URLs and checksums, the tag commit, and every workflow triggered
by the `published` event. For a stable release, also verify that
`/releases/latest` selects the expected tag.

Document accepted vulnerability exceptions with scope, rationale, owner, and
expiry in the public
[`dependency-vulnerability-exceptions.yaml`](../../../security/dependency-vulnerability-exceptions.yaml)
registry. Expired exceptions fail the dependency security policy test and must
be removed, renewed after review, or replaced by a dependency fix. Do not hide
or silently waive a scanner finding.

## Container `latest` alias

Publishing a GitHub Release currently promotes its verified container image
digest to the mutable `latest` alias when the candidate is the highest
published semantic version. This `published`-event workflow also runs for a
Pre-release, so an evaluation release can update the container alias even
though it is not GitHub Latest. A delayed older Release cannot move the alias
backward. The alias applies only to the container image; it does not create or
replace a Chart version.

Use `latest` only for evaluation and discovery. Production and reproducible
deployments must continue to pin an exact semantic version or, preferably,
the verified image digest.

## Immutability

Never replace a published tag, image, Chart, archive, checksum, or Release
asset. If a defect is found, create a new patch version. A rerun fails when a
Release for the tag already exists. Do not change immutable artifacts when
changing only the GitHub Release maturity metadata.

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
through the `release` Environment and creates a **Draft** with the Pre-release
marker initially enabled for manual inspection. It does not publish the draft;
use the same publication decision above after recovery checks pass.
