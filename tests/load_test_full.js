import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
  scenarios: {
    historical_api: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '15s', target: 50 },
        { duration: '30s', target: 100 },
        { duration: '30s', target: 100 },
        { duration: '15s', target: 0 },
      ],
      exec: 'test_historical',
    },
    news_stream: {
      executor: 'constant-vus',
      vus: 20,
      duration: '1m30s',
      exec: 'test_stream',
    },
    news_current: {
      executor: 'constant-vus',
      vus: 20,
      duration: '1m30s',
      exec: 'test_news_current',
    },
    news_upcoming: {
      executor: 'constant-vus',
      vus: 20,
      duration: '1m30s',
      exec: 'test_news_upcoming',
    },
    news_events: {
      executor: 'constant-vus',
      vus: 20,
      duration: '1m30s',
      exec: 'test_news_events',
    },
    news_markers: {
      executor: 'constant-vus',
      vus: 20,
      duration: '1m30s',
      exec: 'test_news_markers',
    },
    strategies: {
      executor: 'constant-vus',
      vus: 20,
      duration: '1m30s',
      exec: 'test_strategies',
    },
  }
};

const WEB_URL = 'http://tradingbot-api-web:8080';
const SSE_URL = 'http://tradingbot-api-sse:8081';

const symbols = ['USDJPY', 'EURUSD', 'GBPUSD'];

export function test_historical() {
  const sym = symbols[Math.floor(Math.random() * symbols.length)];
  const res = http.get(`${WEB_URL}/api/historical/${sym}/H1`);
  check(res, { 'historical 200': (r) => r.status === 200 });
  sleep(Math.random() * 0.5 + 0.1); 
}

export function test_stream() {
  const params = { headers: { 'Accept': 'text/event-stream' }, timeout: '90s' };
  const res = http.get(`${SSE_URL}/api/stream/news`, params);
  check(res, { 'stream connected': (r) => r.status === 200 });
}

export function test_news_current() {
  const res = http.get(`${WEB_URL}/api/news/current`);
  check(res, { 'news_current 200': (r) => r.status === 200 });
  sleep(Math.random() * 0.5 + 0.1);
}

export function test_news_upcoming() {
  const res = http.get(`${WEB_URL}/api/news/upcoming`);
  check(res, { 'news_upcoming 200': (r) => r.status === 200 });
  sleep(Math.random() * 0.5 + 0.1);
}

export function test_news_events() {
  const res = http.get(`${WEB_URL}/api/news/events`);
  check(res, { 'news_events 200': (r) => r.status === 200 });
  sleep(Math.random() * 0.5 + 0.1);
}

export function test_news_markers() {
  const sym = symbols[Math.floor(Math.random() * symbols.length)];
  const res = http.get(`${WEB_URL}/api/news/markers/${sym}`);
  check(res, { 'news_markers 200': (r) => r.status === 200 });
  sleep(Math.random() * 0.5 + 0.1);
}

export function test_strategies() {
  const res = http.get(`${WEB_URL}/api/strategies/all`);
  check(res, { 'strategies 200': (r) => r.status === 200 });
  sleep(Math.random() * 0.5 + 0.1);
}
