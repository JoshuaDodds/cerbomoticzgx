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
cd gitops-managed-repos && git clone "$GIT_REPO" > /dev/null 2>&1
cd "$GIT_PROJECT" || false

# git setup
git config pull.rebase true
git config --global user.email "sgc@hs.mfis.net"
git config --global user.name "SGC Bot"

# Enter work loop
echo -e "$SVC_NAME: Entering work loop...";

while true; do
  echo -e "$SVC_NAME: Updating remote git repo & comparing with current state...";
  git remote update >> /dev/null

  UPSTREAM=${1:-'@{u}'}
  LOCAL=$(git rev-parse @)
  REMOTE=$(git rev-parse "$UPSTREAM")
  BASE=$(git merge-base @ "$UPSTREAM")

  if [ "$LOCAL" = "$REMOTE" ]; then
    # echo "$SVC_NAME: Up-to-date - No remote changes."
    true

  elif [ "$LOCAL" = "$BASE" ]; then
    echo "$SVC_NAME: Remote has changes - Storing changed files..."
    CHANGED_FILES+=" $(git diff --pretty=format: --name-only origin/main main | grep '.yaml' | grep -vE 'vendor|helm|src|-disabled')"
    echo "$SVC_NAME: Pulling changes..."
    git pull >> /dev/null

    echo "$SVC_NAME: Killing any running python3 processes...Sleeping for $INTERVAL"
    pkill -f python3
    echo "$SVC_NAME: Updating cerbomoticzGx service..."
    rsync -qav --exclude 'log.txt' --exclude '__pycache__' /app/gitops-managed-repos/cerbomoticzgx/ /app/

  # currently, caught but unhandled exceptions (will not cause the script to exit)
  elif [ "$REMOTE" = "$BASE" ]; then
    echo "$SVC_NAME: WARNING! Local has changes - This should not happen! Should probably toss the local copy and re-clone."

  else
    echo "$SVC_NAME: Warning: Remote and Local have diverged. This should not happen! Should probably toss the local copy and re-clone."
  fi

  # sleep
  # echo -e "$SVC_NAME: Sleeping for $INTERVAL"
  sleep "$INTERVAL"
done
