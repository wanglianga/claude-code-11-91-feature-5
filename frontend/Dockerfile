FROM node:22-slim

WORKDIR /build
COPY package.json package-lock.json* ./
RUN npm config set registry https://registry.npmmirror.com && npm install

COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=0 /build/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
