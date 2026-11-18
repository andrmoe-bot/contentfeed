# Agent workflow for this repo

This checkout runs on a VM whose GitHub credentials belong to the `andrmoe-bot` account.
`origin` (`github.com/andrmoe/contentfeed`) is read-only for that account; `bot-fork`
(`github.com/andrmoe-bot/contentfeed`) is where agent branches are pushed. Changes reach
upstream only through pull requests from the fork, such as "Merge pull request #69 from
andrmoe-bot/feat/…" in the history.

## Standard workflow

1. Branch off `main`: `git switch -c feat/<name>` (or `fix/<name>`).
2. Run the tests before pushing: `python3 -m unittest` and `python3 -m py_compile *.py`.
   Also increment the final number in `VERSION` for each change — the page footer displays it.
3. Commit, then push the branch to the fork (this VM's credentials are used automatically;
   `gh` is authenticated as andrmoe-bot and git uses it as the HTTPS credential helper):

       git push bot-fork <branch>

4. Open the pull request into upstream:

       gh pr create --repo andrmoe/contentfeed --head andrmoe-bot:<branch> --base main \
         --title "<short title>" --body "<what and why>"

5. Verify with `gh pr view <pr-url>`.

Never push to `origin` directly and never force-push; the bot account cannot write to
upstream, and PRs are reviewed there.
