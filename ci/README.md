# Enabling the self-check workflow

`selfcheck.yml` is kept here instead of `.github/workflows/` because pushing a
workflow file requires a token with the **`workflow`** scope. With a plain
`repo`-scoped `gh` token, GitHub rejects the push with:

```
refusing to allow an OAuth App to create or update workflow `.github/workflows/...` without `workflow` scope
```

Two ways to turn it on:

1. Grant the scope once, then move the file into place and push:

   ```bash
   gh auth refresh -s workflow
   mkdir -p .github/workflows && git mv ci/selfcheck.yml .github/workflows/selfcheck.yml
   git commit -m "ci: enable selfcheck workflow" && git push
   ```

2. Or paste the file contents into
   `https://github.com/<owner>/<repo>/new/main/.github/workflows` in the web UI
   (the web UI is not affected by the token scope restriction).

Either way the job is a single step: `python scripts/selfcheck.py`, which is the
same check you can run locally at any time.
