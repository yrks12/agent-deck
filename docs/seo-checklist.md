# shaliach.me: search and AI-answer checklist

What the site already does (guarded by `tests/test_site_seo.py`): a title, description,
canonical, Open Graph and Twitter card on every page; one `h1`; JSON-LD
(SoftwareApplication, Organization, WebSite, FAQPage, VideoObject, ItemList);
`sitemap.xml`; `robots.txt` that lets Google, Bing and the AI crawlers in (Google-Extended,
GPTBot, OAI-SearchBot, ClaudeBot, Claude-SearchBot, PerplexityBot); `llms.txt` and
`llms-full.txt`; alt text on every image.

What only the domain owner can do is below. Nothing here needs a code change.

## 1. Google Search Console (once, ~10 minutes)

1. Open <https://search.google.com/search-console>, signed in with the Google account that
   should own the site's data.
2. **Add property → Domain** (not "URL prefix"), enter `shaliach.me`.
3. Google shows a TXT record like `google-site-verification=AbC123...`. Copy the whole string.
4. Add it at the DNS host for `shaliach.me`:
   - Type `TXT`, Name/Host `@`, Value = the whole string, TTL 1 hour (or the default).
   - Keep every other record as it is (the GitHub Pages `A`/`AAAA`/`CNAME` records stay).
5. Wait 5-30 minutes, then press **Verify**. If it fails, check with
   `dig +short TXT shaliach.me` and wait for the value to appear.
6. **Sitemaps** → submit `https://shaliach.me/sitemap.xml`.
7. **URL inspection** → paste each URL in the sitemap → **Request indexing** (the home page
   first, then `/alternatives/` and the `/vs/` pages).

## 2. Bing Webmaster Tools (feeds Bing, DuckDuckGo, Copilot and ChatGPT search)

1. Open <https://www.bing.com/webmasters>, sign in.
2. Choose **Import from Google Search Console** (after step 1 is verified): no DNS change
   needed. Without Google, use the DNS option instead: Bing gives a `CNAME` record
   (Name = a code it shows, Value `verify.bing.com`); add it the same way as above.
3. Submit `https://shaliach.me/sitemap.xml`.
4. Optional: turn on **IndexNow** in Bing Webmaster so new pages are picked up within hours.

## 3. Checks after each publish

- <https://search.google.com/test/rich-results?url=https://shaliach.me/>: the FAQ,
  software app and video should show as valid.
- <https://pagespeed.web.dev/analysis?url=https://shaliach.me/>: performance 90+ on mobile.
- Paste `https://shaliach.me/` into a LinkedIn or X post draft: the card should show the
  image and the title.

## 4. The ask for whoever manages the DNS

> Please add one DNS record to shaliach.me so Google Search Console can verify the domain.
> Type: TXT. Name/Host: @. Value: the `google-site-verification=...` string from Search
> Console (the owner gets it in step 1.3 above and sends it to you). TTL: 1 hour.
> Do not change or remove any other record: the A/AAAA/CNAME records for GitHub Pages must
> stay exactly as they are. When it is saved, reply with the output of
> `dig +short TXT shaliach.me` so the owner can press Verify.
