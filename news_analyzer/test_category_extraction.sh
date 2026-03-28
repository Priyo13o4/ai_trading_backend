#!/bin/bash
# Quick test script to verify category extraction for all 3 URLs

echo "Testing ForexFactory Category Extraction"
echo "========================================"

URLS=(
    "https://www.forexfactory.com/news/1377736"
    "https://www.forexfactory.com/news/1377601"
    "https://www.forexfactory.com/news/1377504"
)

for url in "${URLS[@]}"; do
    echo ""
    echo "Testing: $url"
    echo "----------------------------------------"
    
    result=$(curl -s -X POST "http://localhost:8000/api/v1/scrape" \
      -H "Content-Type: application/json" \
      -d "{
        \"url\": \"$url\",
        \"force_selenium\": true,
        \"auto_detect_js\": true,
        \"output_format\": \"all\"
      }")
    
    # Parse JSON and display results
    echo "$result" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(f'✓ Success: {data.get(\"success\")}')
    print(f'✓ Method: {data.get(\"method\")}')
    category = data.get('sections', {}).get('metadata', {}).get('category', 'NOT FOUND')
    # Clean up extra spaces in category
    import re
    category = re.sub(r'\s+', ' ', category.strip()) if category != 'NOT FOUND' else category
    print(f'✓ Category: {category}')
    print(f'✓ Word count: {data.get(\"stats\", {}).get(\"word_count\", 0)}')
    print(f'✓ Status code: {data.get(\"meta\", {}).get(\"status_code\", \"unknown\")}')
except Exception as e:
    print(f'✗ Error: {e}')
"
    
    echo ""
done

echo "========================================"
echo "All tests completed!"
