# Hourly harvest trigger (Cloudflare Worker)

GitHub runs scheduled workflows on a best-effort basis; for this repo the hourly
cron actually fired every 4–8 hours. This Worker calls `workflow_dispatch` on
`feeds.yml` every hour. The GitHub cron stays as a backup and skips itself when
the last harvest is under 40 minutes old.

## 1. GitHub token (fine-grained, one repo only)

GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token

- Token name: `gh-ribbon harvest trigger`
- Expiration: 1 year (put a reminder in your calendar)
- Repository access: **Only select repositories** → `mvanpassel-cmyk/gh-ribbon`
- Repository permissions: **Actions: Read and write** (Metadata: Read-only is added automatically). Nothing else.

Copy the token (starts with `github_pat_`).

## 2. Worker (Cloudflare dashboard, no install needed)

Cloudflare dashboard → Workers & Pages → Create → Create Worker

- Name: `gh-ribbon-harvest` → Deploy
- Edit code → replace everything with the contents of `harvest-trigger.js` → Deploy
- Settings → Variables and Secrets → Add → type **Secret**, name `GITHUB_TOKEN`, value = the token → Deploy
- Settings → Trigger events → Add → **Cron Triggers** → `5 * * * *` (every hour at :05, UTC) → Add

## 3. Check

After the next :05, GitHub → Actions → "Harvest feeds" shows a run with event
`workflow_dispatch`. In Cloudflare: Worker → Logs shows any error (a 401 or 403
means the token is wrong or lacks Actions write).

Cost: free plan (24 invocations a day of a 100,000 daily allowance).
