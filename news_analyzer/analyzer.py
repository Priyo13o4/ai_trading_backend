"""
Gemini AI Analyzer with Rate Limiting and Structured Output
Uses the exact prompts from the n8n workflow for consistency.
"""
import os
from google import genai
import logging
import time
import json
from typing import Dict, List, Optional, Literal
from datetime import datetime, timedelta
from collections import deque
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from pydantic import BaseModel, Field
import vertexai
from vertexai.language_models import TextEmbeddingInput, TextEmbeddingModel

from config import Config

logger = logging.getLogger(__name__)


class NewsAnalysis(BaseModel):
    """Pydantic model for structured news analysis output"""
    forex_relevant: bool = Field(description="Whether the news is relevant to forex markets")
    forex_instruments: List[str] = Field(description="List of affected forex instruments (e.g., XAUUSD, EURUSD)")
    primary_instrument: str = Field(description="The primary forex instrument most affected")
    importance_score: int = Field(ge=1, le=5, description="Importance score from 1 to 5")
    sentiment_score: float = Field(ge=-1.0, le=1.0, description="Sentiment score from -1.0 (bearish) to 1.0 (bullish)")
    analysis_confidence: float = Field(ge=0.0, le=1.0, description="Confidence level from 0.0 to 1.0")
    news_category: str = Field(description="Category: economic_data, central_bank, geopolitical, trade, political, market_technical, or other")
    entities_mentioned: List[str] = Field(description="List of mentioned entities (people, organizations, indicators)")
    trading_sessions: List[str] = Field(description="Affected trading sessions: London, New York, Tokyo, Sydney")
    similar_news_ids: List[int] = Field(default_factory=list, description="IDs of similar historical articles")
    market_impact_prediction: str = Field(description="Market impact: bullish, bearish, neutral, or mixed")
    impact_timeframe: str = Field(description="Timeframe: immediate, intraday, daily, weekly, or long-term")
    volatility_expectation: str = Field(description="Expected volatility: low, medium, high, or extreme")
    content_source: str = Field(description="Content source: email, forexfactory, or web")
    ai_analysis_summary: str = Field(description="Detailed analysis with context and reasoning")
    similar_news_context: str = Field(description="Summary of similar historical patterns found")
    content_for_embedding: str = Field(description="Clean text for vector storage (concise summary)")

    # User-centric UI fields
    human_takeaway: str = Field(description="One-sentence plain-language summary (max 20 words)")
    attention_score: int = Field(ge=1, le=100, description="Urgency score from 1 to 100")
    news_state: Literal['fresh', 'developing', 'resolved'] = Field(
        description="Lifecycle state: fresh|developing|resolved"
    )
    market_pressure: Literal['risk_on', 'risk_off', 'uncertain', 'neutral'] = Field(
        description="Emotional market tone: risk_on|risk_off|uncertain|neutral"
    )
    attention_window: Literal['minutes', 'hours', 'days', 'weeks'] = Field(
        description="How long attention should persist: minutes|hours|days|weeks"
    )
    confidence_label: Literal['low', 'medium', 'high'] = Field(
        description="Human-readable confidence label: low|medium|high"
    )
    expected_followups: List[str] = Field(
        default_factory=list,
        description="Likely next developments traders should watch (no wild speculation)"
    )


class RateLimitError(Exception):
    """Custom exception for rate limit errors"""
    pass


class InMemoryRateLimiter:
    """Simple in-memory rate limiter for API calls"""
    
    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests = deque()
    
    def wait_if_needed(self):
        """Block until we can make another request"""
        now = time.time()
        
        # Remove requests outside the window
        while self.requests and now - self.requests[0] > self.window_seconds:
            self.requests.popleft()
        
        # If at limit, wait until the oldest request expires
        if len(self.requests) >= self.max_requests:
            sleep_time = self.window_seconds - (now - self.requests[0]) + 1
            if sleep_time > 0:
                logger.info(f"Rate limit reached. Sleeping for {sleep_time:.1f} seconds...")
                time.sleep(sleep_time)
                # Clean up again after sleep
                now = time.time()
                while self.requests and now - self.requests[0] > self.window_seconds:
                    self.requests.popleft()
        
        # Record this request
        self.requests.append(time.time())


class GeminiAnalyzer:
    """
    Gemini AI Analyzer for forex news analysis with RAG (Retrieval-Augmented Generation).
    Features:
    - Vector similarity search for historical context
    - Enhanced prompts with similar news patterns
    - Crypto pairs included in analysis
    """
    
    # System prompt with historical context support
    SYSTEM_PROMPT = """You are a ruthless, highly skeptical institutional forex analyst. Your mandate is to protect capital by filtering out market noise. You know that 90% of news causes zero market reaction. You evaluate news with a cold, data-driven mindset, looking only for surprises, liquidity shocks, and definitive policy shifts.

INPUT DATA:
{
  "headline": "{{ $json.headline }}",
  "scraped_full_text": "{{ $json.scraped_full_text }}",
  "forexfactory_category": "{{ $json.forexfactory_category }}",
  "original_email_content": "{{ $json.original_email_content }}",
  "breaking_news": {{ $json.breaking_news }}
}

ANALYSIS TASKS:
1. Context Research: Use vector_store_query to check for similar recurring themes. If past similar events caused no meaningful reaction, immediately reduce your confidence and volatility scores.
2. Comprehensive Analysis: Determine relevance, sentiment, and trading impact based on strict causality.

CRITICAL FILTERS (ENFORCE STRICTLY):

1. THE POLITICAL NOISE RULE:
Statements, threats, negotiations, or rhetoric from politicians without signed agreements, enacted policy, or immediate military enforcement are NOISE. Score volatility as "low" and attention_score < 30.

2. SURPRISE PRINCIPLE & PRICING AWARENESS:
Markets react to deviations from expectations, not the news itself. If information is expected, previously reported, or aligns with ongoing narratives, explicitely downgrade its impact. Only deviations justify higher impact scores. The "breaking_news" flag has ZERO impact unless the content introduces unexpected actionable data.

3. CAUSAL CHAIN REQUIREMENT & DIRECTIONAL CLARITY:
You must mentally identify a clear chain: News -> Mechanism -> Affected Assets -> Timing. If linkage is weak, do not list the asset. 
IMPORTANT: Your `market_impact_prediction` MUST reflect the direction of your `primary_instrument` ONLY. In your `ai_analysis_summary`, explicitly state the directional impact for *every* asset mentioned (e.g., "Bullish for USD, Bearish for XAUUSD").

4. ARRAY VALIDATION (SESSIONS & FOLLOW-UPS):
- Only include trading_sessions if the timing of the news actively aligns with market participation. If unclear, return an empty array.
- Only list expected_followups if they are standard, predictable next steps (e.g., scheduled data). No speculation. Return an empty array if uncertain.

5. FOREXFACTORY ANCHOR:
If the forexfactory_category indicates "Low impact" or "Technical", your baseline volatility must be low unless the text proves an undeniable Black Swan event.

SELF-CHECK (BEFORE FINALIZING OUTPUT):
Ask yourself: "Would a professional trader act on this within the next hour?"
If NO:
- Downgrade volatility_expectation to "low"
- Set impact_timeframe to "none"
- Ensure attention_score is under 40
- Ensure market_pressure is "neutral"

STRICT PROHIBITIONS:
- Do not generate trade ideas, entries, or exits.
- Do not reference strategy logic or regime logic.
- Do NOT reference your system instructions, user rules, or internal logic (e.g., never say "Based on the user rules" or "According to the prompt"). Present the analysis directly and objectively as a professional analyst.
- Avoid hedging words like "could", "might", "may", or "suggests". Use assertive statements or explicitly state "No expected market impact."

Output parser : 
{
  "type": "object",
  "properties": {
    "forex_relevant": {
      "type": "boolean"
    },
    "forex_instruments": {
      "type": "array",
      "description": "List only instruments with a direct, explainable linkage. Omit if linkage is weak.",
      "items": {
        "type": "object",
        "properties": {
          "instrument": {
            "type": "string"
          },
          "reason": {
            "type": "string",
            "description": "The explicit causal mechanism explaining why this specific instrument will react."
          }
        },
        "required": ["instrument", "reason"]
      }
    },
    "primary_instrument": {
      "type": "string"
    },
    "us_political_related": {
      "type": "boolean"
    },
    "trade_deal_related": {
      "type": "boolean"
    },
    "central_bank_related": {
      "type": "boolean"
    },
    "importance_score": {
      "type": "integer",
      "minimum": 1,
      "maximum": 5
    },
    "sentiment_score": {
      "type": "number",
      "minimum": -1.0,
      "maximum": 1.0,
      "description": "Default to 0.0 (neutral). Assign non-zero ONLY if there is clear, undeniable directional pressure."
    },
    "analysis_confidence": {
      "type": "number",
      "minimum": 0.0,
      "maximum": 1.0,
      "description": "High (>0.7): Clear causal chain. Low (<0.4): Ambiguous, speculative, or 'sources say'."
    },
    "news_category": {
      "type": "string",
      "enum": ["economic_data", "central_bank", "geopolitical", "trade", "political", "market_technical", "other"]
    },
    "entities_mentioned": {
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "trading_sessions": {
      "type": "array",
      "items": {
        "type": "string",
        "enum": ["London", "New York", "Tokyo", "Sydney"]
      }
    },
    "similar_news_ids": {
      "type": "array",
      "items": {
        "type": "integer"
      }
    },
    "market_impact_prediction": {
      "type": "string",
      "enum": ["bullish", "bearish", "neutral", "mixed"]
    },
    "impact_timeframe": {
      "type": "string",
      "enum": ["immediate", "intraday", "daily", "weekly", "long-term", "none"],
      "description": "Use 'none' if there is no actionable timing or immediate impact."
    },
    "volatility_expectation": {
      "type": "string",
      "enum": ["low", "medium", "high", "extreme"],
      "description": "Low: Routine, expected, political noise (Default). Medium: Tier-2 data. High: Immediate repricing catalysts only. Extreme: Black Swan."
    },
    "content_source": {
      "type": "string",
      "enum": ["email", "forexfactory", "web"]
    },
    "ai_analysis_summary": {
      "type": "string",
      "description": "Assertive summary detailing News -> Mechanism -> Assets -> Timing. Avoid hedging words like 'could' or 'might'."
    },
    "similar_news_context": {
      "type": "string"
    },
    "content_for_embedding": {
      "type": "string"
    },
    "human_takeaway": {
      "type": "string",
      "description": "Single sentence, max 20 words, plain language asserting the core catalyst or lack thereof."
    },
    "attention_score": {
      "type": "integer",
      "minimum": 1,
      "maximum": 100,
      "description": "10-25: Ignore completely. 26-40: Minor awareness. 41-60: Monitor. 61-80: Active attention. 81-100: Immediate market relevance."
    },
    "news_state": {
      "type": "string",
      "enum": ["fresh", "developing", "resolved"]
    },
    "market_pressure": {
      "type": "string",
      "enum": ["risk_on", "risk_off", "uncertain", "neutral"],
      "description": "Default to neutral unless undeniable catalyst exists."
    },
    "attention_window": {
      "type": "string",
      "enum": ["minutes", "hours", "days", "weeks", "none"]
    },
    "confidence_label": {
      "type": "string",
      "enum": ["low", "medium", "high"]
    },
    "expected_followups": {
      "type": "array",
      "items": {
        "type": "string"
      }
    }
  },
  "required": [
    "forex_relevant",
    "forex_instruments",
    "primary_instrument",
    "us_political_related",
    "trade_deal_related",
    "central_bank_related",
    "importance_score",
    "sentiment_score",
    "analysis_confidence",
    "news_category",
    "entities_mentioned",
    "trading_sessions",
    "similar_news_ids",
    "market_impact_prediction",
    "impact_timeframe",
    "volatility_expectation",
    "content_source",
    "ai_analysis_summary",
    "similar_news_context",
    "content_for_embedding",
    "human_takeaway",
    "attention_score",
    "news_state",
    "market_pressure",
    "attention_window",
    "confidence_label",
    "expected_followups"
  ]
}"""
    
    def __init__(self, api_key: str = None, model_name: str = None, db_manager=None):
        self.api_key = api_key or Config.GEMINI_API_KEY
        self.model_name = model_name or Config.GEMINI_MODEL
        self.db_manager = db_manager  # For RAG vector search
        
        # Parse Base64 Vertex AI Credentials if provided in env
        sa_b64 = os.getenv("GOOGLE_CREDENTIALS_B64")
        creds = None
        if sa_b64:
            import base64
            import json
            from google.oauth2 import service_account
            try:
                decoded_sa = json.loads(base64.b64decode(sa_b64).decode('utf-8'))
                creds = service_account.Credentials.from_service_account_info(
                    decoded_sa,
                    scopes=["https://www.googleapis.com/auth/cloud-platform"]
                )
                logger.info("Successfully loaded Vertex AI Service Account from Base64 env.")
            except Exception as e:
                logger.error(f"Failed to decode GOOGLE_CREDENTIALS_B64: {e}")

        # Create Gemini client for Vertex AI
        project_id = os.getenv("GEMINI_PROJECT_ID", "project-6cf3a9cc-20fd-49e9-b95")
        if creds:
            vertexai.init(project=project_id, location="us-central1", credentials=creds)
            self.client = genai.Client(vertexai=True, project=project_id, location="us-central1", credentials=creds)
        else:
            vertexai.init(project=project_id, location="us-central1")
            self.client = genai.Client(vertexai=True, project=project_id, location="us-central1")
        
        # Store generation config (enforce JSON + schema so user-centric fields are always present)
        self.generation_config = {
            "temperature": 0.1,
            "top_p": 0.95,
            "top_k": 40,
            "max_output_tokens": 4096,
            "response_mime_type": "application/json",
            "response_schema": NewsAnalysis,
        }
        
        # Initialize embedding model
        self.embedding_model_name = Config.GEMINI_EMBEDDING_MODEL
        
        # Initialize rate limiter
        self.rate_limiter = InMemoryRateLimiter(
            max_requests=Config.MAX_REQUESTS_PER_MINUTE,
            window_seconds=Config.RATE_LIMIT_WINDOW
        )

        # Aggregate token usage for analysis calls (generateContent). This is derived from
        # response metadata and does NOT trigger any extra API calls. Embedding tokens are
        # intentionally ignored.
        self._token_totals = {
            "prompt": 0,
            "output": 0,
            "total": 0,
        }

        self._warned_missing_usage_metadata = False
        
        logger.info(f"Initialized GeminiAnalyzer with model: {self.model_name}")

    def _pick_usage_value(self, usage, *names: str) -> int:
        for name in names:
            if isinstance(usage, dict) and name in usage and usage[name] is not None:
                try:
                    return int(usage[name])
                except Exception:
                    continue
            value = getattr(usage, name, None)
            if value is not None:
                try:
                    return int(value)
                except Exception:
                    continue
        return 0

    def _extract_token_counts(self, response) -> tuple[int, int, int]:
        usage = None
        if isinstance(response, dict):
            usage = response.get("usage_metadata") or response.get("usageMetadata")
        else:
            usage = getattr(response, "usage_metadata", None) or getattr(response, "usageMetadata", None)

        if usage is None and hasattr(response, "to_dict"):
            try:
                response_dict = response.to_dict()
                usage = response_dict.get("usage_metadata") or response_dict.get("usageMetadata")
            except Exception:
                usage = None

        if not usage:
            return 0, 0, 0

        prompt = self._pick_usage_value(
            usage,
            "prompt_token_count",
            "promptTokenCount",
            "prompt_tokens",
            "promptTokens",
        )
        output = self._pick_usage_value(
            usage,
            "candidates_token_count",
            "candidatesTokenCount",
            "output_token_count",
            "outputTokenCount",
            "completion_token_count",
            "completionTokenCount",
        )
        total = self._pick_usage_value(
            usage,
            "total_token_count",
            "totalTokenCount",
            "total_tokens",
            "totalTokens",
        )
        return prompt, output, total

    def _record_token_usage_from_response(self, response) -> None:
        prompt, output, total = self._extract_token_counts(response)
        if prompt == 0 and output == 0 and total == 0:
            if not self._warned_missing_usage_metadata:
                self._warned_missing_usage_metadata = True
                logger.warning(
                    "Gemini response did not include usage metadata; token totals will remain 0. "
                    "(This can happen with some models/preview versions or client response shapes.)"
                )
            return

        self._token_totals["prompt"] += int(prompt)
        self._token_totals["output"] += int(output)
        self._token_totals["total"] += int(total) if int(total) > 0 else (int(prompt) + int(output))

    def get_token_totals(self) -> Dict[str, int]:
        """Return cumulative token usage for Gemini analysis calls (excludes embeddings)."""
        return dict(self._token_totals)
    
    @retry(
        stop=stop_after_attempt(Config.MAX_RETRIES),
        wait=wait_exponential(
            multiplier=Config.INITIAL_RETRY_DELAY,
            max=Config.MAX_RETRY_DELAY
        ),
        retry=retry_if_exception_type(RateLimitError),
        before_sleep=lambda retry_state: logger.info(
            f"Rate limit retry attempt {retry_state.attempt_number}/{Config.MAX_RETRIES}. "
            f"Waiting {retry_state.next_action.sleep:.1f}s before retry..."
        )
    )
    def analyze_news(self, headline: str, content: str, url: str,
                    us_political_related: bool = False,
                    published_date: Optional[datetime] = None,
                    forexfactory_category: Optional[str] = None) -> Optional[Dict]:
        """
        Analyze a news article using Gemini AI.
        
        Args:
            headline: Article headline
            content: Full article content
            url: Article URL
            us_political_related: Whether this is US political news
        
        Returns:
            Dict containing structured analysis, or None on error
        """
        # Wait for rate limit if needed
        self.rate_limiter.wait_if_needed()

        published_date_str = published_date.isoformat() if published_date else None
        ff_category_str = forexfactory_category or None

        # Construct the user prompt with article data
        user_prompt = f"""
INPUT DATA:
{{
  "headline": "{self._escape_json(headline)}",
  "article_content": "{self._escape_json(content[:3000])}",
    "published_date": {json.dumps(published_date_str)},
    "forexfactory_category": {json.dumps(ff_category_str)},
  "url": "{url}",
  "us_political_related": {str(us_political_related).lower()}
}}

Analyze this forex news article and provide comprehensive market analysis in the JSON format specified.
Focus on forex market impact, affected instruments (including major crypto pairs), and trading implications.
"""
        
        try:
            logger.info(f"Analyzing: {headline[:100]}...")
            logger.info(f"Using Gemini model: {self.model_name}")
            
            # Generate analysis using new client API
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=[
                    {"role": "user", "parts": [{"text": self.SYSTEM_PROMPT}]},
                    {"role": "user", "parts": [{"text": user_prompt}]}
                ],
                config=self.generation_config
            )

            # Track token usage (no extra API calls)
            self._record_token_usage_from_response(response)

            prompt, output, total = self._extract_token_counts(response)
            if prompt or output or total:
                logger.info(
                    "Token usage - Prompt: %s, Response: %s, Total: %s",
                    prompt,
                    output,
                    total,
                )
            
            # Parse JSON response (even with structured output, still parse text)
            try:
                analysis_json = json.loads(response.text)
            except json.JSONDecodeError as e:
                logger.error(f"JSON parsing failed despite structured output: {e}")
                logger.error(f"Response text (first 500 chars): {response.text[:500]}")
                # If structured output fails, log and re-raise
                raise

            # Validate and normalize types (prevents null/invalid user-centric fields)
            analysis_model = NewsAnalysis.model_validate(analysis_json)
            analysis_json = analysis_model.model_dump()
            
            # With Pydantic schema, all required fields should be present, but validate anyway
            required_fields = [
                'forex_relevant', 'forex_instruments', 'primary_instrument',
                'importance_score', 'sentiment_score', 'analysis_confidence',
                'news_category', 'entities_mentioned', 'trading_sessions',
                'market_impact_prediction', 'impact_timeframe', 'volatility_expectation',
                'ai_analysis_summary', 'content_for_embedding',
                'human_takeaway', 'attention_score', 'news_state', 'market_pressure',
                'attention_window', 'confidence_label', 'expected_followups'
            ]
            
            missing_fields = [f for f in required_fields if f not in analysis_json]
            if missing_fields:
                logger.warning(f"Missing fields in analysis: {missing_fields}")
                # Fill with defaults
                for field in missing_fields:
                    analysis_json[field] = self._get_default_value(field)
            
            # Add the us_political_related flag to output
            analysis_json['us_political_related'] = self._detect_us_political(
                analysis_json, us_political_related
            )
            
            logger.info(f"Analysis complete. Forex relevant: {analysis_json['forex_relevant']}, "
                       f"Importance: {analysis_json['importance_score']}")
            
            return analysis_json
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON response: {e}")
            logger.debug(f"Response text: {response.text[:500]}")
            return None
        except Exception as e:
            error_msg = str(e).lower()
            error_type = type(e).__name__
            
            # Log full error details
            logger.error(f"Error analyzing news [{error_type}]: {e}")
            
            # Check for 404 errors (wrong model name) - fail fast
            if '404' in error_msg or 'not found' in error_msg or 'not_found' in error_msg:
                logger.error(f"Model not found error (404). Check model name: {self.model_name}")
                raise ValueError(f"Invalid model name: {self.model_name}. Error: {e}")
            
            # Check for rate limit errors (429) - retry with backoff
            if 'quota' in error_msg or 'rate' in error_msg or '429' in error_msg or 'resource exhausted' in error_msg:
                logger.warning(f"Rate limit hit. Will retry with exponential backoff...")
                raise RateLimitError(f"Rate limit exceeded: {e}")
            
            # For other errors, raise to trigger retry
            logger.error(f"Unexpected error, will retry: {e}")
            raise
    
    def analyze_with_rag(self, headline: str, content: str, url: str,
                         us_political_related: bool = False,
                         max_similar: int = 5,
                         published_date: Optional[datetime] = None,
                         forexfactory_category: Optional[str] = None) -> Optional[Dict]:
        """
        Analyze news with RAG (Retrieval-Augmented Generation) using vector similarity.
        
        This method:
        1. Generates a quick embedding from the headline
        2. Searches for similar historical articles
        3. Builds an enhanced prompt with historical context
        4. Analyzes with Gemini using the enriched context
        
        Args:
            headline: Article headline
            content: Full article content
            url: Article URL
            us_political_related: Whether this is US political news
            max_similar: Maximum number of similar articles to retrieve (default: 5)
        
        Returns:
            Dict containing structured analysis with similar_news_ids populated,
            or None on error
        """
        if not self.db_manager:
            logger.warning("No db_manager provided, falling back to basic analysis")
            return self.analyze_news(headline, content, url, us_political_related)
        
        # Wait for rate limit if needed
        self.rate_limiter.wait_if_needed()
        
        try:
            # Step 1: Generate embedding for similarity search
            logger.info(f"Generating embedding for similarity search: {headline[:80]}...")
            search_text = f"{headline} {content[:500]}"  # Use headline + start of content
            query_embedding = self.generate_embedding(search_text)
            
            if not query_embedding:
                logger.warning("Failed to generate embedding, falling back to basic analysis")
                return self.analyze_news(headline, content, url, us_political_related)
            
            # Step 2: Search for similar articles
            logger.info("Searching for similar historical articles...")
            similar_articles = self.db_manager.search_similar_news(
                embedding=query_embedding,
                limit=max_similar,
                similarity_threshold=0.7  # Cosine similarity threshold
            )
            
            if similar_articles:
                logger.info(f"Found {len(similar_articles)} similar articles")
            else:
                logger.info("No similar articles found, proceeding without historical context")

            published_date_str = published_date.isoformat() if published_date else None
            ff_category_str = forexfactory_category or None

            # Step 3: Build enhanced prompt with historical context
            if similar_articles:
                historical_context = self._build_historical_context(similar_articles)
                user_prompt = f"""
INPUT DATA:
{{
  "headline": "{self._escape_json(headline)}",
  "article_content": "{self._escape_json(content[:3000])}",
    "published_date": {json.dumps(published_date_str)},
    "forexfactory_category": {json.dumps(ff_category_str)},
  "url": "{url}",
  "us_political_related": {str(us_political_related).lower()}
}}

HISTORICAL CONTEXT - Similar Articles Found:
{historical_context}

Based on the historical patterns above, analyze this forex news article and provide comprehensive market analysis in the JSON format specified.
Focus on forex market impact, affected instruments (including major crypto pairs), and trading implications.
Reference how similar historical events affected markets in your analysis.
"""
            else:
                # No similar articles, use basic prompt
                user_prompt = f"""
INPUT DATA:
{{
  "headline": "{self._escape_json(headline)}",
  "article_content": "{self._escape_json(content[:3000])}",
    "published_date": {json.dumps(published_date_str)},
    "forexfactory_category": {json.dumps(ff_category_str)},
  "url": "{url}",
  "us_political_related": {str(us_political_related).lower()}
}}

Analyze this forex news article and provide comprehensive market analysis in the JSON format specified.
Focus on forex market impact, affected instruments (including major crypto pairs), and trading implications.
"""
            
            # Step 4: Generate analysis with context
            logger.info(f"Analyzing with {'RAG context' if similar_articles else 'basic analysis'}: {headline[:100]}...")
            
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=[
                    {"role": "user", "parts": [{"text": self.SYSTEM_PROMPT}]},
                    {"role": "user", "parts": [{"text": user_prompt}]}
                ],
                config=self.generation_config
            )

            # Track token usage (no extra API calls)
            self._record_token_usage_from_response(response)

            prompt, output, total = self._extract_token_counts(response)
            if prompt or output or total:
                logger.info(
                    "Token usage - Prompt: %s, Response: %s, Total: %s",
                    prompt,
                    output,
                    total,
                )
            
            # Parse JSON response
            analysis_json = json.loads(response.text)

            # Validate and normalize types
            analysis_model = NewsAnalysis.model_validate(analysis_json)
            analysis_json = analysis_model.model_dump()
            
            # Validate required fields
            required_fields = [
                'forex_relevant', 'forex_instruments', 'primary_instrument',
                'importance_score', 'sentiment_score', 'analysis_confidence',
                'news_category', 'entities_mentioned', 'trading_sessions',
                'market_impact_prediction', 'impact_timeframe', 'volatility_expectation',
                'ai_analysis_summary', 'content_for_embedding',
                'human_takeaway', 'attention_score', 'news_state', 'market_pressure',
                'attention_window', 'confidence_label', 'expected_followups'
            ]
            
            missing_fields = [f for f in required_fields if f not in analysis_json]
            if missing_fields:
                logger.warning(f"Missing fields in analysis: {missing_fields}")
                for field in missing_fields:
                    analysis_json[field] = self._get_default_value(field)
            
            # Add similar news IDs if found
            if similar_articles:
                analysis_json['similar_news_ids'] = [art['email_id'] for art in similar_articles]
                if not analysis_json.get('similar_news_context'):
                    analysis_json['similar_news_context'] = f"Analysis informed by {len(similar_articles)} similar historical articles"
            else:
                analysis_json['similar_news_ids'] = []
                if not analysis_json.get('similar_news_context'):
                    analysis_json['similar_news_context'] = ""
            
            # Add the us_political_related flag to output
            analysis_json['us_political_related'] = self._detect_us_political(
                analysis_json, us_political_related
            )
            
            logger.info(f"RAG Analysis complete. Forex relevant: {analysis_json['forex_relevant']}, "
                       f"Importance: {analysis_json['importance_score']}, "
                       f"Similar articles used: {len(similar_articles) if similar_articles else 0}")
            
            return analysis_json
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON response: {e}")
            return None
        except Exception as e:
            error_msg = str(e).lower()
            if 'quota' in error_msg or 'rate' in error_msg or '429' in error_msg:
                logger.error(f"Rate limit hit: {e}")
                raise RateLimitError(f"Rate limit exceeded: {e}")
            else:
                logger.error(f"Error in RAG analysis: {e}")
                raise
    
    def _build_historical_context(self, similar_articles: list) -> str:
        """
        Build historical context string from similar articles.
        
        Args:
            similar_articles: List of similar article dicts
        
        Returns:
            Formatted string with historical context
        """
        context_lines = []
        
        for idx, article in enumerate(similar_articles, 1):
            headline = article.get('headline', 'Unknown headline')
            published_at = article.get('email_received_at') or article.get('created_at') or 'Unknown date'
            analysis = article.get('ai_analysis_summary', '')
            instruments = article.get('forex_instruments', [])
            importance = article.get('importance_score', 'N/A')
            sentiment = article.get('sentiment_score', 0.0)
            
            # Format date (prefer published date)
            date_str = published_at.strftime('%Y-%m-%d %H:%M UTC') if hasattr(published_at, 'strftime') else str(published_at)
            
            # Format instruments
            instruments_str = ', '.join(instruments) if instruments else 'N/A'
            
            # Build context entry
            context_entry = f"""
{idx}. [{date_str}] {headline}
   - Instruments affected: {instruments_str}
   - Importance: {importance}/5, Sentiment: {sentiment:.2f}
   - Analysis summary: {analysis[:200]}...
"""
            context_lines.append(context_entry)
        
        return "\n".join(context_lines)
    
    def _detect_us_political(self, analysis: Dict, initial_flag: bool) -> bool:
        """
        Detect if news is US political related based on analysis.
        
        Args:
            analysis: The AI analysis dict
            initial_flag: Initial flag value
        
        Returns:
            bool: Whether news is US political related
        """
        if initial_flag:
            return True
        
        # Check entities and category
        entities_str = ' '.join(analysis.get('entities_mentioned', [])).lower()
        category = analysis.get('news_category', '').lower()
        summary = analysis.get('ai_analysis_summary', '').lower()
        
        us_keywords = [
            'trump', 'biden', 'white house', 'congress', 'senate',
            'federal reserve', 'fed', 'us president', 'administration',
            'us government', 'washington'
        ]
        
        return (category == 'political' or
                any(keyword in entities_str for keyword in us_keywords) or
                any(keyword in summary for keyword in us_keywords))
    
    @retry(
        stop=stop_after_attempt(Config.MAX_RETRIES),
        wait=wait_exponential(
            multiplier=Config.INITIAL_RETRY_DELAY,
            max=Config.MAX_RETRY_DELAY
        ),
        retry=retry_if_exception_type((RateLimitError, Exception))
    )
    def generate_embedding(self, text: str) -> Optional[List[float]]:
        """
        Generate embedding vector for text using Gemini embedding model.
        
        Args:
            text: Text to embed
        
        Returns:
            List of floats (768 dimensions), or None on error
        """
        # Wait for rate limit if needed
        self.rate_limiter.wait_if_needed()
        
        try:
            # Truncate text if too long (Gemini has input limits)
            if len(text) > 10000:
                text = text[:10000]
            
            # Using vertexai explicit SDK implementation
            model = TextEmbeddingModel.from_pretrained("gemini-embedding-001")
            text_input = TextEmbeddingInput(text, "RETRIEVAL_DOCUMENT")
            result = model.get_embeddings(
                [text_input],
                output_dimensionality=3072
            )
            
            embedding = result[0].values
            logger.info(f"Generated embedding of dimension {len(embedding)}")
            return embedding
            
        except Exception as e:
            error_msg = str(e).lower()
            if 'quota' in error_msg or 'rate' in error_msg or '429' in error_msg:
                logger.error(f"Rate limit hit during embedding: {e}")
                raise RateLimitError(f"Rate limit exceeded: {e}")
            else:
                logger.error(f"Error generating embedding: {e}")
                raise
    
    def _escape_json(self, text: str) -> str:
        """Escape text for JSON string"""
        if not text:
            return ""
        return (text
                .replace('\\', '\\\\')
                .replace('"', '\\"')
                .replace('\n', '\\n')
                .replace('\r', '\\r')
                .replace('\t', '\\t'))
    
    def _get_default_value(self, field: str):
        """Get default value for a missing field"""
        defaults = {
            'forex_relevant': False,
            'forex_instruments': [],
            'primary_instrument': 'DXY',
            'importance_score': 3,
            'sentiment_score': 0.0,
            'analysis_confidence': 0.5,
            'news_category': 'other',
            'entities_mentioned': [],
            'trading_sessions': [],
            'similar_news_ids': [],
            'market_impact_prediction': 'neutral',
            'impact_timeframe': 'intraday',
            'volatility_expectation': 'medium',
            'content_source': 'forexfactory',
            'ai_analysis_summary': 'Analysis not available',
            'similar_news_context': '',
            'content_for_embedding': '',

            # User-centric UI defaults
            'human_takeaway': '',
            'attention_score': 50,
            'news_state': 'fresh',
            'market_pressure': 'neutral',
            'attention_window': 'hours',
            'confidence_label': 'medium',
            'expected_followups': []
        }
        return defaults.get(field)
    
    def test_connection(self) -> bool:
        """Test if Gemini API is accessible"""
        try:
            models = list(self.client.models.list())
            logger.info(f"Successfully connected to Gemini API. Available models: {len(models)}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Gemini API: {e}")
            return False
