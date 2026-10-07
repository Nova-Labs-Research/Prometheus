# Contributing to prometheus-lab

Research only; not for production or safety-critical use. Approval and execution
are simulated, and the conceptual sandbox is not a security boundary. Preserve
failures and uncertainty. Builder != Reviewer != Authority.

## Local development

```powershell
git clone https://github.com/Nova-Labs-Research/Prometheus.git
cd Prometheus
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
pytest
prom-lab run-dummy-loop
prom-lab audit summary
```

Python 3.10+ is required. On Linux/macOS activate with
`source .venv/bin/activate`. On Windows, if activation is unavailable, use
`.\.venv\Scripts\python.exe -m pip`, `.\.venv\Scripts\python.exe -m pytest`,
and `.\.venv\Scripts\prom-lab.exe` directly. Reinstall the editable package after
changing dependencies or its version.

To reproduce the CI smoke test on Linux/macOS:

```sh
export SAFE_LAB_BASE="$(mktemp -d)"
(
  cd "$SAFE_LAB_BASE"
  prom-lab run-dummy-loop
  prom-lab audit summary
  test -s artifacts/audit/events.jsonl
)
```

`SAFE_LAB_BASE` names the smoke test's working directory. The application does
not consume this environment variable: changing directories places the default
audit output there. This is output isolation, not a security sandbox.

## Branches and reviews

`main` is the protected integration branch. Create feature branches from an
up-to-date `main`, and submit pull requests targeting `main`. All four
`test (Python X.Y)` checks must pass before merging, and branches must be current
with `main`. GitHub does not allow authors to approve their own PRs. With one
maintainer, the rule requires zero approving reviews. Before merging, inspect the
diff and CI, record the decision and unresolved limits in the PR, and stop on
findings that need more work. This is self-review, not independent human review.

Include the problem, resulting behavior, tests run, and any known limitations in
the PR description. Keep changes scoped. Do not commit local audit logs,
virtual environments, caches, build products, or credentials. Do not turn
simulated approval into a claim of real human review.

CI runs `pytest` and a CLI smoke test on Ubuntu with Python 3.10, 3.11, 3.12,
and 3.13. A test or command failure fails that matrix job. pip downloads are
cached using `pyproject.toml` as the dependency key. Actions use pinned commit
SHAs; update the SHA and version comment together after reviewing an update.

## Versions and releases

Use Semantic Versioning (`MAJOR.MINOR.PATCH`). `pyproject.toml` is the only
version source; `prometheus_lab.__version__` reads installed package metadata.
PATCH is for compatible fixes; MINOR is for compatible additions; MAJOR is for
breaking stable interfaces. During `0.x` research development, use MINOR bumps
for breaking changes and describe them explicitly. `0.x` is not a stability or
safety guarantee.

1. Change `[project].version` in `pyproject.toml` in a reviewed PR.
2. Reinstall with `python -m pip install -e ".[dev]"` and run `pytest` and
   `python -m build`.
3. Merge after review and green CI, then fetch and fast-forward local `main`.
4. Tag the approved release commit with exactly `vX.Y.Z` and push that tag.
   Never move or reuse a published version tag.

For the first version, after approval of the release commit:

```sh
git switch main
git pull --ff-only origin main
git tag -a v0.1.0 -m "prometheus-lab 0.1.0"
git push origin v0.1.0
```

The tag workflow verifies the tag against `pyproject.toml`, builds the source
distribution and wheel, and retains them as Actions artifacts for 14 days.
It does not publish to PyPI or create a GitHub Release. Download/review the
artifacts and publish release notes separately if needed.

## GitHub branch protection

In Settings -> Branches, use the classic branch protection rule for `main`.
Require a pull request, status checks and an up-to-date branch. Set required
approving reviews to **zero** while only one maintainer can review. Keep stale
approval dismissal enabled for any voluntary reviews. Select these check names:

- `test (Python 3.10)`
- `test (Python 3.11)`
- `test (Python 3.12)`
- `test (Python 3.13)`

Apply the rule to administrators too; keep force pushes and branch deletion
disabled. If an independent reviewer with write access becomes available, a
separate governance decision can raise the required approval count to one.
Protection is a GitHub setting, not enforced by these Markdown files. A solo PR
decision does not grant approval for real execution, VM operations, or research
promotion; those remain separate human authority decisions.
See [GitHub's branch protection instructions](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/managing-a-branch-protection-rule).
