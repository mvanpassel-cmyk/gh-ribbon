// GH Ribbon — hourly harvest trigger.
// GitHub's own cron for this repo fires only every 4–8 hours in practice, so this
// Cloudflare Worker starts the "Harvest feeds" workflow every hour instead.
// Setup: see cloudflare/README.md. Needs one secret, GITHUB_TOKEN.

const REPO = 'mvanpassel-cmyk/gh-ribbon';
const WORKFLOW = 'feeds.yml';

async function dispatch(env) {
  const r = await fetch(`https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${env.GITHUB_TOKEN}`,
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'User-Agent': 'gh-ribbon-harvest-trigger',
    },
    body: JSON.stringify({ ref: 'main' }),
  });
  // 204 = started. Anything else ends up in the Worker's logs.
  if (r.status !== 204) throw new Error(`GitHub answered ${r.status}: ${await r.text()}`);
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatch(env));
  },
  // Visiting the Worker URL only reports that it is alive; it never triggers a run,
  // so the token cannot be abused from the open web.
  async fetch() {
    return new Response('gh-ribbon harvest trigger: runs hourly via cron.\n', { headers: { 'content-type': 'text/plain' } });
  },
};
