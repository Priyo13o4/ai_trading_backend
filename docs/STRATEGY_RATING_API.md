# Strategy Rating & Feedback API

**Created:** February 5, 2026  
**Updated:** February 5, 2026 (Added execution control fields)  
**Purpose:** User rating system for progressive AI learning + execution control for news-aware trading

---

## 📊 Database Schema

### strategies table - Rating & Execution Control Columns

**Rating Columns:**

| Column | Type | Constraint | Description |
|--------|------|------------|-------------|
| `user_rating` | NUMERIC(2,1) | 1.0 - 5.0 | Individual user's rating (stars) |
| `rating_count` | INTEGER | DEFAULT 0 | Total number of ratings received |
| `avg_rating` | NUMERIC(3,2) | 1.0 - 5.0 | Average of all ratings |
| `user_feedback` | TEXT | NULL | Optional user comment |

**Execution Control Columns (NEW):**

| Column | Type | Constraint | Description |
|--------|------|------------|-------------|
| `trade_mode` | VARCHAR(20) | protective, news_opportunistic, scalping, swing | Trade intent mode - determines risk parameters |
| `execution_allowed` | BOOLEAN | DEFAULT true | Whether EA/backend can execute this strategy |
| `risk_level` | VARCHAR(20) | normal, high, extreme | Risk classification for human interpretation |

**Indexes:**
- `idx_strategies_rating` on (avg_rating DESC, rating_count DESC)
- `idx_strategies_trade_mode` on (trade_mode, execution_allowed, status)

**Key Concepts:**
- **execution_allowed = false:** Strategy is informational only (e.g., pre-event analysis, impact window diagnostic). EA MUST NOT execute.
- **execution_allowed = true:** Strategy is executable (normal protective mode or post-event news opportunistic mode).
- **trade_mode:**
  - `protective`: Standard market conditions, normal risk parameters
  - `news_opportunistic`: Post-event exploitation (5-60min after high-impact news), wider stops, higher spread tolerance
  - `scalping`: Future use for ultra-short-term strategies
  - `swing`: Future use for multi-day position trading
- **risk_level:**
  - `normal`: Standard protective mode trading
  - `high`: News opportunistic mode (wider stops, higher spread tolerance expected)
  - `extreme`: Pre-event or impact window (execution blocked)

---

## 🔌 API Endpoints

### 1. Submit Strategy Rating

**Endpoint:** `POST /api/strategies/{strategy_id}/rate`

**Request Body:**
```json
{
  "user_rating": 4.5,
  "user_feedback": "Great entry timing, caught the pullback perfectly!"
}
```

**Validation:**
- `user_rating`: Required, NUMERIC(2,1), range 1.0 - 5.0
- `user_feedback`: Optional, TEXT, max 1000 characters
- `strategy_id`: Must exist and not be archived

**Response (200 OK):**
```json
{
  "success": true,
  "strategy_id": 142,
  "user_rating": 4.5,
  "rating_count": 8,
  "avg_rating": 4.32,
  "message": "Rating submitted successfully"
}
```

**Response (400 Bad Request):**
```json
{
  "success": false,
  "error": "Invalid rating value. Must be between 1.0 and 5.0"
}
```

**Response (404 Not Found):**
```json
{
  "success": false,
  "error": "Strategy not found"
}
```

**SQL Logic:**
```sql
-- Update the rating
UPDATE strategies 
SET 
  user_rating = $1,
  rating_count = rating_count + 1,
  avg_rating = CASE 
    WHEN avg_rating IS NULL THEN $1
    ELSE ROUND(((avg_rating * rating_count) + $1) / (rating_count + 1), 2)
  END,
  user_feedback = $2
WHERE strategy_id = $3 
  AND archived = false
RETURNING strategy_id, user_rating, rating_count, avg_rating;
```

---

### 2. Get Strategy with Rating

**Endpoint:** `GET /api/strategies/{strategy_id}`

**Response (200 OK):**
```json
{
  "strategy_id": 142,
  "trading_pair": "EURUSD",
  "strategy_name": "EURUSD Long H1 Pullback Entry",
  "direction": "long",
  "entry_signal": {
    "condition_type": "pullback_entry",
    "level": 1.20500,
    "timeframe": "H1",
    "confirmation": "bullish_engulfing"
  },
  "take_profit": 1.21500,
  "stop_loss": 1.19800,
  "risk_reward_ratio": 1.43,
  "confidence": "High",
  "expiry_time": "2026-02-05T14:30:00Z",
  "status": "active",
  "detailed_analysis": "...",
  "timestamp": "2026-02-05T12:30:00Z",
  "user_rating": 4.5,
  "rating_count": 8,
  "avg_rating": 4.32,
  "user_feedback": "Great entry timing!",
  "trade_mode": "protective",
  "execution_allowed": true,
  "risk_level": "normal",
  "spread_tolerance_pips": 5.0
}
```

---

### 3. Get All Strategies (Updated with Filters)

**Endpoint:** `GET /api/strategies`

**Query Parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `trading_pair` | string | null | Filter by pair (e.g., "EURUSD") |
| `status` | string | "active" | Filter by status: active, expired, executed, cancelled, informational |
| `confidence` | string | null | Filter by confidence: High, Medium, Low |
| `direction` | string | null | Filter by direction: long, short |
| `trade_mode` | string | null | Filter by mode: protective, news_opportunistic, scalping, swing |
| `execution_allowed` | boolean | null | Filter by execution permission: true (executable), false (informational) |
| `risk_level` | string | null | Filter by risk: normal, high, extreme |
| `min_rating` | float | null | Minimum avg_rating (e.g., 4.0) |
| `min_rating_count` | int | null | Minimum number of ratings (e.g., 3) |
| `limit` | int | 50 | Max results (max: 100) |
| `offset` | int | 0 | Pagination offset |
| `sort_by` | string | "timestamp" | Sort field: timestamp, avg_rating, confidence, risk_reward_ratio |
| `order` | string | "desc" | Sort order: asc, desc |

**Example Requests:**

1. **Get active high-rated EXECUTABLE strategies:**
```
GET /api/strategies?status=active&execution_allowed=true&min_rating=4.0&min_rating_count=3&sort_by=avg_rating
```

2. **Get all EURUSD strategies:**
```
GET /api/strategies?trading_pair=EURUSD&limit=20
```

3. **Get news opportunistic strategies:**
```
GET /api/strategies?trade_mode=news_opportunistic&execution_allowed=true&status=active
```

4. **Get informational (non-executable) analyses:**
```
GET /api/strategies?execution_allowed=false&risk_level=extreme&limit=10
```

3. **Get recent High confidence strategies:**
```
GET /api/strategies?confidence=High&status=active&sort_by=timestamp
```

**Response (200 OK):**
```json
{
  "success": true,
  "total": 142,
  "limit": 50,
  "offset": 0,
  "strategies": [
    {
      "strategy_id": 142,
      "trading_pair": "EURUSD",
      "strategy_name": "EURUSD Long H1 Pullback Entry",
      "direction": "long",
      "confidence": "High",
      "status": "active",
      "avg_rating": 4.32,
      "rating_count": 8,
      "risk_reward_ratio": 1.43,
      "timestamp": "2026-02-05T12:30:00Z",
      "expiry_time": "2026-02-05T14:30:00Z"
    }
  ]
}
```

**SQL Query (Base):**
```sql
SELECT 
    strategy_id,
    trading_pair,
    strategy_name,
    direction,
    entry_signal,
    take_profit,
    stop_loss,
    risk_reward_ratio,
    confidence,
    expiry_minutes,
    timestamp,
    expiry_time,
    detailed_analysis,
    status,
    user_rating,
    rating_count,
    avg_rating,
    user_feedback
FROM strategies
WHERE archived = false
    AND ($1::VARCHAR IS NULL OR trading_pair = $1)
    AND ($2::VARCHAR IS NULL OR status = $2)
    AND ($3::VARCHAR IS NULL OR confidence = $3)
    AND ($4::VARCHAR IS NULL OR direction = $4)
    AND ($5::NUMERIC IS NULL OR avg_rating >= $5)
    AND ($6::INTEGER IS NULL OR rating_count >= $6)
ORDER BY 
    CASE WHEN $7 = 'timestamp' THEN timestamp END DESC,
    CASE WHEN $7 = 'avg_rating' THEN avg_rating END DESC,
    CASE WHEN $7 = 'confidence' THEN confidence END DESC,
    CASE WHEN $7 = 'risk_reward_ratio' THEN risk_reward_ratio END DESC
LIMIT $8 OFFSET $9;
```

---

## 🎯 Frontend Integration

### Rating Component Example

```typescript
interface RatingSubmission {
  strategy_id: number;
  user_rating: number; // 1.0 - 5.0
  user_feedback?: string;
}

async function submitRating(data: RatingSubmission) {
  const response = await fetch(`/api/strategies/${data.strategy_id}/rate`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${userToken}`
    },
    body: JSON.stringify({
      user_rating: data.user_rating,
      user_feedback: data.user_feedback
    })
  });
  
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.error);
  }
  
  return await response.json();
}
```

### Display Rating Stars

```tsx
function StrategyRating({ strategy }) {
  return (
    <div className="strategy-rating">
      <div className="stars">
        {strategy.avg_rating ? (
          <>
            <span className="rating-value">{strategy.avg_rating.toFixed(1)}★</span>
            <span className="rating-count">({strategy.rating_count} ratings)</span>
          </>
        ) : (
          <span className="no-rating">Not rated yet</span>
        )}
      </div>
    </div>
  );
}
```

---

## 📈 Progressive Learning Flow

### 1. User Rates Strategy
```
User opens strategy detail page
→ Sees rating interface (1-5 stars + optional feedback)
→ Submits rating
→ Backend updates: user_rating, rating_count, avg_rating
→ Frontend shows updated average
```

### 2. AI Uses Ratings for Learning
```
Strategy Selector V3 workflow runs
→ AI calls get_high_rated_strategies_in_regime(regime_type, min_rating=4.0)
→ Returns proven winning strategies in similar regimes
→ AI analyzes patterns (entry types, timeframes, confirmations)
→ Boosts confidence if current setup matches high-rated patterns
→ Mentions in detailed_analysis: "Similar setup worked in Jan 2026 (4.5★, 8 ratings)"
```

### 3. Rating Thresholds for Reliability

| Rating Count | Reliability | AI Weight |
|--------------|-------------|-----------|
| 1-2 ratings | Low | 10% weight |
| 3-5 ratings | Medium | 50% weight |
| 6-10 ratings | High | 80% weight |
| 10+ ratings | Very High | 100% weight |

**Recommendation:** Require `rating_count >= 3` for AI learning queries

---

## 🔐 Security Considerations

1. **Rate Limiting:** Max 10 ratings per user per hour
2. **Authentication:** Require valid JWT token
3. **Authorization:** Users can only rate strategies once (optional: implement user_ratings table)
4. **Validation:** 
   - Sanitize user_feedback (XSS protection)
   - Validate rating value server-side
   - Check strategy exists and not archived

---

## 📊 Analytics Queries

### Top Rated Strategies by Pair
```sql
SELECT 
    trading_pair,
    COUNT(*) as total_strategies,
    AVG(avg_rating) as avg_pair_rating,
    SUM(rating_count) as total_ratings
FROM strategies
WHERE avg_rating IS NOT NULL
    AND rating_count >= 3
    AND archived = false
GROUP BY trading_pair
ORDER BY avg_pair_rating DESC;
```

### Most Successful Entry Types
```sql
SELECT 
    entry_signal->>'condition_type' as condition_type,
    COUNT(*) as strategy_count,
    AVG(avg_rating) as avg_rating,
    AVG(risk_reward_ratio) as avg_rr
FROM strategies
WHERE avg_rating >= 4.0
    AND rating_count >= 3
    AND archived = false
GROUP BY entry_signal->>'condition_type'
ORDER BY avg_rating DESC;
```

### Rating Distribution
```sql
SELECT 
    CASE 
        WHEN avg_rating >= 4.5 THEN '4.5-5.0 (Excellent)'
        WHEN avg_rating >= 4.0 THEN '4.0-4.4 (Great)'
        WHEN avg_rating >= 3.0 THEN '3.0-3.9 (Good)'
        WHEN avg_rating >= 2.0 THEN '2.0-2.9 (Fair)'
        ELSE '1.0-1.9 (Poor)'
    END as rating_category,
    COUNT(*) as strategy_count
FROM strategies
WHERE avg_rating IS NOT NULL
    AND rating_count >= 3
GROUP BY rating_category
ORDER BY rating_category DESC;
```

---

## 🚀 Future Enhancements (Not Implemented Yet)

1. **Per-User Rating Tracking:**
```sql
CREATE TABLE user_strategy_ratings (
    rating_id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    strategy_id INTEGER NOT NULL,
    rating NUMERIC(2,1) NOT NULL,
    feedback TEXT,
    rated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, strategy_id)
);
```

2. **MT5 Execution Feedback:**
```sql
ALTER TABLE strategies ADD COLUMN execution_result VARCHAR(20);
ALTER TABLE strategies ADD COLUMN actual_pnl NUMERIC(10,2);
ALTER TABLE strategies ADD COLUMN actual_pnl_pips NUMERIC(8,2);
```

3. **Weighted Ratings:** Give more weight to users with proven track records

---

## 📝 Implementation Checklist

Backend (Python/FastAPI):
- [ ] Create `POST /api/strategies/{id}/rate` endpoint
- [ ] Update `GET /api/strategies/{id}` to include rating fields
- [ ] Update `GET /api/strategies` with new filters (min_rating, sort_by=avg_rating)
- [ ] Add input validation (rating 1.0-5.0, feedback max length)
- [ ] Add rate limiting middleware
- [ ] Create analytics endpoints

Frontend (React/TypeScript):
- [ ] Create RatingStars component (1-5 stars input)
- [ ] Add rating display to strategy cards
- [ ] Create feedback modal/form
- [ ] Add filter UI (min_rating slider)
- [ ] Show rating statistics on dashboard
- [ ] Implement user feedback list view

Testing:
- [ ] Unit tests for rating calculation logic
- [ ] API endpoint tests (valid/invalid inputs)
- [ ] Test avg_rating recalculation accuracy
- [ ] Load test rating endpoint (rate limiting)

---

**Questions?** Check the STRATEGY_SELECTOR_V3_FIXES.md for AI prompt integration details.
