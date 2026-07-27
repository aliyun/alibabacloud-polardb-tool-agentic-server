# Contributing to Alibaba Cloud PolarDB Tool Agentic Server

Thank you for helping improve the project. This guide covers source changes,
documentation, and translations.

## Before you start

- Search existing issues and pull requests before starting duplicate work.
- Keep each pull request focused on one problem.
- Never commit access keys, passwords, tokens, database connection strings, or
  production endpoint details.
- Discuss large behavior or protocol changes before investing in a full
  implementation.

## Development setup

```bash
uv sync --extra dev

export PAS_SERVER_DEV_MODE=true
export PAS_DATABASE_URL='sqlite+aiosqlite:///data/polardb_agentic.db'
export PAS_ENCRYPTION_KEY="$(
  python3 -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())'
)"

uv run pas database migrate
```

Start the backend with:

```bash
uv run python -m server
```

For web-console changes:

```bash
cd web
npm install
npm run dev
```

## Pull request checks

Run the checks relevant to your change. Before requesting review, the complete
open-source verification set is:

```bash
uv run --extra dev ruff check .
uv run --extra dev pytest

cd web
npm ci
npm test -- --run
npm run lint
npm run build
```

Also run `git diff --check` and inspect the staged diff for credentials,
internal URLs, customer identifiers, and generated artifacts.

When dependency lockfiles change, regenerate and verify the reviewed license
inventory:

```bash
python scripts/security/generate-license-report.py
python scripts/security/generate-license-report.py --check
```

An unknown or unapproved license must be reviewed explicitly; do not weaken
the check or substitute a guessed SPDX identifier. Report suspected
vulnerabilities through the private process in [SECURITY.md](SECURITY.md), not
through a public issue.

Before a public release, verify the reviewed allowlist export and its isolated
single-root-commit rehearsal:

```bash
scripts/public-release/export.sh --check
scripts/public-release/rehearse.sh
```

These commands do not modify repository refs or remotes. Files that belong only
to internal design, local development state, customer work, or generated
output must never be added to `.public-release-allowlist`.

The public CI definition in `.github/workflows/ci.yml` repeats these gates and
also tests the backend, Web console, all supported metadata-database engines,
the production image, Compose files, and Helm Chart. Migration-only checks can
be run locally with:

```bash
scripts/ci/check-alembic-head.sh
scripts/ci/test-migrations.sh sqlite
```

Changes to public behavior must update the canonical English guide and its
same-relative-path Simplified Chinese peer in the same pull request. Run the
public documentation graph test so every guide remains linked from its locale
index and every relative link resolves.

Performance tests require an explicitly configured VPC deployment. Do not
present mocked or SQLite results as performance acceptance evidence.

## Documentation structure

The repository uses English as the canonical documentation language:

```text
README.md
README_<locale>.md
docs/
├── en/
│   ├── README.md
│   └── <functional-module>/
│       └── <topic>.md
└── <locale>/
    ├── README.md
    └── <functional-module>/
        └── <topic>.md
```

Current locale names follow lowercase BCP 47-style tags for documentation
directories, such as `zh-cn`. Root README translations use a readable locale
suffix, such as `README_zh-CN.md`.

Group public guides by stable product capability, such as `setup`,
`configuration`, or `database-instances`. Add a new functional-module
directory only when the topic does not belong to an existing capability.
When moving a page, update root READMEs, locale indexes, language-switch links,
cross-guide links, tests, and all repository references in the same change.

Public `docs/` content must help users install, configure, operate, integrate,
or troubleshoot the released software. Internal review notes, implementation
plans, customer-specific proposals, credentials, private links, and unshipped
design alternatives must not be added to the public documentation tree.

## Translation workflow

When translating an existing page:

1. Start from the latest English file.
2. Create the same relative path under the target locale directory.
3. Preserve headings, code blocks, configuration names, API names, and safety
   warnings.
4. Add language-switch links near the top of both pages.
5. Use relative links that work from the translated file's location.
6. Record the English source commit in the pull request description.
7. Ask a fluent reviewer to check technical accuracy and natural language.

Do not mix multiple full-language versions in one Markdown file. Product names,
source identifiers, environment variables, API fields, status values, and code
remain unchanged unless the software itself localizes them.

When the English source changes, update affected translations in the same pull
request. If exceptional circumstances prevent that, state that the translation
may lag behind English in the pull request and open a follow-up issue before
merging.

## Adding a language

A pull request adding a language should include:

- A translated root README when the language has project-wide coverage.
- A `docs/<locale>/README.md` documentation index.
- Translations for every page linked from that locale's index.
- Language-switch links from the English and translated pages.
- A fluent-language review.

Machine translation may be used as a draft, but the pull request must be
reviewed for technical accuracy, terminology, links, and formatting before it
is merged.

## Commit and pull request style

- Write commit messages in English.
- Explain user-visible behavior and verification evidence in the pull request.
- Keep generated files and unrelated formatting out of focused changes.
- Update tests and documentation together when behavior changes.
- Do not claim real PolarDB or performance validation unless it was actually
  run against the stated environment.
