#!/bin/bash
# Quick start script for news analyzer

set -e

echo "========================================="
echo "News Analyzer - Quick Start"
echo "========================================="
echo ""

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Step 1: Check if migration is needed
echo -e "${YELLOW}Step 1: Checking database migration...${NC}"
MIGRATION_NEEDED=$(docker exec n8n-postgres psql -U Priyo13o4 -d ai_trading_bot_data -tAc \
  "SELECT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_name='email_news_analysis' AND column_name='trump_related');" 2>/dev/null || echo "t")

if [ "$MIGRATION_NEEDED" = "t" ]; then
    echo -e "${YELLOW}Migration needed: Renaming trump_related → us_political_related${NC}"
    docker exec -i n8n-postgres psql -U Priyo13o4 -d ai_trading_bot_data < migration_rename_trump_to_us_political.sql
    echo -e "${GREEN}✓ Migration completed${NC}"
else
    echo -e "${GREEN}✓ Database already migrated${NC}"
fi

echo ""

# Step 2: Build container
echo -e "${YELLOW}Step 2: Building news-analyzer container...${NC}"
docker-compose build news-analyzer
echo -e "${GREEN}✓ Build completed${NC}"

echo ""

# Step 3: Run model comparison (optional)
echo -e "${YELLOW}Step 3: Model Comparison Test (optional)${NC}"
read -p "Do you want to run model comparison test? (y/N): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "Running model comparison on 5 articles..."
    docker-compose run --rm news-analyzer python test_models.py --articles 5
    echo -e "${GREEN}✓ Model comparison completed${NC}"
    echo "Check model_comparison_report_*.json for results"
else
    echo "Skipped model comparison"
fi

echo ""

# Step 4: Check database stats
echo -e "${YELLOW}Step 4: Database Statistics${NC}"
docker exec n8n-postgres psql -U Priyo13o4 -d ai_trading_bot_data -c \
  "SELECT 
      COUNT(*) as total_articles,
      COUNT(CASE WHEN ai_analysis_summary IS NOT NULL THEN 1 END) as analyzed,
      COUNT(CASE WHEN ai_analysis_summary IS NULL THEN 1 END) as unanalyzed
   FROM email_news_analysis 
   WHERE forexfactory_content_id IS NOT NULL;"

echo ""

# Step 5: Run options
echo -e "${YELLOW}Step 5: Run Backfill${NC}"
echo "Choose an option:"
echo "  1) Process ALL unanalyzed articles (recommended)"
echo "  2) Process first 50 articles (test run)"
echo "  3) Process first 10 articles (quick test)"
echo "  4) Skip for now"
echo ""
read -p "Enter choice [1-4]: " choice

case $choice in
  1)
    echo -e "${GREEN}Starting full backfill...${NC}"
    docker-compose run --rm news-analyzer python main.py
    ;;
  2)
    echo -e "${GREEN}Processing 50 articles...${NC}"
    docker-compose run --rm news-analyzer python main.py --limit 50
    ;;
  3)
    echo -e "${GREEN}Processing 10 articles (quick test)...${NC}"
    docker-compose run --rm news-analyzer python main.py --limit 10
    ;;
  4)
    echo "Skipped backfill. You can run it later with:"
    echo "  docker-compose run --rm news-analyzer python main.py"
    ;;
  *)
    echo -e "${RED}Invalid choice${NC}"
    ;;
esac

echo ""
echo -e "${GREEN}=========================================${NC}"
echo -e "${GREEN}Setup Complete!${NC}"
echo -e "${GREEN}=========================================${NC}"
echo ""
echo "Useful commands:"
echo "  - View logs: docker logs tradingbot-news-analyzer"
echo "  - Run backfill: docker-compose run --rm news-analyzer python main.py"
echo "  - Test models: docker-compose run --rm news-analyzer python test_models.py"
echo "  - Check stats: docker exec n8n-postgres psql -U Priyo13o4 -d ai_trading_bot_data -c 'SELECT COUNT(*) FROM email_news_analysis WHERE ai_analysis_summary IS NOT NULL;'"
echo ""
