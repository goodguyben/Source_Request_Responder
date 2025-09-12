#!/usr/bin/env python3
"""
Test script for Gemini-based HARO query filtering.
"""

import os
import sys
sys.path.append('.')

from gemini_filter import should_include_query_gemini, analyze_query_with_gemini
from dotenv import load_dotenv

load_dotenv()

def test_query_analysis():
    """Test Gemini analysis with sample HARO queries."""
    
    test_queries = [
        {
            "summary": "AI-powered marketing automation for small businesses",
            "category": "Technology",
            "query": "Looking for experts who can discuss how AI is transforming marketing automation for small businesses. Need insights on tools, implementation challenges, and ROI.",
            "expected": True
        },
        {
            "summary": "Wedding planning tips for budget-conscious couples",
            "category": "Lifestyle",
            "query": "Need advice on planning a wedding on a budget. Looking for tips on venues, catering, and decorations.",
            "expected": False
        },
        {
            "summary": "Website redesign for e-commerce platform",
            "category": "General",
            "query": "Seeking web developers and UX designers who have experience redesigning e-commerce websites. Need insights on conversion optimization and mobile responsiveness.",
            "expected": True
        },
        {
            "summary": "Hockey player romance novels cultural analysis",
            "category": "Entertainment",
            "query": "Looking for cultural commentators to discuss the appeal of hockey player romance novels and what it says about modern culture.",
            "expected": False
        },
        {
            "summary": "SaaS platform integration challenges",
            "category": "Business",
            "query": "Need insights from SaaS experts about common integration challenges businesses face when adopting new software platforms.",
            "expected": True
        }
    ]
    
    print("🧠 Testing Gemini-powered HARO Query Analysis\n")
    print("=" * 60)
    
    for i, test in enumerate(test_queries, 1):
        print(f"\nTest {i}: {test['summary']}")
        print(f"Expected: {'✅ Include' if test['expected'] else '❌ Exclude'}")
        print("-" * 40)
        
        try:
            is_relevant, analysis = should_include_query_gemini(
                test['query'], 
                test['summary'], 
                test['category']
            )
            
            print(f"Gemini Decision: {'✅ Include' if is_relevant else '❌ Exclude'}")
            print(f"Relevance Score: {analysis['relevance_score']:.2f}")
            print(f"Confidence: {analysis['confidence']:.2f}")
            print(f"Matching Topics: {', '.join(analysis['matching_topics'])}")
            print(f"Reasoning: {analysis['reasoning']}")
            
            # Check if decision matches expectation
            if is_relevant == test['expected']:
                print("✅ CORRECT")
            else:
                print("❌ INCORRECT")
                
        except Exception as e:
            print(f"❌ ERROR: {e}")
        
        print("-" * 40)

if __name__ == "__main__":
    if not os.getenv("GEMINI_API_KEY"):
        print("❌ GEMINI_API_KEY not found in environment variables")
        sys.exit(1)
    
    test_query_analysis()
