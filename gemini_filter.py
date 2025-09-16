"""
Gemini-powered HARO query filtering system.
Uses AI to intelligently analyze queries for relevance to specified topics.
"""

from dotenv import load_dotenv
load_dotenv()

import os
import json
import logging
from typing import Dict, List, Optional, Tuple
import google.generativeai as genai

logger = logging.getLogger("gemini_filter")

# Configuration
USE_GEMINI_FILTERING = os.getenv("USE_GEMINI_FILTERING", "true").lower() == "true"
GEMINI_FILTER_MODEL = os.getenv("GEMINI_FILTER_MODEL", "gemini-2.5-flash")
GEMINI_FILTER_THRESHOLD = float(os.getenv("GEMINI_FILTER_THRESHOLD", "0.85"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_CONFIDENCE_THRESHOLD = float(os.getenv("GEMINI_CONFIDENCE_THRESHOLD", "0.75"))

# Topic definitions for Gemini analysis
TOPIC_DEFINITIONS = {
    "artificial_intelligence": {
        "name": "Artificial Intelligence & Machine Learning",
        "description": "AI, machine learning, automation, chatbots, neural networks, deep learning, predictive analytics, generative AI, intelligent systems, cognitive computing, smart technology, robotics",
        "keywords": ["ai", "artificial intelligence", "machine learning", "automation", "chatbot", "neural networks", "deep learning", "predictive analytics", "generative ai", "intelligent systems", "cognitive computing", "smart technology", "robotics", "automated", "ml", "algorithm"]
    },
    "web_development": {
        "name": "Web Development & Design",
        "description": "Website development, front-end/back-end development, UX/UI design, responsive design, web applications, APIs, CMS, e-commerce, web optimization, accessibility, graphic design, digital design",
        "keywords": ["web design", "website development", "frontend", "backend", "fullstack", "ux design", "ui design", "responsive design", "web app", "api", "cms", "ecommerce", "wordpress", "web optimization", "accessibility", "graphic design"]
    },
    "digital_marketing": {
        "name": "Digital Marketing & SEO",
        "description": "Digital marketing, social media marketing, SEO, content marketing, email marketing, PPC, influencer marketing, affiliate marketing, marketing strategy, marketing automation, brand strategy, marketing analytics",
        "keywords": ["digital marketing", "social media marketing", "seo", "content marketing", "email marketing", "ppc", "influencer marketing", "affiliate marketing", "marketing strategy", "marketing automation", "brand strategy", "marketing analytics", "online advertising", "social media strategy"]
    },
    "business_technology": {
        "name": "Business Technology & Software",
        "description": "SaaS, cloud computing, business software, enterprise solutions, data analytics, business intelligence, workflow automation, integration, scalability, digital transformation, business automation",
        "keywords": ["saas", "cloud computing", "business software", "enterprise", "data analytics", "business intelligence", "workflow automation", "integration", "scalability", "digital transformation", "business automation", "software", "platform", "system"]
    },
    "mobile_technology": {
        "name": "Mobile Technology & Apps",
        "description": "Mobile app development, smartphone apps, mobile optimization, iOS, Android, responsive design, mobile marketing, app store optimization, mobile user experience",
        "keywords": ["mobile app", "smartphone", "ios", "android", "mobile development", "mobile optimization", "responsive", "app store", "mobile marketing", "mobile ux"]
    }
}

def create_gemini_filter_prompt(query_text: str, summary: str = "", category: str = "") -> str:
    """Create a prompt for Gemini to analyze HARO query relevance."""
    
    topics_text = "\n".join([
        f"- {topic['name']}: {topic['description']}"
        for topic in TOPIC_DEFINITIONS.values()
    ])
    
    prompt = f"""
You are an expert at analyzing media queries to determine their relevance to specific business and technology topics.

ANALYZE THIS HARO QUERY:
Summary: {summary}
Category: {category}
Query Text: {query_text}

RELEVANT TOPICS TO CONSIDER:
{topics_text}

TASK:
Determine if this query is relevant to any of the above topics. Consider:
1. Direct mentions of technologies, services, or concepts
2. Implicit connections (e.g., "business growth" might relate to marketing)
3. Industry context (startup, enterprise, small business needs)
4. Professional expertise areas (development, marketing, strategy)

RESPOND WITH JSON ONLY:
{{
    "relevant": true/false,
    "relevance_score": 0.0-1.0,
    "matching_topics": ["topic1", "topic2"],
    "reasoning": "Brief explanation of why this query is/isn't relevant",
    "confidence": 0.0-1.0
}}

Guidelines:
- Be strict about relevance; prefer precision over recall. If unsure, set relevant=false.
- Do not infer relevance from generic business language. Require explicit topical signals (direct mentions) or two strong implicit signals.
- Scoring: 0.85–1.0 = clearly relevant; 0.65–0.84 = borderline; <0.65 = not relevant.
- Confidence must reflect evidence. Lower confidence when signals are weak, ambiguous, or generic.
- Only include topics that genuinely match. If none match, use an empty array.
"""
    
    return prompt.strip()

def analyze_query_with_gemini(query_text: str, summary: str = "", category: str = "") -> Dict:
    """Use Gemini to analyze HARO query relevance with automatic fallback."""
    
    if not USE_GEMINI_FILTERING or not GEMINI_API_KEY:
        logger.warning("Gemini filtering disabled or API key missing")
        return {
            "relevant": False,
            "relevance_score": 0.0,
            "matching_topics": [],
            "reasoning": "Gemini filtering disabled",
            "confidence": 0.0
        }
    
    # Try primary model first, then fallback to secondary model
    models_to_try = [GEMINI_FILTER_MODEL, "gemini-1.5-flash"]
    
    for model_name in models_to_try:
        try:
            # Configure Gemini
            genai.configure(api_key=GEMINI_API_KEY)
            model = genai.GenerativeModel(model_name)
            
            # Create prompt
            prompt = create_gemini_filter_prompt(query_text, summary, category)
            
            logger.info(f"Analyzing query with Gemini ({model_name}): {summary[:100]}...")
            
            # Generate response
            response = model.generate_content(prompt)
            response_text = response.text.strip()
            
            # Parse JSON response
            try:
                # Clean up response text (remove markdown formatting if present)
                if response_text.startswith("```json"):
                    response_text = response_text.replace("```json", "").replace("```", "").strip()
                elif response_text.startswith("```"):
                    response_text = response_text.replace("```", "").strip()
                
                result = json.loads(response_text)
                
                # Validate response structure
                required_fields = ["relevant", "relevance_score", "matching_topics", "reasoning", "confidence"]
                if not all(field in result for field in required_fields):
                    logger.error(f"Invalid Gemini response structure from {model_name}")
                    continue  # Try next model
                
                logger.info(f"Gemini analysis ({model_name}): Relevant={result['relevant']}, Score={result['relevance_score']:.2f}, Topics={result['matching_topics']}")
                return result
                
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse Gemini JSON response from {model_name}: {e}")
                logger.error(f"Raw response: {response_text}")
                continue  # Try next model
                
        except Exception as e:
            error_msg = str(e)
            if "quota" in error_msg.lower() or "429" in error_msg:
                logger.warning(f"Quota exceeded for {model_name}, trying fallback model...")
                continue  # Try next model
            else:
                logger.exception(f"Error in Gemini query analysis with {model_name}: {e}")
                continue  # Try next model
    
    # If all models failed
    logger.error("All Gemini models failed, using fallback result")
    return create_fallback_result()

def create_fallback_result() -> Dict:
    """Create a fallback result when Gemini analysis fails."""
    return {
        "relevant": False,
        "relevance_score": 0.0,
        "matching_topics": [],
        "reasoning": "Analysis failed, defaulting to exclude",
        "confidence": 0.0
    }

def should_include_query_gemini(query_text: str, summary: str = "", category: str = "") -> Tuple[bool, Dict]:
    """Use Gemini to determine if a HARO query should be included."""
    
    analysis = analyze_query_with_gemini(query_text, summary, category)
    
    # Decision logic
    # Require at least one matching topic, higher relevance and confidence
    is_relevant = (
        analysis["relevant"]
        and analysis["relevance_score"] >= GEMINI_FILTER_THRESHOLD
        and analysis["confidence"] >= GEMINI_CONFIDENCE_THRESHOLD
        and isinstance(analysis.get("matching_topics"), list)
        and len(analysis.get("matching_topics")) > 0
    )
    
    return is_relevant, analysis
