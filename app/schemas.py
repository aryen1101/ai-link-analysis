"""Pydantic models for the API request/response and the LLM structured output.

The ``LLMAnalysis`` model is passed to OpenAI Structured Outputs, so every field
is required and only JSON-schema-friendly types are used (no dicts / defaults).
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Platform = Literal["Google", "Facebook", "Instagram", "LinkedIn", "X"]
Source = Literal["website_content", "inferred"]


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------
class LinkAnalysisRequest(BaseModel):
    url: str = Field(..., min_length=4, max_length=2048, examples=["https://stripe.com"])

    @field_validator("url")
    @classmethod
    def strip(cls, v: str) -> str:
        return v.strip()


# ---------------------------------------------------------------------------
# LLM structured output
# ---------------------------------------------------------------------------
class Brand(BaseModel):
    name: str
    category: str = Field(description="Main business category")
    summary: str = Field(description="2-3 sentence business overview grounded in the website")


class Competitor(BaseModel):
    name: str
    website: str = Field(description="Competitor homepage URL if known, else empty string")
    target_market: str
    main_service: str
    strengths: list[str]
    weaknesses_or_gaps: list[str]
    source: Literal["ai_suggestion"] = Field(
        description="Always 'ai_suggestion': competitors come from model knowledge, not live research"
    )


class ComparisonRow(BaseModel):
    aspect: str = Field(description="e.g. Main Service, Target Market, Key Strength, Opportunity")
    your_brand: str
    competitors: list[str] = Field(description="One value per competitor, same order as competitors list")


class CompetitorAnalysis(BaseModel):
    brand_overview: str
    main_category: str
    main_products_services: list[str]
    competitors: list[Competitor]
    comparison_table: list[ComparisonRow]
    competitive_advantages: list[str]
    market_positioning_opportunities: list[str]


class SearchQuery(BaseModel):
    query: str
    platforms: list[Platform]
    intent: str
    business_relevance: str
    target_audience: str


class KeywordRecommendations(BaseModel):
    primary_keyword: str
    secondary_keywords: list[str]
    long_tail_keywords: list[str]
    business_keywords: list[str]


class SupportingContent(BaseModel):
    type: Literal[
        "service_landing_page",
        "industry_page",
        "faq",
        "blog_topic",
        "location_page",
        "business_article",
    ]
    title: str
    reason: str


class SEOImprovements(BaseModel):
    current_title: str = Field(description="Title found on the website, or empty string")
    current_meta_description: str = Field(description="Meta description found, or empty string")
    keywords: KeywordRecommendations
    recommended_title: str = Field(description="50-60 characters, includes primary keyword")
    title_structure: str = Field(description="Explain the title pattern, e.g. 'Primary Keyword | Benefit | Brand'")
    meta_description: str = Field(description="140-160 characters, human friendly, includes keywords")
    issues_found: list[str] = Field(description="Concrete on-page SEO issues observed in the extracted data")
    supporting_content: list[SupportingContent]


class Service(BaseModel):
    service_name: str
    description: str
    target_industry: str
    target_customer: str
    main_benefit: str
    source: Source = Field(description="'website_content' if explicitly on the site, else 'inferred'")


class AudienceSegment(BaseModel):
    audience: str
    business_type: str
    requirement: str


class TargetAudience(BaseModel):
    primary_audience: list[str]
    secondary_audience: list[str]
    industries: list[str]
    business_types: list[str]
    customer_profiles: list[str]
    geographic_focus: str = Field(description="Location focus if evident, else 'Not specified on website'")
    customer_needs_and_challenges: list[str]
    customer_interests: list[str]
    recommended_marketing_segments: list[AudienceSegment]


class LLMAnalysis(BaseModel):
    brand: Brand
    competitor_analysis: CompetitorAnalysis
    target_search_queries: list[SearchQuery]
    seo_improvements: SEOImprovements
    top_services: list[Service]
    target_audience: TargetAudience


# ---------------------------------------------------------------------------
# API response
# ---------------------------------------------------------------------------
class Analysis(BaseModel):
    competitor_analysis: CompetitorAnalysis
    target_search_queries: list[SearchQuery]
    seo_improvements: SEOImprovements
    top_services: list[Service]
    target_audience: TargetAudience


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float | None = None


class AnalysisMeta(BaseModel):
    model: str
    pages_analyzed: list[str]
    content_chars_sent: int
    cached: bool
    processing_time_ms: int
    token_usage: TokenUsage
    notes: list[str]


class LinkAnalysisResponse(BaseModel):
    success: Literal[True] = True
    website: str
    brand: Brand
    analysis: Analysis
    meta: AnalysisMeta


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    success: Literal[False] = False
    website: str | None = None
    error: ErrorDetail
