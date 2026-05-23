# Publishing claude-tier-maximizer to GitHub

This file documents how to push the project to GitHub the first time.

Two paths. Pick one.

---

## Path A — gh CLI (recommended)

### Step 1 — log into gh (interactive, one-time)

```bash
gh auth login
```

Walk-through of the prompts:

1. **What account?** → `GitHub.com`
2. **Protocol?** → `HTTPS`
3. **Authenticate Git with your GitHub credentials?** → `Yes`
4. **How would you like to authenticate?** → `Login with a web browser`
5. `gh` prints a one-time code. **Copy it.**
6. Open the URL it gives you in any browser, paste the code, approve.
7. Back in the terminal, `gh` confirms login.

Verify:

```bash
gh auth status
# expect: ✓ Logged in to github.com account <your-username>
```

### Step 2 — initial commit + create + push

```bash
cd /opt/claude-tier-maximizer
git init -b main
git add -A
git commit -m "initial: claude-tier-maximizer v0.1"
gh repo create <your-org>/claude-tier-maximizer --public --source=. --push
```

That one `gh repo create` does three things: creates the repo on GitHub, adds
it as remote `origin`, pushes `main`. Done.

### Step 3 — verify

```bash
gh browse              # opens the new repo in your default browser
# or:
gh repo view <your-org>/claude-tier-maximizer
```

The GitHub Actions `lint` workflow will run automatically on the first push.
Check status:

```bash
gh run list --workflow=lint.yml
```

---

## Path B — manual via browser + Personal Access Token

Use this if you don't want to do `gh auth login` for any reason.

### Step 1 — create the repo on GitHub

1. Open https://github.com/new
2. Settings:
   - Owner: `<your-org>`
   - Repository name: `claude-tier-maximizer`
   - Public
   - **Do NOT** check "Add a README file"
   - **Do NOT** add `.gitignore` or LICENSE (we already have ours)
3. Click **Create repository**

GitHub shows you a setup page with URLs. Copy the HTTPS URL.

### Step 2 — generate a Personal Access Token (PAT)

GitHub stopped accepting passwords for git operations. You need a token.

Recommended: **Fine-grained token** scoped to just this repo.

1. Open https://github.com/settings/personal-access-tokens/new
2. Token name: e.g. `claude-tier-maximizer-push`
3. Expiration: pick something reasonable (90 days, 1 year)
4. Repository access: **Only select repositories** → choose `claude-tier-maximizer`
5. Permissions → **Repository permissions**:
   - `Contents`: **Read and write**
   - (everything else can stay "No access")
6. Click **Generate token**, **copy it now** — you won't see it again.

### Step 3 — push

```bash
cd /opt/claude-tier-maximizer

git init -b main
git add -A
git commit -m "initial: claude-tier-maximizer v0.1"
git remote add origin https://github.com/<your-org>/claude-tier-maximizer.git

# optional: save credentials so you don't paste the token on every push
git config --global credential.helper store

git push -u origin main
# Username: <your-username>
# Password: paste the PAT you generated
```

After the first successful push the PAT is cached in `~/.git-credentials`.

---

## After publishing

### Add a description and topics (one-time)

```bash
gh repo edit <your-org>/claude-tier-maximizer \
  --description "Stretch Claude Pro/Max by routing thinking-budget per prompt" \
  --add-topic claude-code \
  --add-topic anthropic \
  --add-topic proxy \
  --add-topic developer-tools \
  --homepage ""
```

(Or do it via the repo's "About" gear icon on github.com.)

### Future updates

```bash
cd /opt/claude-tier-maximizer
# edit files
git add -A && git commit -m "<message>"
git push
```

### Tag releases

```bash
git tag -a v0.1 -m "first cut: regex routing + usage capture + tune + personalize"
git push --tags
gh release create v0.1 --notes "Initial release."
```

---

## Troubleshooting

**`gh: command not found`** — install: `apt install -y gh` on Ubuntu/Debian.

**`Permission denied (publickey)`** — you're trying SSH; switch the remote to
HTTPS: `git remote set-url origin https://github.com/...`.

**`Authentication failed`** with PAT — token expired or scope is wrong. Make a
new one with `Contents: Read+Write` and `Only select repositories: this repo`.

**Workflow fails on PR** — `lint.yml` validates YAML and regex; check the
GitHub Actions log. Most common: a regex with an unescaped backslash in YAML
single-quoted strings. Either escape it or switch to double quotes.

**`git config --global credential.helper store`** stores the token in plain
text at `~/.git-credentials`. On a personal server that's fine; on shared
machines, prefer `credential.helper cache --timeout=3600` instead.
