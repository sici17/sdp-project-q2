FROM node:22-alpine@sha256:16e22a550f3863206a3f701448c45f7912c6896a62de43add43bb9c86130c3e2

WORKDIR /app

COPY --chown=node:node package.json ./
COPY --chown=node:node package-lock.json ./
# The package manager is a build tool, not a runtime one, so it does not stay.
# Every vulnerability the image scanner reports for this image comes from npm's
# own bundled dependencies - tar, brace-expansion, ip-address and friends - and
# none of them are in the gateway's dependency tree, which `npm audit
# --omit=dev` reports clean. Removing npm removes the code rather than
# suppressing the finding, and leaves no package manager in a running container.
RUN npm ci --omit=dev \
    && rm -rf /usr/local/lib/node_modules/npm \
        /usr/local/lib/node_modules/corepack \
        /usr/local/bin/npm \
        /usr/local/bin/npx \
        /usr/local/bin/corepack \
        /root/.npm \
        /opt/yarn-v* \
        /usr/local/bin/yarn \
        /usr/local/bin/yarnpkg
COPY --chown=node:node src ./src
RUN mkdir -p /app/data/operations /operations \
    && chown -R node:node /app /operations

ENV PORT=8080
EXPOSE 8080

USER node

# Node directly rather than through npm, which is no longer installed. It is
# also one fewer process between the container and the signal that stops it.
CMD ["node", "src/server.js"]
