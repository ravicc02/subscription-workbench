# Subscription package format

The subscription plaza imports portable public source configurations. It does not create workspace subscriptions or bots; the user explicitly connects a category or multiple tags to a workspace after import. Initial polling establishes a baseline and does not replay historical entries.

## JSON v1

```json
{
  "format": "subscription-workbench/package",
  "version": 1,
  "package": {
    "key": "example-pack",
    "name": "Example",
    "description": "Public sources only"
  },
  "categories": [
    {
      "key": "technology",
      "name": "Technology",
      "tags": [
        { "key": "engineering", "name": "Engineering" }
      ],
      "sources": [
        {
          "name": "Engineering RSS",
          "type": "rss",
          "url": "https://example.org/feed.xml",
          "tags": ["engineering"],
          "poll_minutes": 15,
          "capture_mode": "full"
        },
        {
          "name": "Release page",
          "type": "webpage",
          "url": "https://example.org/releases",
          "selector": "main",
          "tags": ["engineering"],
          "poll_minutes": 30
        },
        {
          "name": "Public JSON",
          "type": "api",
          "url": "https://example.org/api/releases",
          "json_path": "data.items",
          "id_field": "id",
          "title_field": "title",
          "url_field": "url",
          "content_field": "description",
          "published_field": "published_at",
          "tags": ["engineering"],
          "poll_minutes": 30
        }
      ]
    }
  ]
}
```

- `key` is a stable lowercase ASCII identifier, beginning with a letter (up to 48 characters). Category keys are package-wide; tag keys are unique within a category. A source belongs to one category and may reference multiple defined tags.
- `type`: `rss` / `webpage` / `api`. `webhook` is configured only in the local UI because its token and sender relationship cannot be safely moved in a public package.
- Optional source fields: `proxy_mode` (`inherit`, `direct`, `proxy`), `domains` (RSS body allowlist), `selector` (webpage), `json_path` and the five API mapping fields, `allow_undated` (RSS only), `capture_mode` (`full` or RSS `summary`), `update_on_change` (boolean). Polling intervals: 5, 10, 15, 30 or 60 minutes.
- Packages contain public HTTPS URLs only. Private addresses, private credentials, arbitrary headers and unknown fields are rejected. Import checks DNS and policy for new addresses before changing the database. A public URL is not proof that the provider allows a given use: respect its license, terms, robots policy and limits.
- Import is additive and atomic. Categories and tags match by stable key; sources match by type and normalized URL. Existing source runtime settings are preserved, new tags are merged, and conflicting classifications require manual resolution. Re-import does not create duplicates or overwrite credentials. Export includes only categories, tags and public source settings. It excludes Webhook credentials, bot/model configuration and runtime state.
- Preview counts distinguish already matched sources from existing sources that still need classification or package tags. The plaza keeps source details collapsed until requested. After import, the subscription form opens with the category preselected for a single-category package; fully synced packages can open the form directly. Import never creates or enables a subscription by itself.
- New workspace subscriptions choose one category and at least one tag. Tags match as a union; a source matching multiple tags is processed once. Each tag can contain multiple source links, and newly categorized sources match automatically. Existing explicit-source subscriptions keep running until edited, when the UI requires a category and tags. Each new item still passes through the workspace's chosen processing mode and is routed to bots bound to that workspace.

The built-in finance, technology and GitHub Hot files are under `packs/`. The GitHub Hot RSS aggregator is community-maintained and has no publication timestamp; the package opts into undated entries, uses feed summaries, and deduplicates by stable item identity. GitHub Trending webpage monitoring treats the selected page area as one change event, not a structured list of individual repositories. Availability and content can change independently of this project.
