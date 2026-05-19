FROM node:20-slim

WORKDIR /app

COPY package*.json ./
RUN npm install --production

COPY . .

RUN mkdir -p baileys_sessions

EXPOSE 3002

CMD ["node", "index.js"]
