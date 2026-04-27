# Mission — Lightweight uptime monitoring for indie SaaS

A multi-tenant web app where solo founders and small teams set up HTTP
endpoint checks (status codes, response time, certificate expiry) and get
notified by email / Slack / Telegram when something breaks. Free tier
generous enough that hobby projects stay covered; paid tiers for teams that
need shorter intervals, more endpoints, or status pages.

## Who it's for

- Solo founders who can't justify Datadog pricing for a single side project
  but have outgrown a cron + curl setup.
- Two-to-five-person teams running a handful of services who want a single
  dashboard their non-engineers can read.
- Open-source maintainers who need a public status page their users can
  point fingers at when something's slow.

## Success looks like

- 60-second-cadence checks on the free tier, 30-second on paid, both delivered
  reliably from at least three geographic regions.
- Median alert latency under 90 seconds from outage start to first notification.
- A status page per project that's brand-able with a logo + colors and lives
  on a custom domain.
- $25 / month entry-level paid tier; no enterprise sales motion.
- 1,000 active free-tier accounts and 50 paying customers within 12 months.

## Out of scope

- Synthetic transaction monitoring (browser flows, login walks). That's the
  Checkly / Pingdom Pro competitive arena and we don't enter it.
- APM / tracing / logs. We do uptime; integrate with the user's existing
  tooling for the rest.
- White-label reseller plans. Single product, single brand.
