# 🎯 AI Trading Bot - Dashboard Features Roadmap

> **Last Updated:** December 25, 2025  
> **Status:** ⚠️ Planning Phase - **NOT IMPLEMENTED**  
> **FastAPI Status:** ✅ Core endpoints functional  
> **Frontend Status:** ❌ Dashboard features not built yet

---

## 🔧 What Was Fixed (November 8, 2025)

### FastAPI Backend Updates:
✅ **CORS Configuration** - Added subdomain wildcard support (`https://*.pipfactor.com`)  
✅ **Database Queries** - Updated all queries to match actual schema columns:
- Fixed `get_latest_signal_from_db` - Returns correct `direction`, `entry_signal` fields
- Fixed `get_old_signal_from_db` - Returns correct preview data
- Fixed `get_latest_regime_from_db` - Returns all regime fields with proper aliasing
- Fixed `get_regime_for_pair` - Returns complete regime data with timestamps
✅ **Error Handling** - Added `exc_info=True` to all error logs for better debugging  
✅ **Logging** - Enhanced logging messages with more context

### Issues Resolved:
- ❌ Column name mismatches between API and database
- ❌ Missing fields in responses (regime_id, batch_id, created_at)
- ❌ Aliasing inconsistencies (e.g., `as risk_reward` vs `risk_reward_ratio`)
- ❌ CORS rejections for subdomains

---

## 📊 System Architecture Overview

### Backend Split Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    CLIENT (React + Vite)                     │
└─────────────────────────────────────────────────────────────┘
                            │
            ┌───────────────┴────────────────┐
            │                                │
┌───────────▼──────────┐        ┌───────────▼──────────────┐
│  SUPABASE (Auth DB)  │        │  FASTAPI (Trading API)    │
│  ─────────────────── │        │  ─────────────────────    │
│  • User Auth         │        │  • Strategies            │
│  • Profiles          │        │  • Regime Analysis       │
│  • Subscriptions     │        │  • News Analysis         │
│  • Payment Tracking  │        │  • Performance Metrics   │
│  • Pair Selections   │        │  • User Preferences      │
│  • RLS Policies      │        │  • Search & Filtering    │
└──────────────────────┘        └──────────────────────────┘
         │                                   │
         │                                   │
    ┌────▼────┐                         ┌───▼───┐
    │ Supabase│                         │ n8n   │
    │ PostGres│                         │ PostGr│
    │   DB    │                         │ es DB │
    └─────────┘                         └───────┘
```

### Database Responsibilities

**Supabase Database (User Management):**
- `profiles` - User accounts
- `subscription_plans` - Plan configurations
- `user_subscriptions` - Active subscriptions with TTL
- `user_pair_selections` - User's chosen trading pairs
- `payment_history` - Payment transactions
- `account_deletion_requests` - Account deletion with OTP

**n8n PostgreSQL (Trading Data):**
- `analysis_batches` - Workflow run tracking
- `regime_data` - Market regime classifications
- `strategies` - AI-generated trading signals
- `signals` - MT5 trade execution (NOT for users - internal only)
- `email_news_analysis` - News sentiment analysis
- `regime_vectors`, `strategy_vectors`, `email_news_vectors` - Semantic search

---

## 🎨 Proposed Dashboard Enhancement

### Current State: Signal Page
**Current Features:**
- View single trading pair strategy
- See regime analysis for selected pair
- View recent/upcoming news
- Basic pair dropdown selector

### Proposed: User Dashboard
**Transform Signal page into comprehensive dashboard**

---

## 📋 Feature Categories

### ✅ Phase 1: Core Dashboard (HIGH PRIORITY)

#### 1.1 User Preferences & Personalization
**Database:** New table in n8n PostgreSQL
```sql
CREATE TABLE user_preferences (
    user_id UUID PRIMARY KEY,  -- Links to Supabase auth.users
    favorite_pairs TEXT[],
    saved_strategy_ids INTEGER[],
    saved_news_ids INTEGER[],
    notification_settings JSONB,
    dashboard_layout JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
```

**Features:**
- ⭐ Favorite trading pairs (star icon in dropdown)
- 💾 Bookmark strategies for later review
- 📌 Save news articles
- 🔔 Notification preferences
- 🎨 Remember last selected pair/tab

**FastAPI Endpoints:**
```
GET    /api/user/preferences
PUT    /api/user/preferences
POST   /api/user/save-strategy/{strategy_id}
DELETE /api/user/unsave-strategy/{strategy_id}
POST   /api/user/save-news/{news_id}
DELETE /api/user/unsave-news/{news_id}
```

---

#### 1.2 Advanced Search & Filtering
**No new tables needed - use existing data**

**Strategy Search:**
```
GET /api/strategies/search?
    pair=XAUUSD&
    confidence=High&
    direction=long&
    date_from=2025-11-01&
    date_to=2025-11-08&
    risk_reward_min=2.0
```

**Regime History:**
```
GET /api/regime/history/{pair}?days=30
Returns: Timeline of regime changes for visualization
```

**News Search:**
```
GET /api/news/search?
    keywords=USD,inflation&
    importance_min=4&
    sentiment=bullish&
    instruments=XAUUSD,EURUSD
```

**Features:**
- 🔍 Filter strategies by confidence, direction, risk/reward
- 📈 View regime transition history
- 📰 Search news by keywords, sentiment, importance
- 📅 Date range filters

---

#### 1.3 Watchlist (Quick Access to Favorite Pairs)
**Uses existing data + user_preferences table**

```
GET /api/watchlist
Returns:
[
  {
    "pair": "XAUUSD",
    "latest_regime": "Trending Bull",
    "active_strategy": {...},
    "last_updated": "2025-11-08T10:30:00Z"
  },
  ...
]
```

**Features:**
- 📊 Grid view of favorite pairs
- 🚀 Quick navigation to any pair
- 🔄 Real-time status indicators
- ⚡ One-click access to full analysis

---

### ✅ Phase 2: Enhanced Visualization (MEDIUM PRIORITY)

#### 2.1 Regime History Timeline
**Visual timeline component showing:**
- 📈 Regime type changes over time (color-coded)
- 🎯 Confidence scores at each point
- 📅 Date markers
- 🔍 Click to see detailed analysis

**Example UI:**
```
Nov 1  ├──[Trending Bull]──┤  Nov 5  ├─[Ranging]─┤  Nov 8
       Confidence: 85%               Confidence: 70%
```

---

#### 2.2 Strategy Performance Tracking
**Track user interactions with strategies:**

```sql
CREATE TABLE user_strategy_interactions (
    interaction_id SERIAL PRIMARY KEY,
    user_id UUID,
    strategy_id INTEGER,
    action VARCHAR(20), -- 'viewed', 'saved', 'executed', 'ignored'
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

**Features:**
- ✅ Mark strategies as "executed" or "ignored"
- 📝 Add personal notes to strategies
- 📊 View your interaction history
- 🏆 See which strategies you followed

---

#### 2.3 News-Regime-Strategy Correlation
**Show the flow of information:**
```
High-Impact News (Importance: 5)
    ↓
Regime Change (Bull → Ranging)
    ↓
New Strategy Generated
```

**Features:**
- 🔗 Link news to regime changes
- 📊 Visualize cause-effect relationships
- ⏱️ Timeline view of events

---

### ✅ Phase 3: Social & Collaboration (LOW PRIORITY)

#### 3.1 Strategy Sharing
- 📤 Export strategies to PDF/CSV
- 🔗 Share strategy links with other users
- 📊 Public performance leaderboard (opt-in)

#### 3.2 Notes & Annotations
- 📝 Add personal notes to strategies
- 🏷️ Tag strategies (e.g., "worked-well", "avoid-volatile")
- 🔖 Organize saved strategies with tags

---

## 🛠️ Technical Implementation Details

### FastAPI Route Structure

```
/api/
├── health (existing)
├── signals/{pair} (existing - needs update)
├── preview/{pair} (existing - needs update)
├── regime (existing - needs update)
├── regime/{pair} (existing - needs update)
├── news/current (existing - needs update)
├── news/upcoming (existing - needs update)
│
├── /user/ (NEW - User Preferences)
│   ├── preferences (GET, PUT)
│   ├── saved-strategies (GET, POST, DELETE)
│   ├── saved-news (GET, POST, DELETE)
│   └── watchlist (GET, POST, DELETE)
│
├── /strategies/ (NEW - Enhanced Strategy Endpoints)
│   ├── search (GET with filters)
│   ├── history/{pair} (GET)
│   └── active (GET with optional pair filter)
│
├── /regime/ (NEW - Enhanced Regime Endpoints)
│   ├── history/{pair} (GET)
│   └── transitions (GET - all regime changes)
│
├── /news/ (NEW - Enhanced News Endpoints)
│   ├── search (GET with filters)
│   └── {id} (GET single news detail)
│
└── /dashboard/ (NEW - Dashboard Overview)
    ├── overview (GET)
    ├── performance (GET)
    └── recent-activity (GET)
```

---

## 🎨 Frontend Dashboard Layout

### Proposed Tab Structure

```
🏠 DASHBOARD PAGE
├── 📊 Overview Tab
│   ├── Watchlist widget (favorite pairs quick view)
│   ├── Recent strategies carousel
│   ├── Performance metrics cards
│   └── News highlights
│
├── 📈 Strategies Tab (Current Signal page enhanced)
│   ├── Pair selector (with favorites star)
│   ├── Active strategy card
│   ├── Regime analysis
│   ├── Search & filter panel (collapsible)
│   └── Strategy history timeline
│
├── 📰 News Tab
│   ├── Recent news (with save button)
│   ├── Upcoming events
│   ├── Search & filter panel
│   └── Saved news section
│
├── 💾 Saved Tab
│   ├── Saved strategies grid
│   ├── Saved news list
│   └── Quick notes
│
└── ⚙️ Settings Tab
    ├── Favorite pairs management
    ├── Notification preferences
    └── Dashboard customization
```

---

## 🎯 UI/UX Enhancements

### Quick Actions Bar
```
┌─────────────────────────────────────────────┐
│  [★ Star Pair] [💾 Save] [🔔 Alert] [📤 Share]  │
└─────────────────────────────────────────────┘
```

### Smart Filters (Collapsible Panel)
```
┌─ Filters ─────────────────────────────────┐
│  Confidence: [All] [High] [Medium] [Low]  │
│  Direction:  [All] [Long] [Short]         │
│  Date Range: [7 days ▼]                   │
│  [Apply Filters]                          │
└───────────────────────────────────────────┘
```

### Timeline View (Horizontal Scroll)
```
Nov 1         Nov 3         Nov 5         Nov 8
  │             │             │             │
  ├─Strategy    ├─Regime      ├─Strategy    ├─News
  │  Generated  │  Changed    │  Generated  │  Event
```

### Comparison Mode (Side-by-Side)
```
┌────────────┬────────────┬────────────┐
│  XAUUSD    │  EURUSD    │  GBPUSD    │
│  Bull      │  Bear      │  Ranging   │
│  Conf: 85% │  Conf: 70% │  Conf: 60% │
└────────────┴────────────┴────────────┘
```

---

## 🔐 Security Considerations

### Authentication Flow
```
User Login (Supabase Auth)
    ↓
Get JWT Token
    ↓
FastAPI validates token → Extract user_id
    ↓
Query n8n PostgreSQL with user_id filter
    ↓
Return personalized data
```

### RLS (Row Level Security)
- **Supabase:** Already has RLS policies for user data
- **FastAPI:** Implement middleware to validate user_id from JWT
- **n8n Database:** Add user_id column to preference tables

### Rate Limiting
```python
# Add to FastAPI
from fastapi_limiter import FastAPILimiter
from fastapi_limiter.depends import RateLimiter

@app.get("/api/strategies/search", dependencies=[Depends(RateLimiter(times=10, seconds=60))])
```

---

## 📈 Priority Recommendation

### ✅ Week 1-2: Fix Current Issues + Core Features
1. ✅ Fix CORS configuration
2. ✅ Update existing endpoints for new schema
3. ✅ Add proper error handling
4. 🆕 Create user_preferences table
5. 🆕 Implement saved strategies feature
6. 🆕 Add search by pair endpoint

### 🎯 Week 3-4: Enhanced Features
7. Add regime history timeline
8. Implement advanced search filters
9. Create dashboard overview page
10. Build user preferences UI

### 🚀 Future Enhancements (Month 2+)
11. Strategy interaction tracking
12. News-regime correlation visualization
13. Export/sharing features
14. Mobile app optimization

---

## ❓ Questions & Decisions

### 1. Multi-user or Single-user?
**Answer:** Multi-user (you already have Supabase auth setup)

### 2. Subscription-based pair access?
**Answer:** Yes - users select pairs based on subscription plan
- Starter: 1 pair
- Professional: 3 pairs
- Elite: All pairs
- Beta: All pairs (temporary)

### 3. Priority Features?
**Top 3 Must-Haves:**
1. Saved strategies (bookmark for review)
2. Watchlist (quick access to favorite pairs)
3. Search & filter (find specific strategies/news)

### 4. Mobile Responsive?
**Answer:** Yes - responsive design priority

---

## 🚫 Out of Scope (NOT for Users)

### MT5 Trade Execution System
**These features are for internal use only:**
- ❌ Live trade execution via MT5 EA
- ❌ Real-time P/L tracking
- ❌ Trade history from `signals` table
- ❌ Performance analytics from actual trades
- ❌ MT5 integration endpoints (`/trigger`, `/signal`)

**Reason:** Currently in heavy testing phase, auto-trading is personal use only

---

## 📝 Implementation Notes

### Database Connection Strategy
```python
# FastAPI connects to TWO databases:
SUPABASE_DB = "postgresql://supabase-user@supabase.co:5432/postgres"
N8N_DB = "postgresql://n8n-postgres:5432/ai_trading_bot_data"

# Use Supabase for:
# - User authentication validation
# - Subscription status checks
# - User profile data

# Use n8n DB for:
# - Trading strategies
# - Regime analysis
# - News data
# - User preferences (new)
```

### CORS Configuration
```python
origins = [
    "http://localhost:5173",
    "https://pipfactor.com",
    "https://*.pipfactor.com",  # Subdomain wildcard
]
```

### Error Handling Pattern
```python
try:
    # Database query
    result = await get_data(pair)
    if not result:
        raise HTTPException(404, f"No data found for {pair}")
    return JSONResponse(content=result)
except HTTPException:
    raise  # Re-raise HTTP exceptions
except Exception as e:
    logger.error(f"Unexpected error: {str(e)}", exc_info=True)
    raise HTTPException(500, "Internal server error")
```

---

## 📚 API Documentation

### Once implemented, FastAPI will auto-generate docs:
- **Swagger UI:** `http://localhost:8080/docs`
- **ReDoc:** `http://localhost:8080/redoc`

---

## 🎉 Success Metrics

### Phase 1 Success Criteria:
- [ ] All existing endpoints return correct data
- [ ] Users can save/unsave strategies
- [ ] Users can filter strategies by pair
- [ ] Watchlist shows favorite pairs
- [ ] Error rates < 1%

### Phase 2 Success Criteria:
- [ ] Regime history timeline renders correctly
- [ ] Search filters work with multiple parameters
- [ ] Dashboard loads in < 2 seconds
- [ ] Mobile responsive on all screen sizes

---

## 🔧 Maintenance & Operations

### Daily Tasks:
- Monitor error logs in FastAPI
- Check Redis cache hit rates
- Verify subscription TTL expiration (Supabase Edge Function)

### Weekly Tasks:
- Review user feedback on new features
- Analyze most-used search filters
- Check database query performance

### Monthly Tasks:
- Archive old data (regime_data, strategies older than 2 years)
- Review and optimize slow queries
- Update feature roadmap based on usage

---

## 📞 Contact & Support

**Developer:** Priyodip  
**Project:** AI Trading Bot  
**Tech Stack:** FastAPI + Supabase + n8n + PostgreSQL + Redis  
**Status:** Planning Phase → Implementation Starting Soon

---

## 📋 Quick Reference

### Current Working Endpoints (Fixed Nov 8):
```
✅ GET  /api/health
✅ GET  /api/signals/{pair}        - Active strategy for pair
✅ GET  /api/preview/{pair}        - Preview strategy (XAUUSD only)
✅ GET  /api/strategies?pair=X     - All active strategies
✅ GET  /api/regime                - All pairs regime data
✅ GET  /api/regime/{pair}         - Single pair regime
✅ GET  /api/news/current          - Recent news
✅ GET  /api/news/upcoming         - Upcoming events
✅ GET  /api/performance/{pair}    - Pair performance metrics
✅ POST /api/trigger               - MT5 data collection (internal)
✅ POST /api/signal                - Receive signals from n8n (internal)
```

### Database Tables in Use:
**Supabase (User Data):**
- profiles, subscription_plans, user_subscriptions, user_pair_selections, payment_history

**n8n PostgreSQL (Trading Data):**
- strategies, regime_data, email_news_analysis, analysis_batches
- regime_vectors, strategy_vectors, email_news_vectors (semantic search)

### NOT in Use (MT5 Internal Only):
- ❌ signals table (trade execution tracking)
- ❌ /api/trades/* endpoints (MT5 integration)
- ❌ /api/performance/* from actual trades

---

*This roadmap is a living document and will be updated as features are implemented.*
