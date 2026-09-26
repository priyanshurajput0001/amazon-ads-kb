---
id: amazon-ads-api-overview
title: Amazon Ads API Developer Guides Overview
sources:
  - https://advertising.amazon.com/API/docs/en-us/guides/overview
confidence: high
status: official
last_checked: 2026-09-26
---

# Amazon Ads API Developer Guides Overview

The Amazon Ads API (formerly the Amazon Advertising API) is a set of advanced
tools that lets developers programmatically create and manage advertising
campaigns and retrieve performance data across Amazon's ad products. The
developer guides are the official entry point covering onboarding, reporting,
campaign management, and per-product documentation for Sponsored Products,
Sponsored Brands, Sponsored Display, Sponsored TV, and Amazon DSP.

## Details

### Onboarding (prerequisite for all tools)
- Before using any developer tool, you must complete the onboarding process:
  register an application and apply for access to the API.
- Guide: https://advertising.amazon.com/API/docs/en-us/guides/onboarding/overview

### Reporting & measurement
Two mechanisms exist for accessing campaign performance metrics:
- **Amazon Ads API** — supports asynchronous report requests.
  Guide: https://advertising.amazon.com/API/docs/en-us/guides/reporting/v3/get-started
- **Amazon Marketing Stream** — provides near real-time access to metrics
  delivered through your AWS account.
  Guide: https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-stream/onboarding

### Campaign management
- A new set of campaign management APIs lets you manage campaigns across ad
  products using a common model and a single set of API endpoints (referred to
  on the page as "campaign management in the Amazon Ads API v1").
  Guide: https://advertising.amazon.com/API/docs/en-us/guides/campaign-management/overview
- The API can also create and manage sponsored ads and Amazon DSP campaigns:
  - Sponsored Products:
    https://advertising.amazon.com/API/docs/en-us/guides/sponsored-products/get-started/manual-campaigns
  - Sponsored Brands:
    https://advertising.amazon.com/API/docs/en-us/guides/sponsored-brands/campaigns/get-started-with-campaigns
  - Sponsored Display (Amazon sellers or vendors):
    https://advertising.amazon.com/API/docs/en-us/guides/sponsored-display/contextual-targeting
  - Sponsored Display (advertisers that do not sell on Amazon):
    https://advertising.amazon.com/API/docs/en-us/guides/sponsored-display/non-amazon-sellers/get-started

### Guide topic areas (site navigation)
The developer guides cover these areas: onboarding, account management,
reporting, campaign management, conversions, exports, Sponsored Products,
Sponsored Brands, Sponsored Display, Sponsored TV, Amazon DSP, Inventory
Management, Amazon Marketing Cloud, Amazon Marketing Stream, Ads data manager,
Retail Ad Service, traffic events, Posts, Assets, Moderation, Portfolios,
Brand Store, Rules, budget usage, recommendations & insights, Amazon
Attribution (beta), billing & invoices, Ad library, Media Planning, 3PAS,
Postman, and translations.

### Ecosystem & references
- Full API reference: https://advertising.amazon.com/API/docs/en-us/reference/api-overview
- API best practices: https://advertising.amazon.com/API/docs/en-us/reference/concepts/overview
- Release notes: https://advertising.amazon.com/API/docs/en-us/release-notes/index
- Support: https://advertising.amazon.com/API/docs/en-us/support/overview
- The developer guides are maintained in a public repository:
  https://github.com/amzn/ads-advanced-tools-docs
- Release-notes RSS feed:
  https://d3a0d0y2hgofx6.cloudfront.net/rss/en-us/ad-api-rss.xml
- Translations of the developer guides are available in select languages (see
  the Translations guide).

## Sources
- https://advertising.amazon.com/API/docs/en-us/guides/overview —
  confirmed (fetched 2026-09-26 via Tavily, advanced extraction): what the
  developer tools provide; the onboarding prerequisite; asynchronous reporting
  via the Ads API vs. near real-time via Amazon Marketing Stream; the common
  campaign-management API model; per-product campaign creation entry points;
  the full list of guide topic areas; links to API reference, best practices,
  release notes, the GitHub docs repository, and the RSS feed.
