#!/usr/bin/env python3
from google import genai
import os

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# Get all models
models = list(client.models.list())
print("=== ALL AVAILABLE MODELS ===\n")

# Group by capability
text_models = []
vision_models = []

for model in models:
    name = model.name
    # Check if it's a vision model by name
    if any(x in name.lower() for x in ['vision', 'image']):
        vision_models.append(name)
    else:
        text_models.append(name)

print("TEXT GENERATION MODELS (sorted by name):")
for name in sorted(text_models):
    print(f"  • {name}")

print(f"\nVISION MODELS (sorted by name):")
for name in sorted(vision_models):
    print(f"  • {name}")

print(f"\n=== SUMMARY ===")
print(f"Total text models: {len(text_models)}")
print(f"Total vision models: {len(vision_models)}")
print(f"Total: {len(text_models) + len(vision_models)}")

# Check for specific models
print("\n=== CHECKING FOR SPECIFIC MODELS ===")
all_names = [m.name for m in models]

candidates = ['gemini-2.5-flash', 'gemini-2.5-pro', 'gemini-3-flash', 'gemini-3-pro', 'gemini-exp-1206', 'gemini-exp-1202']
for candidate in candidates:
    found = [m for m in models if candidate in m.name.lower()]
    if found:
        print(f"✓ {candidate}: {found[0].name}")
    else:
        print(f"✗ {candidate}: NOT FOUND")
