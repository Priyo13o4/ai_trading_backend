CREATE TABLE public.weekly_macro_playbook (
    playbook_id SERIAL PRIMARY KEY,
    target_week_start TIMESTAMP WITH TIME ZONE NOT NULL,
    date_range VARCHAR(100),
    dominant_themes JSONB,
    currency_bias JSONB,
    high_risk_windows JSONB,
    overall_strategy TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Index to quickly grab the most recent playbook
CREATE INDEX idx_weekly_playbook_date ON public.weekly_macro_playbook (target_week_start DESC);


CREATE TABLE public.economic_event_analysis (
    analysis_id SERIAL PRIMARY KEY,
    event_name VARCHAR(255) NOT NULL,
    event_time TIMESTAMP WITH TIME ZONE NOT NULL,
    currency VARCHAR(10) NOT NULL,
    impact VARCHAR(20),
    key_numbers JSONB,
    market_pricing_sentiment TEXT,
    primary_affected_pairs JSONB, -- Stored as JSONB array for easy n8n insertion
    trading_scenarios JSONB,
    market_dynamics JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Optional: Create indexes for fast querying by the Strategy agent later
CREATE INDEX IF NOT EXISTS idx_economic_event_time ON public.economic_event_analysis (event_time DESC);
CREATE INDEX IF NOT EXISTS idx_economic_currency ON public.economic_event_analysis (currency);
CREATE INDEX IF NOT EXISTS idx_economic_impact_event_time ON public.economic_event_analysis (impact, event_time DESC);
CREATE INDEX IF NOT EXISTS idx_economic_created_at_desc ON public.economic_event_analysis (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_weekly_playbook_created_at_desc ON public.weekly_macro_playbook (created_at DESC);