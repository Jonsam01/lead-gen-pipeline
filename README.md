# SMB Lead Generation Pipeline

A production automation pipeline that sources, enriches, and delivers qualified prospect leads for SMBs without manual effort — scheduled to run daily across multiple business niches.

## What It Does

- Scrapes business listings via Apify's Google Maps scraper, targeting SMBs that show no signs of existing automation
- Filters and scores leads based on pain-point signals (missing website features, weak online presence, etc.)
- Deduplicates against previously delivered leads using Supabase
- Delivers fresh leads to Telegram and a Google Sheets campaign queue
- Runs automatically every day across four business niches via GitHub Actions — no manual triggering required

## How It Works

1. **Source** — Apify's Google Maps scraper pulls business listings matching target niches (auto repair, veterinary, removalists, and others)
2. **Check** — Python script checks each business for automation signatures (chatbots, booking widgets, etc.) to filter for SMBs still doing things manually
3. **Extract & Score** — Email extraction and pain-point scoring rank leads by how good a fit they are
4. **Dedup** — Supabase tracks previously delivered leads so the same business never gets sent twice
5. **Deliver** — Qualified leads land in Telegram for instant visibility and a Google Sheet for campaign tracking
6. **Schedule** — GitHub Actions runs the full pipeline daily, no manual intervention needed

## Stack

Python · Apify · Supabase · Google Sheets API · Telegram API · GitHub Actions (cron scheduling)

## Status

Live and running daily. Has delivered 299+ leads across multiple SMB niches targeting the Australian market.
