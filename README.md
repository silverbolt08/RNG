# RNG

Hackathon project.

## Getting started

```bash
git clone https://github.com/silverbolt08/RNG.git
cd RNG
```

## Team workflow: feature branches + pull requests

Nobody commits directly to `main`. Each person works on their own branch and
opens a pull request to merge it.

### 1. Start new work (always from an up-to-date `main`)

```bash
git checkout main
git pull                            # get everyone else's merged work
git checkout -b yourname/what-it-does
```

### 2. Commit as you go

```bash
git add .
git commit -m "Add the thing"
```

### 3. Push your branch

```bash
git push -u origin yourname/what-it-does    # first push of the branch
git push                                    # every push after that
```

### 4. Open a pull request

Go to the repo on GitHub — it will show a banner for your freshly pushed
branch with a **Compare & pull request** button. Open the PR, have a teammate
glance at it, then **Merge pull request**.

### 5. Clean up and go again

```bash
git checkout main
git pull
git branch -d yourname/what-it-does
```

## Getting other people's changes

A clone is a snapshot, not a live folder — new work from teammates only
arrives when you ask for it:

```bash
git pull                 # on main: fetch + merge the latest
git fetch --all          # see what's on the remote without changing your files
git log --oneline -10    # what landed recently
```

If you are mid-branch and want the latest `main` underneath you:

```bash
git checkout main && git pull
git checkout yourname/your-branch
git merge main
```

## Rules of thumb

- `git pull` before you start anything. Most conflicts come from stale `main`.
- Small, frequent PRs beat one giant one.
- Never `git push --force` to `main`.
- Don't commit secrets, `.env` files, or build output (see `.gitignore`).
