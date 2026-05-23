import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
  scenarios: {
    historical_api: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '30s', target: 200 },
        { duration: '1m', target: 500 },
        { duration: '1m', target: 500 },
        { duration: '30s', target: 0 },
      ],
      exec: 'test_historical',
    },
    news_stream: {
      executor: 'constant-vus',
      vus: 100,
      duration: '3m',
      exec: 'test_stream',
    }
  }
};

const WEB_URL = 'http://tradingbot-api-web:8080';
const SSE_URL = 'http://tradingbot-api-sse:8081';

export function test_historical() {
  const symbols = ['USDJPY', 'EURUSD', 'GBPUSD'];
  const sym = symbols[Math.floor(Math.random() * symbols.length)];
  
  const res = http.get(`${WEB_URL}/api/historical/${sym}/H1`);
  
  check(res, {
    'status is 200': (r) => r.status === 200,
  });
  
  sleep(Math.random() * 0.5 + 0.1); 
}

export function test_stream() {
  const params = {
    headers: { 'Accept': 'text/event-stream' },
    timeout: '180s',
  };
  
  const res = http.get(`${SSE_URL}/api/stream/news`, params);
  
  check(res, {
    'stream connected': (r) => r.status === 200,
  });
}
