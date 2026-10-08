# Build the static bundle, then serve it from nginx. Two stages so the
# runtime image carries no node_modules and no toolchain.
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json ./
RUN npm install
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY default.conf.template /etc/nginx/templates/default.conf.template

# Declared empty on purpose. The entrypoint's envsubst step only substitutes
# variables that are actually set -- an unset one is left in the output as the
# literal string ${ORIGIN_SECRET}, which would make the origin check reject
# every request. Empty means "accept requests with no secret header", which is
# what local development wants; production overrides it.
ENV ORIGIN_SECRET=""

EXPOSE 80
