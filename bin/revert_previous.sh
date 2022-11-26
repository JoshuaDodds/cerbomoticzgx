#!/usr/bin/env bash

git checkout main~1         # check out the revision that you want to reset to
git checkout -b hotfix      # create a branch named hotfix to do the work
git merge -s ours main      # merge main's history without changing any files
git checkout main           # switch back to main
git merge hotfix            # and merge in the hotfix branch
git push                    # done, no need to force push!
git branch -D hotfix        # cleanup hotfix branch
