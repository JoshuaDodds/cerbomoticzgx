#!/usr/bin/env bash

# build arm64 (raspPi) custom docker images and push to container registry
# Note: this needs to run on a machine with docker desktop installed and able to build multiplatform images

# source some env vars that we dont want to put under version control
source .env || exit 1

# tell docker to switch to QEMU when reading other executable binaries
docker run --rm --privileged multiarch/qemu-user-static --reset -p yes

# Docker Login
echo "$CR_PAT" | docker login ghcr.io -u USERNAME --password-stdin

# build and tag local amd64 version
# docker rmi ghcr.io/joshuadodds/cerbomoticzgx:latest-amd64
# docker buildx build --platform linux/amd64 -t ghcr.io/joshuadodds/cerbomoticzgx:"$VERSION"-amd64 -t ghcr.io/joshuadodds/cerbomoticzgx:latest-amd64 -f cerbomoticzGx.Dockerfile .

# rm older image
docker rmi ghcr.io/joshuadodds/cerbomoticzgx:latest

# build, tag, and push to CR
docker buildx build --platform linux/arm64 --cache-from ghcr.io/joshuadodds/cerbomoticzgx:buildcache \
  --cache-to ghcr.io/joshuadodds/cerbomoticzgx:buildcache \
  -t ghcr.io/joshuadodds/cerbomoticzgx:"$VERSION" -t ghcr.io/joshuadodds/cerbomoticzgx:latest \
  -f cerbomoticzGx.Dockerfile --push .

# cleanup older local untagged images
docker image prune -f
