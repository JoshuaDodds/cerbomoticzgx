#!/usr/bin/env bash

SVC_NAME="simple-gitops-controller"
INTERVAL="60s"

# source secrets
source /app/.env-gitops

# ssh setup for access to Github repo
mkdir ~/.ssh && chmod og-rwx ~/.ssh
echo "$id_rsa" > ~/.ssh/id_rsa && chmod og-rwx ~/.ssh/id_rsa
echo "$id_rsa_pub" > ~/.ssh/id_rsa.pub

# add github keys to known_hosts to avoid interactive prompt on clone action
ssh-keyscan github.com 2>/dev/null >> ~/.ssh/known_hosts

# Initial clone of the repo which describes the cluster state
cd /app && mkdir gitops-managed-repos > /dev/null 2>&1
cd gitops-managed-repos

# Check if GIT_BRANCH is set, default to 'main' if not
GIT_BRANCH=${GIT_BRANCH:-main}
echo "$SVC_NAME: Using branch '$GIT_BRANCH' for sync operations."

# Clone the repository and check out the desired branch
if [ ! -d "$GIT_PROJECT" ]; then
  git clone "$GIT_REPO" > /dev/null 2>&1
  cd "$GIT_PROJECT" || { echo "$SVC_NAME: Failed to navigate to $GIT_PROJECT. Retrying in the next interval."; false; }
  git checkout "$GIT_BRANCH" || { echo "$SVC_NAME: Branch '$GIT_BRANCH' not found."; false; }
else
  cd "$GIT_PROJECT" || { echo "$SVC_NAME: Failed to navigate to $GIT_PROJECT."; false; }
  git fetch origin
  git checkout "$GIT_BRANCH" || { echo "$SVC_NAME: Branch '$GIT_BRANCH' not found."; false; }
fi

# git setup
git config pull.rebase true
git config --global user.email "sgc@hs.mfis.net"
git config --global user.name "SGC Bot"

# Check if update from git is needed and then enter work loop
echo -e "$SVC_NAME: Checking if container runtime needs an update..."
if diff -rq /app/ /app/gitops-managed-repos/cerbomoticzgx/ | grep -vE 'pycache|env|json|token|png|db|Only'; then
  echo -e "$SVC_NAME: Updating container runtime..."
  pkill -f python3
  rsync -qav --exclude 'log.txt' --exclude '__pycache__' /app/gitops-managed-repos/cerbomoticzgx/ /app/
else
  echo -e "$SVC_NAME: Container runtime is up to date."
fi

# Main work loop
echo -e "$SVC_NAME: Entering work loop...";
while true; do
  # echo -e "$SVC_NAME: Updating remote git repo & comparing with current state...";
  git remote update >> /dev/null

  UPSTREAM="origin/$GIT_BRANCH"
  LOCAL=$(git rev-parse @)
  REMOTE=$(git rev-parse "$UPSTREAM")
  BASE=$(git merge-base @ "$UPSTREAM")

  # no changes
  if [ "$LOCAL" = "$REMOTE" ]; then
    # echo "$SVC_NAME: Up-to-date - No remote changes."
    true

  # changes detected
  elif [ "$LOCAL" = "$BASE" ]; then
    echo "$SVC_NAME: Remote has changes - Pulling changes from branch '$GIT_BRANCH'..."
    git pull >> /dev/null
    echo "$SVC_NAME: Successfully pulled from branch '$GIT_BRANCH'."

    echo "$SVC_NAME: Killing any running python3 processes..."
    pkill -f python3

    echo "$SVC_NAME: Updating cerbomoticzGx service runtime..."
    rsync -qav --exclude 'log.txt' --exclude '__pycache__' /app/gitops-managed-repos/cerbomoticzgx/ /app/
    echo "$SVC_NAME: Successfully synchronized runtime with branch '$GIT_BRANCH'."


  # currently, caught but unhandled exceptions (will not cause the script to exit)
  elif [ "$REMOTE" = "$BASE" ]; then
    echo "$SVC_NAME: WARNING! Local changes have diverged from '$GIT_BRANCH'. Re-cloning the repository..."
    cd /app/gitops-managed-repos
    rm -rf "$GIT_PROJECT"
    git clone "$GIT_REPO" && cd "$GIT_PROJECT" && git checkout "$GIT_BRANCH"
  else
    echo "$SVC_NAME: WARNING! Local and remote have diverged. Re-cloning is recommended."
  fi

  # go back to sleep
  # echo -e "$SVC_NAME: Sleeping for $INTERVAL"
  sleep "$INTERVAL"
done
